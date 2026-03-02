# Memory Enhancement #1 Lexical Fallback + #2 Access-Driven Forgetting Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add lexical fallback search (when embedding is unavailable/timed out) and access-driven forgetting (recent access protects memories from decay).

**Architecture:** Extend `QdrantStorageAdapter.search()` with Qdrant full-text index fallback (`MatchText` on abstract/overview). Modify `apply_decay()` to factor in `accessed_at` timestamps. Add server-side embedding timeout via `asyncio.wait_for` + `run_in_executor`.

**Tech Stack:** Python 3.10+ async, Qdrant embedded, existing VikingDBInterface

**Design doc:** `docs/plans/2026-03-02-memory-enhancement-1-2-design.md`

---

### Task 1: Add `accessed_at` to collection schema

**Files:**
- Modify: `src/opencortex/storage/collection_schemas.py:40-75`

**Step 1: Add `accessed_at` field to context collection schema**

At `collection_schemas.py:48`, after the `updated_at` field, add:

```python
                {"FieldName": "accessed_at", "FieldType": "date_time"},
```

At `collection_schemas.py:68`, in the `ScalarIndex` list, after `"active_count"`, add:

```python
                "accessed_at",
```

**Step 2: Verify schema is correct**

Run: `uv run python3 -c "from opencortex.storage.collection_schemas import CollectionSchemas; s = CollectionSchemas.context_collection('test', 4); fields = [f['FieldName'] for f in s['Fields']]; assert 'accessed_at' in fields; assert 'accessed_at' in s['ScalarIndex']; print('OK')"`

Expected: `OK`

**Step 3: Commit**

```bash
git add src/opencortex/storage/collection_schemas.py
git commit -m "feat(schema): add accessed_at field to context collection"
```

---

### Task 2: Extend `Profile` with `accessed_at`

**Files:**
- Modify: `src/opencortex/storage/qdrant/rl_types.py:12-20`
- Modify: `src/opencortex/storage/qdrant/adapter.py:707-725`

**Step 1: Add `accessed_at` to Profile dataclass**

At `rl_types.py:20`, after `is_protected: bool = False`, add:

```python
    accessed_at: str = ""
```

**Step 2: Return `accessed_at` from `get_profile()`**

At `adapter.py:724`, after `is_protected=p.get("protected", False),`, add:

```python
            accessed_at=p.get("accessed_at", ""),
```

**Step 3: Verify import works**

Run: `uv run python3 -c "from opencortex.storage.qdrant.rl_types import Profile; p = Profile(accessed_at='2026-03-02T00:00:00Z'); print(p.accessed_at)"`

Expected: `2026-03-02T00:00:00Z`

**Step 4: Commit**

```bash
git add src/opencortex/storage/qdrant/rl_types.py src/opencortex/storage/qdrant/adapter.py
git commit -m "feat(rl): add accessed_at to Profile dataclass"
```

---

### Task 3: Add `text_query` parameter to search interface and implementations

**Files:**
- Modify: `src/opencortex/storage/vikingdb_interface.py:289-305`
- Modify: `src/opencortex/storage/qdrant/adapter.py:307-317`
- Modify: `tests/test_e2e_phase1.py:201-210` (InMemoryStorage.search)

**Step 1: Add `text_query` to abstract interface**

At `vikingdb_interface.py:299`, after `with_vector: bool = False,`, add:

```python
        text_query: str = "",
```

**Step 2: Add `text_query` to QdrantStorageAdapter.search()**

At `adapter.py:316`, after `with_vector: bool = False,`, add:

```python
        text_query: str = "",
```

**Step 3: Add `text_query` to InMemoryStorage.search()**

At `test_e2e_phase1.py`, in `InMemoryStorage.search()`, after the `with_vector` parameter, add:

```python
        text_query: str = "",
```

At the end of the method (before `return`), add simple text matching fallback:

```python
        # Text fallback when no vector scoring
        if not query_vector and text_query and candidates:
            query_lower = text_query.lower()
            scored = []
            for r in candidates:
                r = dict(r)
                abstract = (r.get("abstract") or "").lower()
                overview = (r.get("overview") or "").lower()
                score = 0.0
                if query_lower in abstract:
                    score += 0.8
                if query_lower in overview:
                    score += 0.4
                r["_score"] = score
                scored.append(r)
            scored.sort(key=lambda x: x["_score"], reverse=True)
            candidates = scored
```

**Step 4: Verify no import errors**

Run: `uv run python3 -c "from opencortex.storage.vikingdb_interface import VikingDBInterface; print('OK')"`

Expected: `OK`

**Step 5: Commit**

```bash
git add src/opencortex/storage/vikingdb_interface.py src/opencortex/storage/qdrant/adapter.py tests/test_e2e_phase1.py
git commit -m "feat(interface): add text_query parameter to search across all implementations"
```

---

### Task 4: Add `_tokenize_for_scoring` and `_compute_text_score` helpers

**Files:**
- Modify: `src/opencortex/storage/qdrant/adapter.py` (add at top of file, after imports)

**Step 1: Write the failing test**

Create `tests/test_text_scoring.py`:

```python
"""Tests for lexical text scoring helpers."""
import unittest


class TestTokenizeForScoring(unittest.TestCase):
    def test_english_words(self):
        from opencortex.storage.qdrant.adapter import _tokenize_for_scoring
        tokens = _tokenize_for_scoring("hello world")
        self.assertIn("hello", tokens)
        self.assertIn("world", tokens)

    def test_chinese_chars(self):
        from opencortex.storage.qdrant.adapter import _tokenize_for_scoring
        tokens = _tokenize_for_scoring("查询记忆")
        self.assertIn("查", tokens)
        self.assertIn("询", tokens)
        self.assertIn("记", tokens)
        self.assertIn("忆", tokens)

    def test_mixed_chinese_english(self):
        from opencortex.storage.qdrant.adapter import _tokenize_for_scoring
        tokens = _tokenize_for_scoring("python 开发指南")
        self.assertIn("python", tokens)
        self.assertIn("开", tokens)

    def test_error_codes_and_paths(self):
        from opencortex.storage.qdrant.adapter import _tokenize_for_scoring
        tokens = _tokenize_for_scoring("error-404 config.yaml")
        self.assertIn("error-404", tokens)
        self.assertIn("config.yaml", tokens)

    def test_empty_string(self):
        from opencortex.storage.qdrant.adapter import _tokenize_for_scoring
        tokens = _tokenize_for_scoring("")
        self.assertEqual(tokens, set())

    def test_none_input(self):
        from opencortex.storage.qdrant.adapter import _tokenize_for_scoring
        tokens = _tokenize_for_scoring(None)
        self.assertEqual(tokens, set())


class TestComputeTextScore(unittest.TestCase):
    def test_exact_match_abstract(self):
        from opencortex.storage.qdrant.adapter import _compute_text_score
        score = _compute_text_score("python", "python project setup", "")
        self.assertGreater(score, 0.0)

    def test_exact_match_overview(self):
        from opencortex.storage.qdrant.adapter import _compute_text_score
        score = _compute_text_score("python", "", "python project setup")
        self.assertGreater(score, 0.0)

    def test_abstract_weighted_higher(self):
        from opencortex.storage.qdrant.adapter import _compute_text_score
        score_abstract = _compute_text_score("python", "python", "")
        score_overview = _compute_text_score("python", "", "python")
        self.assertGreater(score_abstract, score_overview)

    def test_no_match(self):
        from opencortex.storage.qdrant.adapter import _compute_text_score
        score = _compute_text_score("python", "java setup", "ruby guide")
        self.assertEqual(score, 0.0)

    def test_chinese_query_matches(self):
        from opencortex.storage.qdrant.adapter import _compute_text_score
        score = _compute_text_score("记忆", "用户记忆存储", "")
        self.assertGreater(score, 0.0)

    def test_empty_query(self):
        from opencortex.storage.qdrant.adapter import _compute_text_score
        score = _compute_text_score("", "some text", "other text")
        self.assertEqual(score, 0.0)

    def test_score_capped_at_one(self):
        from opencortex.storage.qdrant.adapter import _compute_text_score
        score = _compute_text_score("a", "a a a a a", "a a a a a")
        self.assertLessEqual(score, 1.0)


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_text_scoring -v`

Expected: FAIL (ImportError — functions don't exist yet)

**Step 3: Implement the helpers**

At `adapter.py`, after the existing imports (around line 14), add:

```python
import re


def _tokenize_for_scoring(text: str) -> set:
    """Zero-dependency tokenizer for Chinese+English mixed text scoring."""
    text = (text or "").lower()
    # English words, paths, error codes (e.g. error-404, config.yaml)
    words = set(re.findall(r"[a-z0-9][a-z0-9_\-\.]*[a-z0-9]|[a-z0-9]", text))
    # Chinese characters (single char as unigram)
    chinese_chars = set(re.findall(r"[\u4e00-\u9fa5]", text))
    return words | chinese_chars


def _compute_text_score(query: str, abstract: str, overview: str) -> float:
    """Term-overlap scoring for lexical search results.

    Abstract matches are weighted 2x higher than overview matches.
    """
    query_terms = _tokenize_for_scoring(query)
    if not query_terms:
        return 0.0
    abstract_terms = _tokenize_for_scoring(abstract)
    overview_terms = _tokenize_for_scoring(overview)
    abstract_hits = len(query_terms & abstract_terms)
    overview_hits = len(query_terms & overview_terms)
    return min(1.0, (abstract_hits * 2 + overview_hits) / (len(query_terms) * 2))
```

**Step 4: Run test to verify it passes**

Run: `uv run python3 -m unittest tests.test_text_scoring -v`

Expected: All PASS

**Step 5: Commit**

```bash
git add src/opencortex/storage/qdrant/adapter.py tests/test_text_scoring.py
git commit -m "feat(lexical): add tokenizer and text scoring helpers for Chinese+English"
```

---

### Task 5: Add `ensure_text_indexes()` to QdrantStorageAdapter

**Files:**
- Modify: `src/opencortex/storage/qdrant/adapter.py` (add method after `_ensure_client`, around line 57)

**Step 1: Implement `ensure_text_indexes()`**

After the `_ensure_client()` method (after line 56), add:

```python
    async def ensure_text_indexes(self) -> None:
        """Ensure full-text indexes exist on abstract/overview fields.

        Safe to call on existing collections — Qdrant create_payload_index
        is idempotent (skips if index already exists).
        """
        client = await self._ensure_client()
        collections = await client.get_collections()
        existing = {c.name for c in collections.collections}

        for coll_name in existing:
            for field in ("abstract", "overview"):
                try:
                    await client.create_payload_index(
                        collection_name=coll_name,
                        field_name=field,
                        field_schema=models.TextIndexParams(
                            type=models.TextIndexType.TEXT,
                            tokenizer=models.TokenizerType.MULTILINGUAL,
                            min_token_len=2,
                            max_token_len=20,
                        ),
                    )
                except Exception as exc:
                    logger.debug(
                        "[QdrantAdapter] Text index %s.%s: %s",
                        coll_name, field, exc,
                    )
        logger.info("[QdrantAdapter] Text indexes ensured on %d collections", len(existing))
```

**Step 2: Verify it's importable**

Run: `uv run python3 -c "from opencortex.storage.qdrant.adapter import QdrantStorageAdapter; print(hasattr(QdrantStorageAdapter, 'ensure_text_indexes'))"`

Expected: `True`

**Step 3: Commit**

```bash
git add src/opencortex/storage/qdrant/adapter.py
git commit -m "feat(qdrant): add ensure_text_indexes for full-text index migration"
```

---

### Task 6: Implement text search fallback in `QdrantStorageAdapter.search()`

**Files:**
- Modify: `src/opencortex/storage/qdrant/adapter.py:376-387`

**Step 1: Write the failing test**

Add to `tests/test_text_scoring.py`:

```python
class TestAdapterTextFallback(unittest.TestCase):
    """Test QdrantStorageAdapter text search path using InMemoryStorage as proxy."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_text_query_no_vector_returns_results(self):
        """InMemoryStorage text fallback returns scored results."""
        import asyncio
        from tests.test_e2e_phase1 import InMemoryStorage

        storage = InMemoryStorage()
        self._run(storage.create_collection("test", {
            "CollectionName": "test",
            "Fields": [{"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True}],
        }))
        self._run(storage.insert("test", "r1", {
            "id": "r1", "abstract": "python setup guide", "overview": "",
        }))
        self._run(storage.insert("test", "r2", {
            "id": "r2", "abstract": "java tutorial", "overview": "",
        }))
        results = self._run(storage.search(
            "test", query_vector=None, text_query="python",
        ))
        self.assertTrue(len(results) > 0)
        self.assertEqual(results[0]["id"], "r1")
        self.assertGreater(results[0].get("_score", 0), 0)

    def test_text_query_with_vector_ignores_text(self):
        """When vector is present, text_query is ignored (fallback_only mode)."""
        import asyncio
        from tests.test_e2e_phase1 import InMemoryStorage

        storage = InMemoryStorage()
        self._run(storage.create_collection("test", {
            "CollectionName": "test",
            "Fields": [{"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True}],
        }))
        self._run(storage.insert("test", "r1", {
            "id": "r1", "abstract": "python setup", "vector": [1.0, 0.0, 0.0, 0.0],
        }))
        results = self._run(storage.search(
            "test", query_vector=[1.0, 0.0, 0.0, 0.0], text_query="python",
        ))
        # Should use vector scoring, not text
        self.assertTrue(len(results) > 0)
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_text_scoring.TestAdapterTextFallback -v`

Expected: FAIL or ERROR (InMemoryStorage doesn't have text fallback yet — wait, we added it in Task 3. Let me verify Task 3 implementation handles this correctly first.)

Actually these tests should pass since we already added text fallback to InMemoryStorage in Task 3. Run to verify:

Run: `uv run python3 -m unittest tests.test_text_scoring -v`

Expected: All PASS

**Step 3: Add text search fallback to QdrantStorageAdapter.search()**

At `adapter.py:376-385`, replace the current `else` block:

```python
        else:
            # Pure scalar filter — use scroll
            points_list, _ = await client.scroll(
                collection_name=collection,
                scroll_filter=qdrant_filter,
                limit=limit + offset,
                with_payload=True,
                with_vectors=with_vector,
            )
            points = points_list[offset:]
```

With:

```python
        else:
            if text_query:
                # Lexical fallback: MatchText on abstract OR overview
                text_conditions = [
                    models.FieldCondition(
                        key="abstract",
                        match=models.MatchText(text=text_query),
                    ),
                    models.FieldCondition(
                        key="overview",
                        match=models.MatchText(text=text_query),
                    ),
                ]
                combined_filter = models.Filter(
                    must=[qdrant_filter] if qdrant_filter else [],
                    should=text_conditions,
                )
                oversample = (limit + offset) * 3
                points_list, _ = await client.scroll(
                    collection_name=collection,
                    scroll_filter=combined_filter,
                    limit=oversample,
                    with_payload=True,
                    with_vectors=with_vector,
                )
                # Score and rank by text overlap
                for p in points_list:
                    payload = p.payload or {}
                    p.payload["_text_score"] = _compute_text_score(
                        text_query,
                        payload.get("abstract", ""),
                        payload.get("overview", ""),
                    )
                points_list.sort(
                    key=lambda p: (p.payload or {}).get("_text_score", 0),
                    reverse=True,
                )
                points = points_list[offset : offset + limit]
            else:
                # Pure scalar filter — use scroll
                points_list, _ = await client.scroll(
                    collection_name=collection,
                    scroll_filter=qdrant_filter,
                    limit=limit + offset,
                    with_payload=True,
                    with_vectors=with_vector,
                )
                points = points_list[offset:]
```

**Step 4: Verify module imports cleanly**

Run: `uv run python3 -c "from opencortex.storage.qdrant.adapter import QdrantStorageAdapter; print('OK')"`

Expected: `OK`

**Step 5: Commit**

```bash
git add src/opencortex/storage/qdrant/adapter.py tests/test_text_scoring.py
git commit -m "feat(lexical): implement text search fallback in QdrantStorageAdapter"
```

---

### Task 7: Add server-side embedding timeout

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py:63-116` (__init__) and `172-181` (embed call)

**Step 1: Write the failing test**

Add to `tests/test_text_scoring.py`:

```python
import asyncio


class TestEmbedTimeout(unittest.TestCase):
    def test_timeout_returns_none(self):
        """Embedding timeout returns None vectors."""
        from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever
        from opencortex.models.embedder.base import EmbedResult
        import time

        class SlowEmbedder:
            def embed(self, text):
                time.sleep(3.0)  # Simulate slow embedding
                return EmbedResult(dense_vector=[1.0, 0.0, 0.0, 0.0])

            def get_dimension(self):
                return 4

        retriever = HierarchicalRetriever(
            storage=None, embedder=SlowEmbedder(),
            embed_timeout=0.1,
        )
        result = asyncio.run(retriever._embed_with_timeout("test query"))
        self.assertIsNone(result)

    def test_fast_embed_returns_result(self):
        """Fast embedding returns normal result."""
        from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever
        from opencortex.models.embedder.base import EmbedResult

        class FastEmbedder:
            def embed(self, text):
                return EmbedResult(dense_vector=[1.0, 0.0, 0.0, 0.0])

            def get_dimension(self):
                return 4

        retriever = HierarchicalRetriever(
            storage=None, embedder=FastEmbedder(),
            embed_timeout=5.0,
        )
        result = asyncio.run(retriever._embed_with_timeout("test query"))
        self.assertIsNotNone(result)
        self.assertEqual(result.dense_vector, [1.0, 0.0, 0.0, 0.0])
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_text_scoring.TestEmbedTimeout -v`

Expected: FAIL (no `embed_timeout` param, no `_embed_with_timeout` method)

**Step 3: Add `embed_timeout` to __init__ and `_embed_with_timeout` method**

At `hierarchical_retriever.py:70`, after `max_waves: int = 8,`, add:

```python
        embed_timeout: float = 2.0,
```

At `hierarchical_retriever.py:90` (inside __init__), after `self._max_waves = max_waves`, add:

```python
        self._embed_timeout = embed_timeout
```

At the end of `__init__` (after line ~115), add the timeout method:

```python
    async def _embed_with_timeout(self, text: str) -> Optional[EmbedResult]:
        """Embed text with server-side timeout.

        Uses run_in_executor (embedder.embed is synchronous) +
        asyncio.wait_for for timeout control.  Returns None on timeout
        so caller can fall back to lexical search.
        """
        if not self.embedder:
            return None
        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self.embedder.embed, text),
                timeout=self._embed_timeout,
            )
            return result
        except asyncio.TimeoutError:
            logger.warning(
                "[HierarchicalRetriever] Embedding timeout (%.1fs), "
                "falling back to lexical search",
                self._embed_timeout,
            )
            return None
        except Exception as exc:
            logger.warning(
                "[HierarchicalRetriever] Embedding error: %s, "
                "falling back to lexical search",
                exc,
            )
            return None
```

Add necessary import at top of file if not present:

```python
from opencortex.models.embedder.base import EmbedResult
```

**Step 4: Run test to verify it passes**

Run: `uv run python3 -m unittest tests.test_text_scoring.TestEmbedTimeout -v`

Expected: All PASS

**Step 5: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py tests/test_text_scoring.py
git commit -m "feat(retriever): add server-side embedding timeout with fallback"
```

---

### Task 8: Wire `text_query` through HierarchicalRetriever.retrieve()

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py:172-210` (retrieve embed section)

**Step 1: Replace current embed call with `_embed_with_timeout`**

At `hierarchical_retriever.py:172-181`, replace:

```python
        # Generate query vectors once to avoid duplicate embedding calls
        query_vector = None
        sparse_query_vector = None
        if self.embedder:
            loop = asyncio.get_event_loop()
            result: EmbedResult = await loop.run_in_executor(
                None, self.embedder.embed, query.query
            )
            query_vector = result.dense_vector
            sparse_query_vector = result.sparse_vector
```

With:

```python
        # Generate query vectors with timeout protection
        query_vector = None
        sparse_query_vector = None
        text_query = query.query  # Always available for lexical fallback
        if self.embedder:
            result = await self._embed_with_timeout(query.query)
            if result:
                query_vector = result.dense_vector
                sparse_query_vector = result.sparse_vector
            # If result is None (timeout/error), query_vector stays None
            # and text_query will trigger lexical fallback in adapter
```

**Step 2: Pass `text_query` to no-embedder fallback search**

At `hierarchical_retriever.py:192-196`, replace:

```python
            results = await self.storage.search(
                collection=collection,
                query_vector=None,
                filter=final_metadata_filter,
                limit=limit,
            )
```

With:

```python
            results = await self.storage.search(
                collection=collection,
                query_vector=None,
                filter=final_metadata_filter,
                limit=limit,
                text_query=text_query,
            )
```

**Step 3: Pass `text_query` to all `storage.search()` calls in `_recursive_search` and `_frontier_search_impl`**

Find every `await self.storage.search(` call in the file. Add `text_query=text_query` parameter to each one. The `text_query` variable needs to be passed through the method chain:

For `_global_vector_search()` (around line 280): add `text_query: str = ""` parameter, pass it through:

```python
    async def _global_vector_search(
        self, collection, query_vector, sparse_query_vector,
        global_filter, limit, text_query: str = "",
    ):
```

And in its body:

```python
        results = await self.storage.search(
            collection=collection,
            query_vector=query_vector,
            sparse_query_vector=sparse_query_vector,
            filter=global_filter,
            limit=limit,
            text_query=text_query,
        )
```

For `_recursive_search()`: add `text_query: str = ""` parameter, pass to every `self.storage.search()` call.

For `_frontier_search_impl()`: add `text_query: str = ""` parameter, pass to every `self.storage.search()` call (frontier wave, compensation, tiny compensation).

For the dispatch in `retrieve()` that calls these methods: pass `text_query=text_query`.

**Step 4: Run existing tests to verify no regressions**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 -v`

Expected: All PASS (default `text_query=""` is backward compatible)

**Step 5: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py
git commit -m "feat(retriever): wire text_query through all search paths with embed timeout"
```

---

### Task 9: Implement async access stats update in orchestrator

**Files:**
- Modify: `src/opencortex/orchestrator.py:691-804` (search method)

**Step 1: Write the failing test**

Add to `tests/test_text_scoring.py`:

```python
class TestAccessStatsUpdate(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_search_updates_access_count(self):
        """Search results have access_count incremented."""
        import asyncio
        from tests.test_e2e_phase1 import InMemoryStorage, MockEmbedder
        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.config import CortexConfig, init_config
        from opencortex.http.request_context import set_request_identity, reset_request_identity
        import tempfile, shutil

        temp_dir = tempfile.mkdtemp()
        config = CortexConfig(data_root=temp_dir, embedding_dimension=4)
        init_config(config)
        tokens = set_request_identity("testteam", "alice")

        try:
            storage = InMemoryStorage()
            embedder = MockEmbedder()
            orch = MemoryOrchestrator(config=config, storage=storage, embedder=embedder)
            self._run(orch.init())

            # Add a memory
            self._run(orch.add(abstract="python setup guide", content="guide content"))

            # Search to trigger access update
            result = self._run(orch.search("python"))

            # Wait for async task to complete
            import asyncio
            loop = asyncio.new_event_loop()
            loop.run_until_complete(asyncio.sleep(0.2))
            loop.close()

            # Check access_count was incremented
            if result.memories:
                uri = result.memories[0].uri
                records = self._run(storage.filter(
                    "context",
                    {"op": "must", "field": "uri", "conds": [uri]},
                    limit=1,
                ))
                if records:
                    self.assertGreaterEqual(records[0].get("active_count", 0), 1)
        finally:
            reset_request_identity(tokens)
            shutil.rmtree(temp_dir, ignore_errors=True)
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_text_scoring.TestAccessStatsUpdate -v`

Expected: FAIL (access_count not updated on search)

**Step 3: Add `_update_access_stats` to orchestrator**

At `orchestrator.py`, after the `search()` method (after line 803), add:

```python
    async def _update_access_stats(self, record_ids: list) -> None:
        """Async batch update access_count + accessed_at for retrieved records.

        Called as fire-and-forget task after search returns Top-K.
        Failures are logged but do not affect search results.
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for record_id in record_ids:
            try:
                records = await self._storage.get(_CONTEXT_COLLECTION, record_id)
                if records:
                    rec = records if isinstance(records, dict) else records
                    count = rec.get("active_count", 0)
                    await self._storage.update(
                        _CONTEXT_COLLECTION,
                        record_id,
                        {"active_count": count + 1, "accessed_at": now},
                    )
            except Exception as exc:
                logger.debug(
                    "[Orchestrator] Access stats update failed for %s: %s",
                    record_id, exc,
                )
```

At the end of `search()`, before `return result` (around line 802), add:

```python
        # Async update access stats for returned results (fire-and-forget)
        all_matched = result.memories + result.resources + result.skills
        if all_matched:
            record_ids = []
            for mc in all_matched:
                # Look up record id by URI
                try:
                    recs = await self._storage.filter(
                        _CONTEXT_COLLECTION,
                        {"op": "must", "field": "uri", "conds": [mc.uri]},
                        limit=1,
                    )
                    if recs:
                        record_ids.append(recs[0].get("id", ""))
                except Exception:
                    pass
            if record_ids:
                asyncio.create_task(self._update_access_stats(record_ids))
```

**Step 4: Run test to verify it passes**

Run: `uv run python3 -m unittest tests.test_text_scoring.TestAccessStatsUpdate -v`

Expected: PASS

**Step 5: Run full test suite for regressions**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 -v`

Expected: All PASS

**Step 6: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_text_scoring.py
git commit -m "feat(orchestrator): async access stats update on search results"
```

---

### Task 10: Implement access-driven decay in `apply_decay()`

**Files:**
- Modify: `src/opencortex/storage/qdrant/adapter.py:737-792`

**Step 1: Write the failing test**

Add to `tests/test_text_scoring.py`:

```python
class TestAccessDrivenDecay(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_recent_access_slower_decay(self):
        """Recently accessed memory decays slower than non-accessed."""
        from tests.test_e2e_phase1 import InMemoryStorage
        from datetime import datetime, timezone

        storage = InMemoryStorage()
        self._run(storage.create_collection("ctx", {
            "CollectionName": "ctx",
            "Fields": [{"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True}],
        }))

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Record with recent access
        self._run(storage.insert("ctx", "r1", {
            "id": "r1", "reward_score": 1.0, "accessed_at": now,
        }))
        # Record without access
        self._run(storage.insert("ctx", "r2", {
            "id": "r2", "reward_score": 1.0,
        }))

        self._run(storage.apply_decay())

        r1 = self._run(storage.get("ctx", "r1"))
        r2 = self._run(storage.get("ctx", "r2"))
        # Recently accessed should have higher remaining reward
        self.assertGreater(r1["reward_score"], r2["reward_score"])

    def test_old_access_normal_decay(self):
        """Memory accessed 90+ days ago decays at near-normal rate."""
        from tests.test_e2e_phase1 import InMemoryStorage
        from datetime import datetime, timezone, timedelta

        storage = InMemoryStorage()
        self._run(storage.create_collection("ctx", {
            "CollectionName": "ctx",
            "Fields": [{"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True}],
        }))

        old_date = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")

        self._run(storage.insert("ctx", "r1", {
            "id": "r1", "reward_score": 1.0, "accessed_at": old_date,
        }))
        self._run(storage.insert("ctx", "r2", {
            "id": "r2", "reward_score": 1.0,
        }))

        self._run(storage.apply_decay())

        r1 = self._run(storage.get("ctx", "r1"))
        r2 = self._run(storage.get("ctx", "r2"))
        # 90-day old access should give minimal bonus (<0.003)
        diff = abs(r1["reward_score"] - r2["reward_score"])
        self.assertLess(diff, 0.01)

    def test_never_accessed_base_rate(self):
        """Memory never accessed uses base decay rate."""
        from tests.test_e2e_phase1 import InMemoryStorage

        storage = InMemoryStorage()
        self._run(storage.create_collection("ctx", {
            "CollectionName": "ctx",
            "Fields": [{"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True}],
        }))
        self._run(storage.insert("ctx", "r1", {
            "id": "r1", "reward_score": 1.0,
        }))

        self._run(storage.apply_decay())

        r1 = self._run(storage.get("ctx", "r1"))
        # Should decay at base rate 0.95
        self.assertAlmostEqual(r1["reward_score"], 0.95, places=2)
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_text_scoring.TestAccessDrivenDecay -v`

Expected: FAIL (InMemoryStorage.apply_decay doesn't factor in accessed_at)

**Step 3: Update InMemoryStorage.apply_decay() with access time factor**

In `tests/test_e2e_phase1.py`, find the `apply_decay()` method in `InMemoryStorage` and update it to include access time factor:

```python
    async def apply_decay(self, decay_rate=0.95, protected_rate=0.99, threshold=0.01):
        import math
        from datetime import datetime, timezone

        processed = decayed = below = 0
        now = datetime.now(timezone.utc)
        for col_records in self._records.values():
            for record in col_records.values():
                processed += 1
                reward = record.get("reward_score", 0.0)
                if reward == 0.0:
                    continue
                is_protected = record.get("protected", False)
                rate = protected_rate if is_protected else decay_rate
                # Access-driven protection
                accessed_at = record.get("accessed_at")
                if accessed_at:
                    try:
                        accessed_dt = datetime.fromisoformat(
                            accessed_at.replace("Z", "+00:00")
                        )
                        days_since = max(0, (now - accessed_dt).days)
                        access_bonus = 0.04 * math.exp(-days_since / 30)
                        rate = min(1.0, rate + access_bonus)
                    except (ValueError, TypeError):
                        pass
                new_reward = reward * rate
                if abs(new_reward) < threshold:
                    new_reward = 0.0
                    below += 1
                record["reward_score"] = new_reward
                decayed += 1

        class _R:
            def __init__(self):
                self.records_processed = processed
                self.records_decayed = decayed
                self.records_below_threshold = below
                self.records_archived = 0

        return _R()
```

**Step 4: Update QdrantStorageAdapter.apply_decay() with access time factor**

At `adapter.py:763-768`, replace:

```python
                    is_protected = record.get("protected", False)
                    rate = protected_rate if is_protected else decay_rate
                    new_reward = reward * rate
```

With:

```python
                    is_protected = record.get("protected", False)
                    rate = protected_rate if is_protected else decay_rate
                    # Access-driven protection: recent access → slower decay
                    accessed_at = record.get("accessed_at")
                    if accessed_at:
                        try:
                            from datetime import datetime, timezone
                            accessed_dt = datetime.fromisoformat(
                                accessed_at.replace("Z", "+00:00")
                            )
                            days_since = max(0, (now - accessed_dt).days)
                            access_bonus = 0.04 * math.exp(-days_since / 30)
                            rate = min(1.0, rate + access_bonus)
                        except (ValueError, TypeError):
                            pass
                    new_reward = reward * rate
```

Add `import math` at the top of the file if not already present. Also add `now = datetime.now(timezone.utc)` before the collection loop (around line 753, after `result = DecayResult()`):

```python
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
```

**Step 5: Run test to verify it passes**

Run: `uv run python3 -m unittest tests.test_text_scoring.TestAccessDrivenDecay -v`

Expected: All PASS

**Step 6: Run full test suite for regressions**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 tests.test_ace_phase1 tests.test_ace_phase2 tests.test_rule_extractor tests.test_skill_search_fusion -v`

Expected: All PASS

**Step 7: Commit**

```bash
git add src/opencortex/storage/qdrant/adapter.py tests/test_e2e_phase1.py tests/test_text_scoring.py
git commit -m "feat(decay): implement access-driven forgetting in apply_decay"
```

---

### Task 11: Wire `ensure_text_indexes()` into orchestrator init

**Files:**
- Modify: `src/opencortex/orchestrator.py:158-163` (_init method)

**Step 1: Add ensure_text_indexes call after collection init**

At `orchestrator.py`, after the skillbook collection init (find `init_skillbook_collection` call), add:

```python
        # Ensure full-text indexes on existing collections (idempotent)
        if hasattr(self._storage, "ensure_text_indexes"):
            await self._storage.ensure_text_indexes()
```

**Step 2: Run full test suite**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 -v`

Expected: All PASS (InMemoryStorage doesn't have `ensure_text_indexes`, so `hasattr` check skips it)

**Step 3: Commit**

```bash
git add src/opencortex/orchestrator.py
git commit -m "feat(init): wire ensure_text_indexes into orchestrator startup"
```

---

### Task 12: Final integration test and regression

**Step 1: Run all new tests**

Run: `uv run python3 -m unittest tests.test_text_scoring -v`

Expected: All PASS

**Step 2: Run full Python test suite**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 tests.test_ace_phase1 tests.test_ace_phase2 tests.test_rule_extractor tests.test_skill_search_fusion -v`

Expected: All PASS

**Step 3: Run integration tests if Qdrant available**

Run: `uv run python3 -m unittest tests.test_integration_skill_pipeline -v`

Expected: All PASS (or skip if Qdrant not configured)

**Step 4: Final commit with version bump**

```bash
git add -A
git commit -m "test: verify memory enhancement #1 lexical fallback + #2 access-driven forgetting"
```
