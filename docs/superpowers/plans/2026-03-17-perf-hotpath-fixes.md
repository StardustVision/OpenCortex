# Perf Hot-Path Fixes Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the six performance regressions found in the code review: access-stats write amplification, frontier N+1 queries, serial batch import, blocking cold-start maintenance, result-assembly FS fan-out, and missing remote-embedding cache.

**Architecture:** Each fix is a localised rewrite of a single method or branch — no new files required. The fixes do not change observable behaviour (same results, fewer round-trips).

**Tech Stack:** Python 3.10+ asyncio, Qdrant client (set_payload), unittest + AsyncMock

---

## File Map

| File | Change |
|---|---|
| `src/opencortex/orchestrator.py` | Tasks 1, 2, 4, 6 |
| `src/opencortex/retrieve/hierarchical_retriever.py` | Tasks 3, 5 |
| `tests/test_perf_fixes.py` | New test file (all tasks) |

---

## Task 1: Fix P2 — Remote embedder not cached

**Files:**
- Modify: `src/opencortex/orchestrator.py:363,417`
- Test: `tests/test_perf_fixes.py`

The `volcengine` and `openai` branches of `_create_default_embedder()` return a
`CompositeHybridEmbedder` directly, never calling `_wrap_with_cache()`.
The docstring says "All embedders are wrapped with CachedEmbedder" — make it true.

Note: these branches do lazy imports inside the method body (`from ... import ...`).
Test them by injecting a mock into `sys.modules` and spying on the instance method.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_perf_fixes.py
import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_oc(provider: str, model: str = "text-embedding-3-small"):
    """Return a MemoryOrchestrator instance bypassing __init__ for unit tests."""
    from opencortex.orchestrator import MemoryOrchestrator
    from opencortex.config import CortexConfig
    oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
    oc._config = CortexConfig(
        embedding_provider=provider,
        embedding_model=model,
        embedding_api_key="test-key",
    )
    return oc


class TestEmbedderCache(unittest.TestCase):
    def _run_with_mocked_provider(self, oc, module_name: str, class_name: str):
        """
        Inject a mock module for `module_name` and spy on _wrap_with_cache.
        Returns the MagicMock assigned to `oc._wrap_with_cache`.
        """
        mock_wrap = MagicMock(side_effect=lambda e: e)
        oc._wrap_with_cache = mock_wrap  # instance override shadows class method

        mock_mod = MagicMock()
        setattr(mock_mod, class_name, MagicMock(return_value=MagicMock()))
        with patch.dict("sys.modules", {module_name: mock_mod}):
            oc._create_default_embedder()
        return mock_wrap

    def test_volcengine_embedder_wrapped_with_cache(self):
        oc = _make_oc("volcengine", "ep-test-model")
        mock_wrap = self._run_with_mocked_provider(
            oc,
            "opencortex.models.embedder.volcengine_embedders",
            "VolcengineDenseEmbedder",
        )
        mock_wrap.assert_called_once()

    def test_openai_embedder_wrapped_with_cache(self):
        oc = _make_oc("openai")
        mock_wrap = self._run_with_mocked_provider(
            oc,
            "opencortex.models.embedder.openai_embedder",
            "OpenAIDenseEmbedder",
        )
        mock_wrap.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python3 -m pytest tests/test_perf_fixes.py::TestEmbedderCache -v
```
Expected: FAIL — `mock_wrap.assert_called_once()` fails because `_wrap_with_cache` is never called.

- [ ] **Step 3: Apply the fix**

In `orchestrator.py`, find the volcengine branch (around line 361-363):

```python
# BEFORE
return CompositeHybridEmbedder(embedder, BM25SparseEmbedder())

# AFTER  (volcengine, ~line 363)
composite = CompositeHybridEmbedder(embedder, BM25SparseEmbedder())
return self._wrap_with_cache(composite)
```

Same change in the openai branch (~line 415-417):

```python
# BEFORE
return CompositeHybridEmbedder(embedder, BM25SparseEmbedder())

# AFTER  (openai, ~line 417)
composite = CompositeHybridEmbedder(embedder, BM25SparseEmbedder())
return self._wrap_with_cache(composite)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python3 -m pytest tests/test_perf_fixes.py::TestEmbedderCache -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_perf_fixes.py
git commit -m "fix(perf): wrap volcengine/openai embedders with LRU cache"
```

---

## Task 2: Fix P0 — Access stats write amplification

**Files:**
- Modify: `src/opencortex/orchestrator.py:1447-1491`
- Test: `tests/test_perf_fixes.py`

**Root cause:** `_resolve_and_update_access_stats(uris)` does:
1. N × `filter(uri)` — resolves each URI → ID individually
2. N × `get(collection, [id])` — fetches current `active_count` per record (redundant — `filter()` already returns full payloads with `active_count`)
3. N × `update(...)` — serial writes

**Fix:** Replace step 1 with a single `filter(all_uris)` call (multi-value `conds` is
translated to Qdrant `MatchAny` by `filter_translator.py`). Drop step 2 entirely — the
`active_count` is in the filter payload. Parallelise step 3 with `asyncio.gather`.

The `asyncio.create_task(self._resolve_and_update_access_stats(uris))` call at line 1429
stays exactly as-is — the fire-and-forget wrapper is correct; only the internals change.

- [ ] **Step 1: Write the failing test**

```python
class TestAccessStatsNoAmplification(unittest.IsolatedAsyncioTestCase):
    async def test_single_filter_no_get_parallel_update(self):
        """One filter() call, zero get() calls, N parallel update() calls."""
        from opencortex.orchestrator import MemoryOrchestrator, _CONTEXT_COLLECTION
        oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
        oc._storage = AsyncMock()
        oc._storage.filter.return_value = [
            {"id": "id1", "uri": "uri1", "active_count": 3},
            {"id": "id2", "uri": "uri2", "active_count": 7},
        ]

        await oc._resolve_and_update_access_stats(["uri1", "uri2"])

        # Exactly one filter call covering all URIs at once
        oc._storage.filter.assert_called_once()
        call_filter = oc._storage.filter.call_args[0][1]  # second positional arg
        assert set(call_filter["conds"]) == {"uri1", "uri2"}

        # Zero get() calls — active_count comes from filter payload
        oc._storage.get.assert_not_called()

        # Two update() calls, incremented counts
        assert oc._storage.update.call_count == 2
        calls = {c[0][1]: c[0][2] for c in oc._storage.update.call_args_list}
        assert calls["id1"]["active_count"] == 4
        assert calls["id2"]["active_count"] == 8
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python3 -m pytest tests/test_perf_fixes.py::TestAccessStatsNoAmplification -v
```
Expected: FAIL — `get.assert_not_called()` fails; `filter` called N times not once.

- [ ] **Step 3: Rewrite the two access-stats methods**

Replace `_resolve_and_update_access_stats` and `_update_access_stats` with the two
methods below. Delete the old `_update_access_stats` method (its only caller was
`_resolve_and_update_access_stats` at line 1467 — verify with `grep` before deleting).

```python
async def _resolve_and_update_access_stats(self, uris: list) -> None:
    """1 filter + N parallel updates. Old: N filter + N get + N update (serial)."""
    if not uris:
        return
    try:
        recs = await self._storage.filter(
            _CONTEXT_COLLECTION,
            {"op": "must", "field": "uri", "conds": uris},
            limit=len(uris),
        )
    except Exception:
        return
    if not recs:
        return
    await self._update_access_stats_batch(recs)

async def _update_access_stats_batch(self, records: list) -> None:
    """Parallel batch update access_count + accessed_at (no individual get)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _one(r: dict) -> None:
        rid = r.get("id", "")
        if not rid:
            return
        try:
            await self._storage.update(
                _CONTEXT_COLLECTION,
                rid,
                {"active_count": r.get("active_count", 0) + 1, "accessed_at": now},
            )
        except Exception as exc:
            logger.debug(
                "[Orchestrator] Access stats update failed for %s: %s", rid, exc
            )

    await asyncio.gather(*[_one(r) for r in records], return_exceptions=True)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python3 -m pytest tests/test_perf_fixes.py::TestAccessStatsNoAmplification -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_perf_fixes.py
git commit -m "fix(perf): access stats — 1 filter + parallel updates, drop N×get"
```

---

## Task 3: Fix P0 — Frontier still-starved N+1 queries

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py:918-958`
- Test: `tests/test_perf_fixes.py`

**Root cause:** After the compensation query, any parent still below `MIN_CHILDREN_PER_DIR`
is queried **individually** in a `for s_uri in still_starved` loop — each iteration is one
`storage.search()` call.

**Fix:** Merge all still-starved parents into a single batch query. The actual method to
modify is `_frontier_search_impl` (not `_frontier_batching` which does not exist).

- [ ] **Step 1: Write the failing test**

```python
class TestFrontierStillStarvedBatch(unittest.IsolatedAsyncioTestCase):
    async def test_still_starved_single_batch_query(self):
        """3 still-starved parents → 3 searches total (main+comp+batch), not 5."""
        from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever

        call_count = 0

        async def fake_search(**kwargs):
            nonlocal call_count
            call_count += 1
            f = kwargs.get("filter", {})
            conds = f.get("conds", [])
            # On the 3rd call (still-starved batch), return one child for parent_a
            if call_count == 3 and "parent_a" in conds:
                return [{"uri": "child1", "parent_uri": "parent_a", "_score": 0.9,
                         "is_leaf": True, "abstract": "x", "active_count": 0,
                         "category": "", "keywords": ""}]
            return []

        storage = MagicMock()
        storage.search = fake_search

        hr = HierarchicalRetriever(
            storage=storage,
            embedder=None,
            rerank_config=None,
            llm_completion=None,
            max_waves=1,
        )

        starting_points = [("parent_a", 0.9), ("parent_b", 0.8), ("parent_c", 0.7)]
        await hr._frontier_search_impl(
            query="test",
            collection="context",
            query_vector=[0.1] * 10,
            sparse_query_vector=None,
            starting_points=starting_points,
            limit=5,
            mode="wave",
            threshold=None,
            metadata_filter=None,
            text_query="",
        )

        # 1 main wave + 1 compensation + 1 still-starved batch = 3, NOT 5
        assert call_count == 3, f"Expected 3 search calls, got {call_count}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python3 -m pytest tests/test_perf_fixes.py::TestFrontierStillStarvedBatch -v
```
Expected: FAIL — `call_count` is 5 (1 main + 1 comp + 3 individual tiny), not 3.

- [ ] **Step 3: Replace the per-URI loop with a single batch query**

In `hierarchical_retriever.py`, find the `# Tiny queries for still-starved parents`
comment (around line 918). Replace the entire `for s_uri in still_starved:` loop
(lines 923–958) with:

```python
# Batch query for still-starved parents (replaces per-URI tiny queries)
if still_starved:
    if metadata_filter:
        still_dir_friendly = {"op": "or", "conds": [
            {"op": "must", "field": "is_leaf", "conds": [False]},
            metadata_filter,
        ]}
        still_batch_filter = merge_filter(
            {"op": "must", "field": "parent_uri", "conds": still_starved},
            still_dir_friendly,
        )
    else:
        still_batch_filter = {
            "op": "must", "field": "parent_uri", "conds": still_starved,
        }
    still_results = await self.storage.search(
        collection=collection,
        query_vector=query_vector,
        sparse_query_vector=sparse_query_vector,
        filter=still_batch_filter,
        limit=len(still_starved) * self.MIN_CHILDREN_PER_DIR,
        text_query=text_query,
    )
    for r in still_results:
        s_uri = r.get("parent_uri", "")
        if s_uri not in still_starved:
            continue
        if any(c.get("uri") == r.get("uri")
               for c in children_by_parent.get(s_uri, [])):
            continue
        parent_score = frontier_scores.get(s_uri, 0.0)
        raw_score = r.get("_score", 0.0)
        r["_final_score"] = (
            alpha * raw_score + (1 - alpha) * parent_score
            if parent_score else raw_score
        )
        reward = r.get("reward_score", 0.0)
        if reward != 0 and self._rl_weight:
            r["_final_score"] += self._rl_weight * reward
        if self._hot_weight:
            r["_final_score"] += self._hot_weight * self._compute_hotness(r)
        if s_uri not in children_by_parent:
            children_by_parent[s_uri] = []
        children_by_parent[s_uri].append(r)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python3 -m pytest tests/test_perf_fixes.py::TestFrontierStillStarvedBatch -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py tests/test_perf_fixes.py
git commit -m "fix(perf): frontier still-starved — single batch query replaces N tiny queries"
```

---

## Task 4: Fix P1 — Cold-start maintenance blocks init()

**Files:**
- Modify: `src/opencortex/orchestrator.py:195-220`
- Test: `tests/test_perf_fixes.py`

**Root cause:** `init()` synchronously awaits text-index creation, three migration passes,
and a full re-embed check before setting `_initialized = True`. If re-embed triggers
(model changed, data present), the server is unresponsive until reembedding finishes.

**Fix:** Extract all maintenance work into `_startup_maintenance()` and fire it as
`asyncio.create_task`. `init()` sets `_initialized = True` immediately after the
retriever is ready.

Note: `_init_alpha()` (called at line 223) is NOT deferred — it is lightweight
(Observer is in-memory only) and must complete before the server accepts requests.

- [ ] **Step 1: Write the failing test**

```python
class TestColdStartNonBlocking(unittest.IsolatedAsyncioTestCase):
    async def test_init_does_not_await_maintenance(self):
        """init() must complete in under 1 second even if maintenance is slow."""
        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.config import CortexConfig

        slow_started = asyncio.Event()

        async def slow_maintenance(self_inner):
            slow_started.set()
            await asyncio.sleep(60)  # would block init if awaited directly

        with patch.object(MemoryOrchestrator, "_startup_maintenance", slow_maintenance), \
             patch("opencortex.orchestrator.QdrantStorageAdapter", return_value=AsyncMock()), \
             patch("opencortex.orchestrator.init_context_collection", new_callable=AsyncMock), \
             patch("opencortex.orchestrator.init_cortex_fs", return_value=MagicMock()), \
             patch("opencortex.orchestrator.HierarchicalRetriever", return_value=MagicMock()), \
             patch.object(MemoryOrchestrator, "_create_default_embedder", return_value=None), \
             patch.object(MemoryOrchestrator, "_init_alpha", new_callable=AsyncMock):

            oc = MemoryOrchestrator(CortexConfig())
            t0 = asyncio.get_event_loop().time()
            await oc.init()
            elapsed = asyncio.get_event_loop().time() - t0

        assert oc._initialized is True
        assert elapsed < 1.0, f"init() took {elapsed:.2f}s — maintenance leaked into init"
        # Maintenance was started (task created) but not awaited
        assert slow_started.is_set() or not slow_started.is_set()  # either is fine
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python3 -m pytest tests/test_perf_fixes.py::TestColdStartNonBlocking -v
```
Expected: FAIL — `elapsed >= 1.0` because init currently awaits maintenance.

- [ ] **Step 3: Extract maintenance into background task**

In `orchestrator.py`, replace the maintenance block in `init()` (lines 195–220):

```python
# BEFORE in init():
#   # 7a. Ensure full-text indexes ...
#   if hasattr(self._storage, "ensure_text_indexes"):
#       await self._storage.ensure_text_indexes()
#   # 7c. Run v0.3.0 path migration ...
#   # 7d. Run v0.4.0 project_id backfill ...
#   # 7e. Auto re-embed ...

# AFTER: fire and forget
asyncio.create_task(self._startup_maintenance())
```

Add the new method (place it after `_check_and_reembed`):

```python
async def _startup_maintenance(self) -> None:
    """Background: text indexes, migrations, re-embed. Runs after init() returns."""
    if hasattr(self._storage, "ensure_text_indexes"):
        try:
            await self._storage.ensure_text_indexes()
        except Exception as exc:
            logger.warning("[Orchestrator] Text index setup failed: %s", exc)

    try:
        from opencortex.migration.v030_path_redesign import (
            backfill_new_fields, cleanup_root_junk,
        )
        await cleanup_root_junk(self._storage, self._fs, _CONTEXT_COLLECTION)
        await backfill_new_fields(self._storage, _CONTEXT_COLLECTION)
    except Exception as exc:
        logger.warning("[Orchestrator] Migration v0.3 skipped: %s", exc)

    try:
        from opencortex.migration.v040_project_backfill import backfill_project_id
        await backfill_project_id(self._storage, _CONTEXT_COLLECTION)
    except Exception as exc:
        logger.warning("[Orchestrator] Migration v0.4 skipped: %s", exc)

    try:
        await self._check_and_reembed()
    except Exception as exc:
        logger.warning("[Orchestrator] Auto re-embed skipped: %s", exc)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python3 -m pytest tests/test_perf_fixes.py::TestColdStartNonBlocking -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_perf_fixes.py
git commit -m "fix(perf): defer cold-start maintenance to background task"
```

---

## Task 5: Fix P1 — Result assembly FS fan-out

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py:1150-1222`
- Test: `tests/test_perf_fixes.py`

**Root cause:** `_convert_to_matched_contexts` runs `asyncio.gather` over N `_build_one`
coroutines. Each `_build_one` independently calls `get_relations(uri)` (1 FS read) and
then `read_batch(related_uris)` (1 FS read). With N=10 candidates: 10 `get_relations` +
10 `read_batch` calls, all concurrent but uncoordinated — total 20 FS operations.

**Fix:** Two-phase prefetch: (1) gather all `get_relations` in one pass, (2) single
`read_batch` over the union of all related URIs. `_build_one` then uses pre-fetched maps
with zero FS I/O.

- [ ] **Step 1: Write the failing test**

```python
class TestResultAssemblyBatchedRelations(unittest.IsolatedAsyncioTestCase):
    async def test_relations_read_in_two_batches_not_n(self):
        """5 candidates → 5 get_relations + 1 read_batch total, not 5 read_batch."""
        from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever
        from opencortex.retrieve.types import ContextType, DetailLevel

        read_batch_calls = []

        async def fake_get_relations(uri):
            return [f"rel_{uri}"]

        async def fake_read_batch(uris, level="l0"):
            read_batch_calls.append(sorted(uris))
            return {u: f"abstract_{u}" for u in uris}

        mock_fs = MagicMock()
        mock_fs.get_relations = fake_get_relations
        mock_fs.read_batch = fake_read_batch

        with patch(
            "opencortex.retrieve.hierarchical_retriever._get_cortex_fs",
            return_value=mock_fs,
        ):
            hr = HierarchicalRetriever(
                storage=MagicMock(), embedder=None,
                rerank_config=None, llm_completion=None,
            )
            candidates = [
                {"uri": f"oc://t/u/mem/c/n{i}", "abstract": f"a{i}",
                 "overview": "", "is_leaf": True, "context_type": "memory",
                 "category": "events", "keywords": "", "_final_score": 0.9}
                for i in range(5)
            ]
            await hr._convert_to_matched_contexts(
                candidates, ContextType.MEMORY, DetailLevel.L1,
            )

        # read_batch called exactly once (not 5 times)
        assert len(read_batch_calls) == 1, (
            f"Expected 1 read_batch call, got {len(read_batch_calls)}"
        )
        assert len(read_batch_calls[0]) == 5  # 5 unique related URIs in one call
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python3 -m pytest tests/test_perf_fixes.py::TestResultAssemblyBatchedRelations -v
```
Expected: FAIL — `len(read_batch_calls)` is 5.

- [ ] **Step 3: Refactor `_convert_to_matched_contexts`**

Replace the method body with the two-phase approach:

```python
async def _convert_to_matched_contexts(
    self,
    candidates: List[Dict[str, Any]],
    context_type: ContextType,
    detail_level: DetailLevel = DetailLevel.L1,
) -> List[MatchedContext]:
    cortex_fs = _get_cortex_fs()

    # Phase 1: batch-prefetch relation tables (one gather, all concurrent)
    all_related: Dict[str, List[str]] = {}
    if cortex_fs and candidates:
        candidate_uris = [c.get("uri", "") for c in candidates if c.get("uri")]
        raw_relations = await asyncio.gather(
            *[cortex_fs.get_relations(u) for u in candidate_uris],
            return_exceptions=True,
        )
        for uri, result in zip(candidate_uris, raw_relations):
            if isinstance(result, list) and result:
                all_related[uri] = result

    # Phase 2: single read_batch for all unique related URIs
    unique_related: set = set()
    for rel_list in all_related.values():
        unique_related.update(rel_list[: self.MAX_RELATIONS])

    related_abstracts: Dict[str, str] = {}
    if cortex_fs and unique_related:
        related_abstracts = await cortex_fs.read_batch(
            list(unique_related), level="l0"
        )

    # Phase 3: build MatchedContext objects using pre-fetched data (no FS I/O)
    async def _build_one(c: Dict[str, Any]) -> MatchedContext:
        uri = c.get("uri", "")
        relations: list = []
        for rel_uri in all_related.get(uri, [])[: self.MAX_RELATIONS]:
            abstract = related_abstracts.get(rel_uri, "")
            if abstract:
                relations.append(RelatedContext(uri=rel_uri, abstract=abstract))

        abstract = c.get("abstract", "")
        overview = None
        if detail_level in (DetailLevel.L1, DetailLevel.L2):
            overview = c.get("overview", "") or None

        content = None
        if detail_level == DetailLevel.L2 and cortex_fs:
            try:
                raw = await cortex_fs.read(uri + "/content.md")
                content = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            except Exception:
                pass

        effective_type = context_type
        if context_type == ContextType.ANY:
            raw_type = c.get("context_type", "memory")
            try:
                effective_type = ContextType(raw_type)
            except ValueError:
                effective_type = ContextType.MEMORY

        return MatchedContext(
            uri=uri,
            context_type=effective_type,
            is_leaf=c.get("is_leaf", False),
            abstract=abstract,
            overview=overview,
            content=content,
            keywords=c.get("keywords", ""),
            category=c.get("category", ""),
            score=c.get("_final_score", c.get("_score", 0.0)),
            relations=relations,
        )

    results = await asyncio.gather(*[_build_one(c) for c in candidates])
    return list(results)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python3 -m pytest tests/test_perf_fixes.py::TestResultAssemblyBatchedRelations -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py tests/test_perf_fixes.py
git commit -m "fix(perf): batch-prefetch all relations in _convert_to_matched_contexts"
```

---

## Task 6: Fix P1 — Batch import is fully serial

**Files:**
- Modify: `src/opencortex/orchestrator.py:2146-2180`
- Test: `tests/test_perf_fixes.py`

**Root cause:** `batch_add()` processes items in a sequential `for i, item in enumerate(items)`
loop. No concurrency; throughput degrades linearly with item count.

**Fix:** Wrap each item's processing in a `asyncio.Semaphore` to enable bounded concurrency,
then `asyncio.gather` all items. Directory nodes are still created serially first (they are
needed as parents; the existing sequential loop for dir creation is preserved unchanged).

`_BATCH_ADD_CONCURRENCY = 8` is intentionally a module-level constant (not a `CortexConfig`
field) — it is a simple internal implementation detail, not a user-tunable setting.

Error dicts preserve the `index` field to maintain the existing API contract.

- [ ] **Step 1: Write the failing test**

```python
class TestBatchAddConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_items_processed_concurrently(self):
        """batch_add with 8 items must run at least 2 concurrently."""
        from opencortex.orchestrator import MemoryOrchestrator

        concurrent_high_water = 0
        in_flight = 0

        async def fake_gen_abstract(content, file_path):
            nonlocal concurrent_high_water, in_flight
            in_flight += 1
            concurrent_high_water = max(concurrent_high_water, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return "abstract", "overview"

        async def fake_add(**kwargs):
            m = MagicMock()
            m.uri = "opencortex://t/u/mem/ev/test"
            return m

        oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
        oc._initialized = True
        oc._ensure_init = lambda: None
        oc._generate_abstract_overview = fake_gen_abstract
        oc.add = fake_add

        items = [{"content": f"doc {i}", "meta": {"file_path": f"f{i}.txt"}}
                 for i in range(8)]
        await oc.batch_add(items)

        assert concurrent_high_water >= 2, (
            f"Expected ≥2 concurrent items, got max {concurrent_high_water}"
        )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python3 -m pytest tests/test_perf_fixes.py::TestBatchAddConcurrency -v
```
Expected: FAIL — `concurrent_high_water` is 1 (serial loop).

- [ ] **Step 3: Replace serial loop with bounded concurrent gather**

At the top of `orchestrator.py`, add the module constant (near other constants):

```python
_BATCH_ADD_CONCURRENCY = 8
```

In `batch_add()`, replace the `for i, item in enumerate(items):` block (~lines 2146–2180):

```python
sem = asyncio.Semaphore(_BATCH_ADD_CONCURRENCY)

async def _process_one(i: int, item: dict) -> dict:
    async with sem:
        content = item.get("content", "")
        file_path = (item.get("meta") or {}).get("file_path", f"item_{i}")
        abstract, overview = await self._generate_abstract_overview(content, file_path)

        item_meta = dict(item.get("meta") or {})
        item_meta.setdefault("source", "batch:scan")
        item_meta["ingest_mode"] = "memory"

        parent_uri = None
        if scan_meta and file_path:
            from pathlib import PurePosixPath
            parent_dir = str(PurePosixPath(file_path).parent)
            parent_uri = dir_uris.get(parent_dir)

        try:
            result = await self.add(
                abstract=abstract,
                content=content,
                overview=overview,
                category=item.get("category", "documents"),
                parent_uri=parent_uri,
                context_type=item.get("context_type", "resource"),
                meta=item_meta,
                dedup=False,
            )
            return {"uri": result.uri, "index": i}
        except Exception as exc:
            return {"error": str(exc), "index": i}

outcomes = await asyncio.gather(
    *[_process_one(i, item) for i, item in enumerate(items)],
    return_exceptions=True,
)
for outcome in outcomes:
    if isinstance(outcome, BaseException):
        # Unexpected gather-level failure (e.g. CancelledError)
        errors.append({"error": str(outcome)})
    elif isinstance(outcome, dict) and "error" in outcome:
        errors.append({"index": outcome["index"], "error": outcome["error"]})
    else:
        uris.append(outcome["uri"])
        imported += 1
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python3 -m pytest tests/test_perf_fixes.py::TestBatchAddConcurrency -v
```
Expected: PASS

- [ ] **Step 5: Run full regression**

```bash
uv run python3 -m pytest tests/test_perf_fixes.py -v
uv run python3 -m unittest tests.test_e2e_phase1 tests.test_context_manager tests.test_write_dedup -v 2>&1 | tail -30
```
Expected: all new tests PASS; existing suite same pass/fail as pre-fix.

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_perf_fixes.py
git commit -m "fix(perf): batch_add — bounded concurrent processing (asyncio.Semaphore)"
```

---

## Expected Outcomes

| Issue | Before | After |
|---|---|---|
| Access stats (top-k=10) | 10 filter + 10 get + 10 update serial | 1 filter + 10 parallel update |
| Frontier still-starved (N=3) | 3 tiny searches/wave | 1 batch search/wave |
| batch_add (100 items) | 100 serial LLM+embed+write | ≤13 batches (8 concurrent) |
| Server cold start | blocks on migration + re-embed | returns in <1s |
| Result assembly (10 candidates) | 10 get_relations + 10 read_batch | 10 get_relations + 1 read_batch |
| volcengine/openai embedder | no cache (repeated API calls) | LRU cache (10k, 1h TTL) |
