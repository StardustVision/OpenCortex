# Frontier Batching + Late Rerank: HierarchicalRetriever Optimization

## Problem

Current `_recursive_search` uses a heapq-based per-directory loop: each `heappop` triggers a separate `storage.search()` call and potentially a `rerank()` call. For a tree with N directory nodes, this produces O(N) search calls and up to O(N) rerank calls.

## Solution: Plan A+ (Frontier Batching with Guardrails)

Replace per-directory search with wave-based frontier batching. Each wave batches all frontier directories into a single `storage.search()` call using `parent_uri IN [...]` filter. Rerank moves to a single late pass on the final candidate set.

### Call Count Reduction

| Metric | Current | After |
|---|---|---|
| `storage.search` calls | O(N) directory nodes | O(D) waves + O(S) compensation (D = tree depth typically 2-3, S = starved parent count typically 0-2) |
| `rerank` calls | up to N | 1 (late rerank) |

## Algorithm

```
Constants:
  MAX_FRONTIER_SIZE = 64
  MIN_CHILDREN_PER_DIR = 2
  LATE_RERANK_FACTOR = 5
  LATE_RERANK_CAP = 50
  DEFAULT_MAX_WAVES = 8       # configurable via __init__

Input: starting_points, query, query_vector, collection, limit, threshold

Init:
  frontier = starting_points
  collected: Dict[str, Dict] = {}       # uri -> dict, O(1) dedup + keeps higher score
  visited_dirs: Set[str] = set()
  convergence_rounds = 0
  prev_topk_uris = set()

Wave Loop (wave_idx = 0 .. max_waves - 1):

  # 1. Frontier truncation (diversity-aware)
  if len(frontier) > MAX_FRONTIER_SIZE:
      frontier = diverse_truncate(frontier, MAX_FRONTIER_SIZE)
      # Bucket by root branch (URI prefix), round-robin fill by score

  # 2. Batch query
  per_wave_limit = max(limit * 3, len(frontier) * MIN_CHILDREN_PER_DIR * 2, 30)
  parent_uris = [uri for uri, _ in frontier]

  results = storage.search(
      collection, query_vector, sparse_query_vector,
      filter = {parent_uri IN parent_uris} AND metadata_filter,
      limit = per_wave_limit
  )

  # 3. Group by parent + score propagation
  children_by_parent = group_by(results, key=parent_uri)
  frontier_scores = {uri: score for uri, score in frontier}

  for parent_uri, children in children_by_parent.items():
      parent_score = frontier_scores[parent_uri]
      for child in children:
          child["_final_score"] = alpha * child.get("_score", 0.0) + (1-alpha) * parent_score
          reward = child.get("reward_score", 0.0)
          if reward != 0 and rl_weight:
              child["_final_score"] += rl_weight * reward

  # 4. Compensation query (starved parents)
  starved = [uri for uri in parent_uris
             if len(children_by_parent.get(uri, [])) < MIN_CHILDREN_PER_DIR
             and uri not in visited_dirs]
  if starved:
      comp_results = storage.search(
          filter = {parent_uri IN starved} AND metadata_filter,
          limit = len(starved) * MIN_CHILDREN_PER_DIR
      )
      # If still starved after first compensation, do per-parent tiny queries
      merge into children_by_parent, apply score propagation
      still_starved = [uri for uri in starved
                       if len(children_by_parent.get(uri, [])) < MIN_CHILDREN_PER_DIR]
      for uri in still_starved:
          tiny = storage.search(
              filter = {parent_uri == uri} AND metadata_filter,
              limit = MIN_CHILDREN_PER_DIR
          )
          merge, apply score propagation

  # 5. Fair select
  selected = per_parent_fair_select(
      children_by_parent,
      min_quota = MIN_CHILDREN_PER_DIR,
      total_budget = per_wave_limit
  )

  # 6. Triage + cycle prevention
  next_frontier: Dict[str, float] = {}   # uri -> best score, deduped
  for child in selected:
      if not passes_threshold(child.get("_final_score", 0.0)):
          continue
      uri = child.get("uri", "")
      if uri in collected:
          if child["_final_score"] > collected[uri]["_final_score"]:
              collected[uri] = child     # keep higher score version
      else:
          collected[uri] = child
      if not child.get("is_leaf", False) and uri not in visited_dirs:
          old_score = next_frontier.get(uri, -1.0)
          if child["_final_score"] > old_score:
              next_frontier[uri] = child["_final_score"]

  visited_dirs.update(uri for uri, _ in frontier)

  # 7. Convergence check (per wave, not per pop)
  # Use heapq.nlargest to get top-k WITHOUT sorting collected (no index invalidation)
  import heapq
  top_k_items = heapq.nlargest(limit, collected.values(), key=lambda x: x["_final_score"])
  current_topk_uris = {c["uri"] for c in top_k_items}
  if current_topk_uris == prev_topk_uris and len(collected) >= limit:
      convergence_rounds += 1
      if convergence_rounds >= MAX_CONVERGENCE_ROUNDS:
          break
  else:
      convergence_rounds = 0
      prev_topk_uris = current_topk_uris

  frontier = [(uri, score) for uri, score in next_frontier.items()]
  if not frontier:
      break

# 8. Late Rerank
all_candidates = sorted(collected.values(), key=lambda x: x["_final_score"], reverse=True)
M = min(LATE_RERANK_CAP, limit * LATE_RERANK_FACTOR)
top_m = all_candidates[:M]

if rerank_client and should_rerank(top_m, score_key="_final_score"):
    docs = [c.get("abstract", "") for c in top_m]
    rerank_scores = rerank(query, docs)
    for c, rs in zip(top_m, rerank_scores):
        # Use propagated_score as retrieval score, no double RL
        c["_final_score"] = beta * rs + (1-beta) * c["_final_score"]

top_m.sort(key=lambda x: x.get("_final_score", 0.0), reverse=True)
return top_m[:limit]
```

## Parameters

| Parameter | Default | Configurable | Notes |
|---|---|---|---|
| `MAX_FRONTIER_SIZE` | 64 | Class constant | Prevents oversized IN clause |
| `MIN_CHILDREN_PER_DIR` | 2 | Class constant | Min guaranteed children per parent |
| `LATE_RERANK_FACTOR` | 5 | Class constant | Late rerank candidate multiplier |
| `LATE_RERANK_CAP` | 50 | Class constant | Late rerank candidate cap |
| `max_waves` | 8 | `__init__` param | Depth guard, adjustable per scenario |
| `use_frontier_batching` | True | `__init__` param | Feature flag for fallback |

## Fallback Mechanism

### Feature Flag

```python
class HierarchicalRetriever:
    def __init__(self, ..., use_frontier_batching: bool = True, max_waves: int = 8):
        self._use_frontier_batching = use_frontier_batching
        self._max_waves = max_waves
```

### Dispatch in `retrieve()`

```python
if self._use_frontier_batching:
    candidates = await self._frontier_search(...)
else:
    candidates = await self._recursive_search(...)
```

### Auto-degradation

```python
async def _frontier_search(self, ...) -> List[Dict[str, Any]]:
    try:
        return await self._frontier_search_impl(...)
    except Exception as e:
        logger.error("[FrontierSearch] Fallback to recursive: %s", e)
        return await self._recursive_search(...)
```

The old `_recursive_search` method is preserved untouched as the fallback path.

## Helper Methods

### `_should_rerank` (adapted)

Accepts `score_key` parameter to support both `_score` (old path) and `_final_score` (late rerank):

```python
def _should_rerank(self, results, score_key="_score") -> bool:
    if len(results) < 2:
        return False
    scores = sorted([r.get(score_key, 0.0) for r in results], reverse=True)
    gap = scores[0] - scores[1]
    return gap <= self._score_gap_threshold
```

### `_diverse_truncate`

Buckets frontier by root branch (URI prefix), sorts each bucket by score descending, then round-robin fills to `max_size`.

### `_per_parent_fair_select`

Each parent gets `min_quota` children first (sorted by score). Remaining budget is filled by global score competition across all leftover children.

### Collected dedup

`collected` is a `Dict[str, Dict]` mapping URI directly to the result dict. On duplicate URI, compares `_final_score` and keeps the higher version. This avoids the index-invalidation bug that would occur with a list + index mapping approach (sorting a list invalidates stored indices).

## Testing Strategy

### Infrastructure

- **Storage**: `QdrantStorageAdapter(path=tmpdir, embedding_dim=128)` - real embedded Qdrant
- **Embedder**: `MockEmbedder(dim=128)` - deterministic hash vectors, no API key
- **Retriever**: Direct instantiation with `TypedQuery.target_directories` explicitly set to test root URIs (no global config dependency)
- **Call counting**: `StorageSpy(QdrantStorageAdapter)` subclass that increments counters in `search()` then calls `super()`

```python
class StorageSpy(QdrantStorageAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.call_counts = {"search": 0}

    async def search(self, *args, **kwargs):
        self.call_counts["search"] += 1
        return await super().search(*args, **kwargs)
```

### Test Data: 3-Level Directory Tree

```
root/
├── dir_A/          (hot, 20 leaves)
│   ├── sub_A1/     (5 leaves)
│   └── sub_A2/     (5 leaves)
├── dir_B/          (cold, 2 leaves)
│   └── sub_B1/     (1 leaf)
└── dir_C/          (medium, 8 leaves)
```

All nodes upserted via `storage.upsert` with real vectors from MockEmbedder.

### Test Cases (`tests/test_frontier_search.py`)

| # | Test | Data | Validates |
|---|---|---|---|
| 1 | `test_frontier_single_wave` | Flat directory (1 level of leaves) | 1 wave completes, search calls <= 2 |
| 2 | `test_frontier_multi_wave_depth` | 3-level tree | wave count <= depth + 1, deep leaves reachable |
| 3 | `test_frontier_convergence_early_stop` | High-score leaves concentrated at level 1 | Early termination when top-k stabilizes |
| 4 | `test_frontier_max_waves_guard` | Chain directory (depth=12) | Stops at max_waves=8 |
| 5 | `test_frontier_no_infinite_loop` | Circular parent_uri references | visited_dirs prevents re-entry, returns normally |
| 6 | `test_frontier_empty_node` | Directory exists but has no children | Wave advances or terminates, no exception, no invalid query |
| 7 | `test_diverse_truncate_balances_branches` | 3 root branches x 40 subdirs | After truncation to 64, each branch >= 20 |
| 8 | `test_fair_select_protects_cold_parents` | dir_A: 20 children, dir_B: 2 children | dir_B children all present in selected |
| 9 | `test_starved_compensation_query` | per_wave_limit set small, dir_B squeezed out | After compensation, dir_B children appear |
| 10 | `test_collected_dedup_keeps_higher_score` | Same leaf reachable from two parent paths | collected contains only the higher-score version |
| 11 | `test_late_rerank_uses_propagated_score` | Inject fake rerank client (fixed scores) | final_score = beta * fixed + (1-beta) * propagated, no double RL |
| 12 | `test_should_rerank_score_key_param` | Constructed results list | `score_key="_final_score"` and `"_score"` return correct judgments |
| 13 | `test_fallback_on_frontier_error` | Storage subclass that raises on specific condition | Auto-degrades to old `_recursive_search`, returns valid results |
| 14 | `test_frontier_vs_recursive_overlap` | Same tree + same query | Top-K URI overlap >= 80%; log warning at 70%-80% |

### Acceptance Criteria

| Metric | Measurement | Target |
|---|---|---|
| `storage.search` call count | StorageSpy counter | 3-level x 5-fanout: from ~31 to <= 8 |
| `rerank` call count | Fake rerank client counter | From <= N to <= 1 |
| Recall@K overlap | `test_frontier_vs_recursive_overlap` | >= 80% |
| Wall clock time | `time.perf_counter()` comparison | `t_frontier < t_recursive` (no fixed target) |
| All 14 tests pass | `uv run python3 -m unittest tests.test_frontier_search -v` | 100% |

### Notes

- `test_late_rerank_uses_propagated_score`: Uses injected fake rerank client with fixed scores (not LLM fallback) for determinism.
- `test_fallback_on_frontier_error`: Depends on feature flag + old path being implemented first; skip-marked until then.
- `test_frontier_vs_recursive_overlap`: Soft threshold logging at 70%-80% to catch vector ordering jitter without false failures.
- Sparse vector compatibility: If MockEmbedder gains `sparse_vector` support, frontier search tests should verify hybrid search path.
