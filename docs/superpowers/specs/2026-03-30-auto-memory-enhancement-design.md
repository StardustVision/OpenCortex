# Auto Memory Enhancement

Absorb five design patterns from Claude Code's auto memory mechanism into OpenCortex: typed memory, memory index API, store/don't-store rules, relevance hints, and pre-use verification.

## Motivation

Claude Code's auto memory has well-defined memory types (user/feedback/project/reference) with distinct storage and retrieval strategies. OpenCortex currently has a flat `context_type` enum (memory/resource/skill/case/pattern) without clear guidance on when to store, when to recall, or how to validate stale memories. This enhancement brings structured intent to the memory lifecycle.

## 1. Consolidate context_type to 4 Types

### Current State

`ContextType` enum in `src/opencortex/retrieve/types.py`:

```python
class ContextType(str, Enum):
    MEMORY = "memory"
    RESOURCE = "resource"
    SKILL = "skill"
    CASE = "case"
    PATTERN = "pattern"
    STAGING = "staging"
    ANY = "any"
```

### New State

```python
class ContextType(str, Enum):
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"
    STAGING = "staging"      # internal, unchanged
    ANY = "any"              # internal, unchanged
```

### Type Definitions

| Type | Purpose | Mergeable | Decay Rate | Recall Priority |
|------|---------|-----------|------------|-----------------|
| `user` | User role, preferences, knowledge, working style | Yes (category-scoped) | Slow (standard) | High — always include in recall context |
| `feedback` | Behavioral corrections, confirmed approaches, patterns | Yes (category-scoped) | Slow (standard) | High — returned as behavioral constraints |
| `project` | Active work, goals, deadlines, bugs, incidents | No | Fast (2x standard) | Medium — sorted by recency |
| `reference` | External resource pointers, reusable procedures, docs | No | Slow (standard) | Normal — by relevance |

### Migration Map (Category-Aware)

The old `memory` type is a catch-all that includes user profiles, events, plans, and fixes. A flat `memory→user` mapping would incorrectly promote ephemeral content to high-priority user profile status. Migration uses the existing `category` field to route accurately:

| Old Type | Category | New Type | Rationale |
|----------|----------|----------|-----------|
| `memory` | `profile`, `preferences`, `entities` | `user` | User-centric, stable |
| `memory` | `patterns`, `strategies` | `feedback` | Behavioral guidance |
| `memory` | `events`, `plans`, `error_fixes`, `workflows` | `project` | Time-bound, work-scoped |
| `memory` | `documents` | `reference` | Reference material |
| `memory` | empty `""` or unknown | `project` | Safe default — gets faster decay and verification, avoids polluting user/feedback pools |
| `resource` | any | `reference` | Same concept, renamed |
| `skill` | any | `reference` | Reusable procedures are reference material |
| `case` | any | `project` | Problem cases are project-scoped |
| `pattern` | any | `feedback` | Recurring patterns guide behavior |

### Migration Strategy: Blocking with Dual-Value Enum

Current `_startup_maintenance()` runs as a background `create_task()` after init returns (`orchestrator.py:225`), meaning the server is already accepting requests while migration runs. A context_type migration under this model creates a dangerous mixed-value window: code expects new types, data still has old ones, causing filter misses and `ContextType()` parse failures.

**Solution: two-phase rollout.**

**Phase 1 — Dual-value enum + blocking migration (this release):**

The `ContextType` enum keeps BOTH old and new values during migration:

```python
class ContextType(str, Enum):
    # New canonical values
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"
    # Legacy aliases (accepted on read, never written)
    MEMORY = "memory"
    RESOURCE = "resource"
    SKILL = "skill"
    CASE = "case"
    PATTERN = "pattern"
    # Internal
    STAGING = "staging"
    ANY = "any"
```

This ensures `ContextType("memory")` never raises `ValueError` even if a record hasn't been migrated yet.

The migration itself runs **blocking** in `__init__` (before `self._initialized = True`), NOT in `_startup_maintenance()`:

```python
# In orchestrator.init(), BEFORE setting _initialized = True:
from opencortex.migration.v070_context_type_consolidation import migrate_context_types
await migrate_context_types(self._storage, self._get_collection())
```

This eliminates the mixed-value window entirely. Migration is idempotent (skips records already using new values) and fast (Qdrant payload-only update, no re-embedding).

**Phase 2 — Remove legacy aliases (next release):**

Once all data is migrated and clients updated, remove `MEMORY/RESOURCE/SKILL/CASE/PATTERN` from the enum.

### Backward Compatibility: Full Read/Write Path

The spec must cover ALL entry points, not just store. Every path that parses or filters by `context_type` needs dual-value support:

| Layer | File | What changes |
|-------|------|-------------|
| **HTTP store** | `http/server.py:store` | Accept old values, map to new, return `deprecation_warning` |
| **HTTP search** | `http/server.py:search` | Accept old values in `context_type` filter param, map to new |
| **HTTP client** | `http/client.py:128,167` | Accept old values, map to new when constructing requests |
| **HTTP models** | `http/models.py:21,33` | Update `MemoryStoreRequest.context_type` and `MemorySearchRequest.context_type` validators to accept both old and new |
| **MCP store tool** | `mcp-server.mjs:38` | Update enum in tool schema |
| **MCP search tool** | `mcp-server.mjs:57` | Update enum in tool schema |
| **MCP recall tool** | `mcp-server.mjs:94` | No filter change needed (recall doesn't filter by type) |
| **Retriever** | `hierarchical_retriever.py:1399-1403` | Dual-value enum handles fallback naturally |
| **Orchestrator add()** | `orchestrator.py:900` | Map old values to new before writing |
| **Dedup check** | `orchestrator.py:1029` | Pass new type values |

The mapping function (shared across all layers):

```python
_LEGACY_TYPE_MAP = {
    "memory": "user",    # Note: store endpoint uses category-aware mapping
    "resource": "reference",
    "skill": "reference",
    "case": "project",
    "pattern": "feedback",
}

def normalize_context_type(raw: str) -> str:
    """Map legacy context_type values to new canonical values."""
    return _LEGACY_TYPE_MAP.get(raw, raw)
```

For HTTP store specifically, the mapping is category-aware (using the table above). For search/filter paths, the simpler `_LEGACY_TYPE_MAP` suffices since the caller doesn't have category context.

### Merge Scope: Category-Scoped, Not Type-Wide

Current dedup in `_check_duplicate()` (`orchestrator.py:1175-1228`) filters by `category` when non-empty, but falls back to tenant+leaf-only when category is `""`. The current `MERGEABLE_CATEGORIES` (`profile`, `preferences`, `entities`, `patterns`) acts as a second gate at merge-decision time.

If we replace `MERGEABLE_CATEGORIES` with `MERGEABLE_TYPES = {USER, FEEDBACK}` without tightening the dedup filter, any two `user`-type memories with empty category and high vector similarity would merge — even if semantically unrelated.

**Solution: require non-empty category for merge to trigger.**

```python
MERGEABLE_TYPES = frozenset({ContextType.USER, ContextType.FEEDBACK})

# In add(), at the merge decision point (orchestrator.py:1038):
if (
    effective_category
    and effective_category in MERGEABLE_CATEGORIES
    and ContextType(context_type) in MERGEABLE_TYPES
):
    await self._merge_into(existing_uri, abstract, content)
```

This is a dual gate:
1. `context_type` must be `user` or `feedback` (type-level intent)
2. `category` must be non-empty AND in `MERGEABLE_CATEGORIES` (content-level scope)

`MERGEABLE_CATEGORIES` is retained as-is: `{"profile", "preferences", "entities", "patterns"}`. Both gates must pass for merge. Empty-category memories always create new records, even for mergeable types.

Additionally, add `context_type` to the dedup filter query (`_check_duplicate`):

```python
conds.append(
    {"op": "must", "field": "context_type", "conds": [context_type]}
)
```

This prevents cross-type dedup matches (a `user` memory should never merge into a `project` memory).

## 2. Memory Index API

### Endpoint

```
GET /api/v1/memory/index?type=user,feedback&limit=200
```

### Response

```json
{
  "index": {
    "user": [
      {"uri": "opencortex://t/u/memories/user/abc123", "abstract": "Senior backend engineer, deep Go expertise", "created_at": "2026-03-28T10:00:00Z"}
    ],
    "feedback": [...],
    "project": [...],
    "reference": [...]
  },
  "total": 42
}
```

### Implementation

- Query Qdrant with scroll (no embedding needed), return `uri` + `abstract` + `context_type` + `created_at`
- Group by `context_type`
- Truncate each `abstract` to 150 characters
- Default limit 200 records, ordered by `created_at` desc within each group
- Filter by `type` query parameter (comma-separated)

### MCP Tool

New tool `memory_index` in `mcp-server.mjs`:

```javascript
memory_index: {
  description: "Get a lightweight index of all stored memories, grouped by type. Call at session start to understand what context is available.",
  inputSchema: {
    type: { type: "string", description: "Comma-separated types to include (user,feedback,project,reference). Omit for all." }
  }
}
```

## 3. Store/Don't-Store Rules

### Prompt Layer

Add to the MCP `usage-guide` prompt:

```
## What to Store

- **user**: Role, expertise, preferences, working style, communication preferences
- **feedback**: Corrections to your behavior, confirmed good approaches, patterns to follow/avoid
- **project**: Active goals, deadlines (use absolute dates), decisions, blockers, incidents
- **reference**: URLs, doc locations, tool pointers, reusable procedures

## What NOT to Store

- Code structure, file paths, architecture — derivable from reading the codebase
- Git history, recent changes — use git log / git blame
- Debugging steps or fix recipes — the fix is in the code, the context in the commit
- Anything already in CLAUDE.md or project docs
- Ephemeral task state or current conversation context
```

### Server Soft Checks

In the `store` endpoint handler (`http/server.py`), add warnings (HTTP 200 with `warnings` array, never reject):

| Condition | Warning Message |
|-----------|-----------------|
| `len(abstract) < 10` | `"abstract_too_short: Memory abstract should be at least 10 characters for useful retrieval"` |
| Abstract is >80% code (heuristic: line starts with common code patterns) | `"code_snippet_detected: Consider storing a description of the code pattern rather than raw code"` |
| Dedup match with similarity > 0.95 | `"near_duplicate: Very similar memory exists at {uri}. Consider updating it instead of creating a new one"` |

Response shape when warnings exist:

```json
{
  "uri": "...",
  "context_type": "user",
  "warnings": ["abstract_too_short: ..."]
}
```

No warnings → `warnings` key omitted.

## 4. Relevance Hint

### Store API

Accept optional `meta.relevance_hint` (string) in store requests:

```json
{
  "abstract": "Team uses PostgreSQL for production",
  "context_type": "project",
  "meta": {
    "relevance_hint": "When discussing database choices or migration"
  }
}
```

### Recall Integration: Full Pipeline

`relevance_hint` must flow through the entire retrieval pipeline, not just "surface in serialization." Current pipeline:

```
Qdrant payload → _build_one() in hierarchical_retriever.py:1405
    → MatchedContext (no meta field currently)
    → _format_memories() in context/manager.py:702
    → recall response dict
```

**Changes required at each layer:**

1. **MatchedContext** (`retrieve/types.py:347`): Add `meta: Dict[str, Any] = field(default_factory=dict)` field
2. **Retriever `_build_one()`** (`hierarchical_retriever.py:1405`): Pass `meta=c.get("meta", {})` when constructing MatchedContext
3. **ContextManager `_format_memories()`** (`context/manager.py:708`): Extract and surface `relevance_hint`:
   ```python
   if matched.meta.get("relevance_hint"):
       item["relevance_hint"] = matched.meta["relevance_hint"]
   ```
4. **MCP server**: No change needed — MCP passes through whatever the HTTP API returns

No new Qdrant schema field — `meta` is already stored as a JSON payload in Qdrant. The change is about reading it back through the pipeline.

## 5. Pre-Use Verification Flag

### Recall Response

Add `needs_verification: bool` to each result in recall/search responses:

```json
{
  "uri": "...",
  "abstract": "Merge freeze begins 2026-03-05",
  "context_type": "project",
  "needs_verification": true,
  "verification_reason": "project memory older than 7 days"
}
```

### Rules

| context_type | Condition | needs_verification |
|-------------|-----------|-------------------|
| `project` | `created_at` older than 7 days | `true` |
| `reference` | Always (external URLs may change) | `true` |
| `user` | Never | `false` |
| `feedback` | Never | `false` |

### Full Pipeline Changes

Same pipeline issue as relevance_hint — `created_at` is not currently in MatchedContext.

1. **MatchedContext** (`retrieve/types.py:347`): Add `created_at: str = ""` field
2. **Retriever `_build_one()`** (`hierarchical_retriever.py:1405`): Pass `created_at=c.get("created_at", "")`
3. **ContextManager `_format_memories()`** (`context/manager.py:708`): Compute and append:
   ```python
   needs_verification = False
   reason = ""
   ct = str(matched.context_type)
   if ct == "reference":
       needs_verification = True
       reason = "reference may have changed"
   elif ct == "project" and matched.created_at:
       age_days = (datetime.utcnow() - datetime.fromisoformat(matched.created_at)).days
       if age_days > 7:
           needs_verification = True
           reason = f"project memory older than {age_days} days"
   if needs_verification:
       item["needs_verification"] = True
       item["verification_reason"] = reason
   ```
4. **MCP server**: Pass through — no additional change needed

## Files Changed

### New Files
- `src/opencortex/migration/v070_context_type_consolidation.py` — blocking migration (category-aware type mapping)

### Modified Files
- `src/opencortex/retrieve/types.py` — `ContextType` enum (dual-value with legacy aliases), `MERGEABLE_TYPES`, MatchedContext gains `meta` + `created_at` fields
- `src/opencortex/orchestrator.py` — blocking migration call in init(), dual-gate merge logic, `context_type` filter in dedup, `normalize_context_type()` in add()
- `src/opencortex/retrieve/hierarchical_retriever.py` — pass `meta` + `created_at` in `_build_one()`
- `src/opencortex/context/manager.py` — surface `relevance_hint` + `needs_verification` in `_format_memories()`
- `src/opencortex/http/server.py` — new `/api/v1/memory/index` endpoint, store warning checks, `normalize_context_type()` in search/store handlers
- `src/opencortex/http/models.py` — update validators to accept both old and new type values, index response model
- `src/opencortex/http/client.py` — `normalize_context_type()` on outgoing requests
- `src/opencortex/storage/collection_schemas.py` — no schema change
- `plugins/opencortex-memory/lib/mcp-server.mjs` — new `memory_index` tool, update type enums in store/search tool schemas, update `usage-guide` prompt
- `src/opencortex/prompts.py` — update usage-guide prompt content

## Backward Compatibility

- **Enum**: `ContextType` keeps both old and new values. `ContextType("memory")` continues to parse without error.
- **Migration**: Runs blocking before server accepts requests. No mixed-value window.
- **All API paths** (store, search, recall, client, MCP tools): Accept old values, map to new via `normalize_context_type()`. Store returns `deprecation_warning` for old values.
- **Merge safety**: Dual gate (type + category). Empty-category memories never merge. `context_type` added to dedup filter to prevent cross-type merges.
- **Phase 2 cleanup**: Next release removes legacy enum values after confirming all data migrated and all clients updated.
- `category` field unchanged — still free-form, still works as before.
- `STAGING` and `ANY` internal types unchanged.
