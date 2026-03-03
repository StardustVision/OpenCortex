# Write-Time Semantic Deduplication

## Problem

`orchestrator.add()` has no semantic dedup — every call with a unique URI creates a new record, even if nearly identical content already exists. The only dedup is in `SessionManager._try_merge()` (0.85 threshold, mergeable categories only), which doesn't cover direct `add()` calls via MCP, HTTP, or hooks.

## Solution

Add a dedup check inside `orchestrator.add()`, between embedding and upsert. Reuse the already-generated vector for a single search query — zero extra embedding cost.

### Flow

```
add(abstract, content, ..., dedup=True, dedup_threshold=0.82)
  → embed(abstract) → vector         # existing step
  → if dedup:
      search(vector, filter={category, tenant, scope}, limit=1)
      if top1.score >= threshold:
        mergeable category → merge content + feedback(0.5), return existing URI
        non-mergeable      → skip, return existing URI
  → else: upsert + write CortexFS    # existing step
```

### Dedup Filter

Scoped to: same `source_tenant_id` + same `category` + `is_leaf=True` + accessible scope (shared OR private+own_user).

### Return Value

`Context.meta["dedup_action"]`: `"created"` (default), `"merged"`, or `"skipped"`.

### Parameter Propagation

| Layer | Change |
|-------|--------|
| `orchestrator.add()` | Add `dedup: bool = True`, `dedup_threshold: float = 0.82` |
| `http/models.py` | `MemoryStoreRequest.dedup: bool = True` |
| `http/server.py` | Pass dedup to add() |
| `mcp-server.mjs` | `memory_store` tool gets `dedup` param (bool, default true) |

### SessionManager Simplification

Remove `_try_merge()`. `_store_memory()` calls `add(dedup=True)` and checks `meta.dedup_action` for merged/skipped counts.

### Performance

- 1 vector search on embedded Qdrant (~5-15ms)
- No extra embedding (reuses add()'s vector)
- `dedup=False` escape hatch for bulk import

### Tests

- Same abstract twice → second skipped
- Similar abstract + mergeable → merged
- Similar abstract + non-mergeable → skipped
- Different abstract → created
- dedup=False → force write
- Cross-tenant → no cross-dedup
- SessionManager stats (stored/merged/skipped) via add(dedup=True)
