# Memory Pipeline Enhancement Design

Date: 2026-03-03

## Problem

Current memory formation has gaps and non-deterministic behavior:

1. **oc-scan not implemented** — no automated document import
2. **Session LLM extraction disconnected** — MemoryExtractor exists but hooks don't call it
3. **URI node_id is random** — `uuid4().hex[:12]` prevents idempotent upsert
4. **No Snowflake ID** — Qdrant uses auto UUID, not portable to distributed setup
5. **Scope logic incomplete** — documents should support user→shared promotion

## Three Memory Formation Paths

```
Path 1: oc-scan → documents (deterministic import, whole-file, LLM abstract)
Path 2: session hooks → LLM extraction → preference / case / pattern
Path 3: ACE → skill extraction (disabled, not in scope)
Path 0: Agent direct memory_store (unchanged, cannot restrict)
```

## Design

### A. Dual ID System (OpenViking Pattern)

#### A1. Snowflake Generator — Qdrant Point ID

Port OpenViking's `SnowflakeGenerator` for vector DB primary keys.

**File**: `src/opencortex/utils/id_generator.py`

```python
class SnowflakeGenerator:
    EPOCH = 1704067200000  # 2024-01-01 00:00:00 UTC
    # 1 bit sign + 41 bit timestamp(ms) + 5 bit datacenter + 5 bit worker + 12 bit sequence
    # Thread-safe, clock-skew handling (wait ≤5ms, else raise)

_default_generator = SnowflakeGenerator()
def generate_id() -> int:
    return _default_generator.next_id()
```

**Integration**: `QdrantStorageAdapter.upsert()` receives explicit `point_id: int` instead of relying on Qdrant auto-UUID.

#### A2. Semantic Node Name — URI Identifier

Replace `uuid4().hex[:12]` with deterministic semantic names from abstract/filename.

**File**: Update `src/opencortex/orchestrator.py` `_auto_uri()`

```python
def _semantic_node_name(text: str, max_length: int = 50) -> str:
    """Sanitize text for URI segment (OpenViking pattern)."""
    # Preserve: letters, digits, CJK, underscore, hyphen
    safe = re.sub(r'[^\w\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af-]', '_', text)
    safe = re.sub(r'_+', '_', safe).strip('_')[:max_length]
    if not safe:
        safe = "unnamed"
    # Truncation: append SHA256[:8] suffix
    if len(text) > max_length:
        import hashlib
        hash_suffix = hashlib.sha256(text.encode()).hexdigest()[:8]
        safe = f"{safe[:max_length - 9]}_{hash_suffix}"
    return safe
```

**Conflict resolution**: When URI already exists, append `_1`, `_2`, ... (max 100 attempts).

```python
async def _resolve_unique_uri(self, uri: str) -> str:
    if not await self._uri_exists(uri):
        return uri
    for i in range(1, 101):
        candidate = f"{uri}_{i}"
        if not await self._uri_exists(candidate):
            return candidate
    raise ValueError(f"URI conflict unresolved: {uri}")
```

#### A3. URI Examples After Change

| Path | Input text | URI |
|------|-----------|-----|
| oc-scan | `docs/architecture.md` | `resources/{project}/documents/docs__architecture_md` |
| memory (preference) | `用户偏好深色主题` | `user/{uid}/memories/preferences/用户偏好深色主题` |
| case | `Fix import error by checking PYTHONPATH` | `shared/cases/Fix_import_error_by_checking_PYTHONPATH` |
| pattern | `代码评审证据标准禁止假设性结论` | `shared/patterns/代码评审证据标准禁止假设性结论` |
| session extraction | `偏好使用bun不用npm` | `user/{uid}/memories/preferences/偏好使用bun不用npm` |

**Idempotency**: Same abstract → same URI → Qdrant upsert overwrites → no duplicates.

---

### B. oc-scan Deterministic Document Import

#### B1. Scan Script

**File**: `plugins/opencortex-memory/bin/oc-scan.mjs` (new)

- Pure Node.js, zero external dependencies
- Discovery: `git ls-files` (fast) → fallback to recursive walk
- Each file → 1 item (no chunking)
- Supported: `.md`, `.mdx`, `.py`, `.js`, `.ts`, `.go`, `.rs`, `.java`, `.yaml`, `.json`, `.toml`, etc.
- Max file size: 1 MB
- Skip: `.git`, `node_modules`, `__pycache__`, `.venv`, `dist`, `build`

**Output**:
```json
{
  "items": [
    {
      "content": "<full file content>",
      "category": "documents",
      "context_type": "resource",
      "meta": { "source": "scan", "file_path": "docs/arch.md", "file_type": "markdown" }
    }
  ],
  "source_path": "/abs/path",
  "scan_meta": { "total_files": 42, "has_git": true, "project_id": "OpenCortex" }
}
```

Note: No abstract/overview generated client-side. No URI generated client-side. Server handles both.

#### B2. Server Batch Store Endpoint

**Endpoint**: `POST /api/v1/memory/batch_store`

**Request**:
```python
class MemoryBatchItem(BaseModel):
    content: str
    category: str = "documents"
    context_type: str = "resource"
    meta: Optional[Dict[str, Any]] = None

class MemoryBatchStoreRequest(BaseModel):
    items: List[MemoryBatchItem]
    source_path: str = ""
    scan_meta: Optional[Dict[str, Any]] = None
```

**Server processing per item**:
1. LLM generates abstract (L0, 1-2 sentences) + overview (L1, 1 paragraph)
2. `_semantic_node_name(file_path)` → deterministic URI node name
3. `_auto_uri()` builds full URI with `context_type="resource"`
4. `generate_id()` → Snowflake point_id
5. Embed abstract → vector
6. Qdrant upsert + CortexFS write (scope=private)

**Response**:
```json
{
  "status": "ok",
  "total": 42,
  "imported": 42,
  "has_git_project": true,
  "project_id": "OpenCortex",
  "uris": ["opencortex://netops/user/liaowh4/resources/documents/..."]
}
```

#### B3. Scope Promotion Flow

After batch_store returns, the caller (skill/hook) checks `has_git_project`:

- **true** → Return `systemMessage`: "已导入 N 条文档到个人空间。是否升级为项目 {project} 共享？"
  - User confirms → `POST /api/v1/memory/promote_to_shared` with URI list
  - Batch rewrite: `user/{uid}/resources/...` → `resources/{project}/documents/...`
  - Update Qdrant `scope` field: `private` → `shared`
  - Update CortexFS path
- **false** → Keep user private, no prompt

**Orchestrator method**:
```python
async def promote_to_shared(self, uris: List[str], project_id: str) -> Dict[str, Any]:
    """Batch promote private resources to shared project scope."""
    # For each URI:
    #   1. Read existing record from Qdrant
    #   2. Build new shared URI: resources/{project}/documents/{node_name}
    #   3. Update record: uri, scope="shared", project_id
    #   4. Qdrant upsert with new URI + CortexFS move
    #   5. Delete old private record
```

---

### C. Session LLM Extraction

#### C1. Stop Hook Enhancement (per turn)

**File**: `plugins/opencortex-memory/hooks/handlers/stop.mjs`

Current behavior (kept):
- Store raw turn as `category=session`

New additions:
```javascript
// 1. Buffer turn in SessionManager
await httpPost(`${httpUrl}/api/v1/session/message`, {
  session_id: state.session_id,
  role: 'user',
  content: turn.userText,
});
await httpPost(`${httpUrl}/api/v1/session/message`, {
  session_id: state.session_id,
  role: 'assistant',
  content: turn.assistantText,
});

// 2. Extract from single turn
await httpPost(`${httpUrl}/api/v1/session/extract_turn`, {
  session_id: state.session_id,
}, 15000);  // LLM call, longer timeout
```

#### C2. New Endpoint: extract_turn

**Endpoint**: `POST /api/v1/session/extract_turn`

**Server processing**:
1. SessionManager retrieves latest 2 messages (1 user + 1 assistant)
2. MemoryExtractor analyzes single turn with LLM
3. Filters by `confidence >= 0.7`
4. Stores results via `orchestrator.add()` (with dedup)
5. Categories: `preference`, `case`, `pattern` (as determined by LLM)

#### C3. Session-End Hook Enhancement

**File**: `plugins/opencortex-memory/hooks/handlers/session-end.mjs`

Current behavior (kept):
- Store session summary

New addition:
```javascript
// Trigger full session extraction
await httpPost(`${httpUrl}/api/v1/session/end`, {
  session_id: state.session_id,
  quality_score: 0.5,
}, 30000);  // LLM analyzes all turns, needs more time
```

**Server processing**:
1. SessionManager.end_session() calls MemoryExtractor with all buffered messages
2. MemoryExtractor analyzes full conversation, extracts memories
3. Filters by `confidence >= 0.7`
4. Write-time semantic dedup against existing + stop-hook-extracted memories
5. Stores results

---

## Files to Modify / Create

| File | Action | Description |
|------|--------|-------------|
| `src/opencortex/utils/id_generator.py` | **Create** | Snowflake generator (port from OpenViking) |
| `src/opencortex/orchestrator.py` | Modify | `_auto_uri()` → semantic names; `_resolve_unique_uri()`; `batch_add()`; `promote_to_shared()` |
| `src/opencortex/storage/qdrant/adapter.py` | Modify | Accept explicit `point_id: int` in upsert |
| `src/opencortex/http/server.py` | Modify | Add `/batch_store`, `/promote_to_shared`, `/session/extract_turn` endpoints |
| `src/opencortex/http/models.py` | Modify | Add `MemoryBatchItem`, `MemoryBatchStoreRequest`, `PromoteRequest` |
| `src/opencortex/session/manager.py` | Modify | Add `extract_turn()` method |
| `plugins/.../bin/oc-scan.mjs` | **Create** | Client-side file scanner |
| `plugins/.../handlers/stop.mjs` | Modify | Add session/message + extract_turn calls |
| `plugins/.../handlers/session-end.mjs` | Modify | Add session/end call |
| `plugins/.../lib/mcp-server.mjs` | Modify | Add `memory_batch_store` tool |

## Implementation Phases

| Phase | Content | Dependency |
|-------|---------|-----------|
| **Phase 1** | Snowflake ID generator + semantic URI naming | None |
| **Phase 2** | Session LLM extraction (stop + session-end hooks) | Phase 1 (for semantic URIs) |
| **Phase 3** | oc-scan + batch_store + LLM abstract generation | Phase 1 |
| **Phase 4** | promote_to_shared scope upgrade | Phase 3 |

## Migration

Existing records (154) have random UUID node_ids. No migration needed — new records use semantic names, old records remain accessible. Dedup will match by semantic similarity, not by URI.
