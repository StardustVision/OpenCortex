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
| `user` | User role, preferences, knowledge, working style | Yes | Slow (standard) | High — always include in recall context |
| `feedback` | Behavioral corrections, confirmed approaches, patterns | Yes | Slow (standard) | High — returned as behavioral constraints |
| `project` | Active work, goals, deadlines, bugs, incidents | No | Fast (2x standard) | Medium — sorted by recency |
| `reference` | External resource pointers, reusable procedures, docs | No | Slow (standard) | Normal — by relevance |

### Migration Map

| Old Type | New Type | Rationale |
|----------|----------|-----------|
| `memory` | `user` | Default catch-all becomes user-oriented |
| `resource` | `reference` | Same concept, renamed |
| `skill` | `reference` | Reusable procedures are reference material |
| `case` | `project` | Problem cases are project-scoped |
| `pattern` | `feedback` | Recurring patterns guide behavior |

### Migration Strategy

One-time migration at server startup, similar to existing `v030_path_redesign.py`:

- New file: `src/opencortex/migration/v070_context_type_consolidation.py`
- Scan all records in the collection
- Map old `context_type` values to new using the table above
- Update `mergeable` field: `user` and `feedback` → True, `project` and `reference` → False
- Idempotent: skip records already using new type values
- Run automatically on startup (orchestrator init), guarded by a migration version flag

### Category Simplification

Current `MERGEABLE_CATEGORIES` in `retrieve/types.py`:

```python
MERGEABLE_CATEGORIES = frozenset({"profile", "preferences", "entities", "patterns"})
```

After consolidation, mergeability is determined by `context_type`, not `category`:

```python
MERGEABLE_TYPES = frozenset({ContextType.USER, ContextType.FEEDBACK})
```

The `category` field remains as a free-form tag for sub-classification (e.g., user memories can still be tagged "preferences" or "profile"), but it no longer drives merge behavior.

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

### Recall Integration

During recall, if a retrieved memory has `meta.relevance_hint`, append it to the returned result as a separate field:

```json
{
  "uri": "...",
  "abstract": "Team uses PostgreSQL for production",
  "relevance_hint": "When discussing database choices or migration",
  "score": 0.87
}
```

### Implementation

- No new Qdrant schema field — stored in the existing `meta` JSON payload
- Extract and surface in `MatchedContext` serialization if present
- No embedding of relevance_hint — it's metadata for the consuming agent, not for search ranking

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

### Implementation

- Computed at recall time in `MatchedContext` serialization, not stored
- `verification_reason` provides context: `"project memory older than 7 days"` or `"reference may have changed"`
- Agent sees the flag and should verify before acting on the memory

## Files Changed

### New Files
- `src/opencortex/migration/v070_context_type_consolidation.py` — one-time migration

### Modified Files
- `src/opencortex/retrieve/types.py` — `ContextType` enum, `MERGEABLE_TYPES` replaces `MERGEABLE_CATEGORIES`
- `src/opencortex/orchestrator.py` — update all `context_type` references, merge logic uses `MERGEABLE_TYPES`
- `src/opencortex/http/server.py` — new `/api/v1/memory/index` endpoint, store warning checks
- `src/opencortex/http/models.py` — index response model
- `src/opencortex/storage/collection_schemas.py` — no schema change (meta is already flexible)
- `plugins/opencortex-memory/lib/mcp-server.mjs` — new `memory_index` tool, update `store` tool description, update `usage-guide` prompt, surface `needs_verification` and `relevance_hint` in recall results
- `src/opencortex/prompts.py` — update usage-guide prompt content if served from Python side

## Backward Compatibility

- Old `context_type` values (`memory`, `resource`, `skill`, `case`, `pattern`) are migrated once at startup
- MCP `store` tool continues to accept old values during a transition period: server maps them to new types with a deprecation warning
- `category` field unchanged — still free-form, still works as before
- `STAGING` and `ANY` internal types unchanged
