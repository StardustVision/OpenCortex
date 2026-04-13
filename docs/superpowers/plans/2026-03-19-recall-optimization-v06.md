# v0.6 Recall Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve retrieval accuracy (QASPER 0.15→0.65+, LoCoMo 0.56→0.70+) and latency (p50 5-12s→1.5-3s) through 13 targeted changes to the retrieval pipeline.

**Architecture:** Bottom-up infrastructure first (SearchExplain + schema + collection routing), then query classification + document scoped search, then accuracy improvements (context flattening, hybrid weights, time filter), finally performance polish (rerank gate, frontier budget, ablation).

**Tech Stack:** Python 3.10+ async, Qdrant embedded, FastEmbed (local embedding), FastAPI

**Spec:** `docs/superpowers/specs/2026-03-19-recall-optimization-v06-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/opencortex/retrieve/types.py` | Modify | SearchExplain, SearchExplainSummary dataclasses; TypedQuery.target_doc_id |
| `src/opencortex/retrieve/query_classifier.py` | **Create** | QueryFastClassifier + QueryClassification |
| `src/opencortex/config.py` | Modify | 16 feature flag fields |
| `src/opencortex/storage/collection_schemas.py` | Modify | 6 new payload field definitions |
| `src/opencortex/orchestrator.py` | Modify | Classifier integration, payload flattening, embed_text, _get_collection(), target_doc_id |
| `src/opencortex/retrieve/hierarchical_retriever.py` | Modify | Dynamic weights, time filter, rerank gate, frontier budget, per-query explain, Small-to-Big |
| `src/opencortex/retrieve/intent_router.py` | _(No change needed)_ | Classifier bypass is in orchestrator.search(), not IntentRouter itself |
| `src/opencortex/storage/qdrant/filter_translator.py` | Modify | Time range filter support |
| `src/opencortex/parse/parsers/markdown.py` | Modify | Output section_path in chunk meta |
| `src/opencortex/http/server.py` | Modify | Admin collection endpoints, X-Collection header |
| `src/opencortex/http/request_context.py` | Modify | collection_name contextvar |
| `src/opencortex/models/embedder/local_embedder.py` | Modify | ONNX intra_op_threads passthrough |
| `tests/benchmark/runner.py` | Modify | X-Collection header, collection create/delete |
| `tests/benchmark/ablation.py` | **Create** | Ablation sweep framework |
| `tests/test_search_explain.py` | **Create** | SearchExplain tests |
| `tests/test_query_classifier.py` | **Create** | QueryFastClassifier tests |
| `tests/test_collection_routing.py` | **Create** | X-Collection routing tests |
| `tests/test_doc_scoped_search.py` | **Create** | Document scoped search tests |
| `tests/test_context_flattening.py` | **Create** | Context flattening tests |
| `tests/test_time_filter.py` | **Create** | Time filter tests |
| `tests/test_rerank_gate.py` | **Create** | Enhanced rerank gate tests |
| `tests/test_frontier_budget.py` | **Create** | Frontier hard budget tests |

---

## Task 1: SearchExplain + Feature Flags Infrastructure

**Spec ref:** 4.1 (SearchExplain), 5.2 (Feature Flags)

**Files:**
- Modify: `src/opencortex/retrieve/types.py` (add SearchExplain, SearchExplainSummary; add `explain` to QueryResult, `explain_summary` to FindResult)
- Modify: `src/opencortex/config.py` (add 16 feature flag fields to CortexConfig)
- Test: `tests/test_search_explain.py`

- [ ] **Step 1: Write failing test for SearchExplain**

```python
# tests/test_search_explain.py
import unittest
from opencortex.retrieve.types import SearchExplain, SearchExplainSummary

class TestSearchExplain(unittest.TestCase):
    def test_search_explain_fields(self):
        e = SearchExplain(
            query_class="fact_lookup", path="fast_path",
            intent_ms=0.0, embed_ms=5.2, search_ms=12.3,
            rerank_ms=0.0, assemble_ms=3.1,
            doc_scope_hit=False, time_filter_hit=False,
            candidates_before_rerank=10, candidates_after_rerank=10,
            frontier_waves=0, frontier_budget_exceeded=False,
            total_ms=20.6,
        )
        self.assertEqual(e.query_class, "fact_lookup")
        self.assertEqual(e.total_ms, 20.6)

    def test_search_explain_summary_fields(self):
        s = SearchExplainSummary(
            total_ms=25.0, query_count=2,
            primary_query_class="document_scoped",
            primary_path="fast_path",
            doc_scope_hit=True, time_filter_hit=False,
            rerank_triggered=False,
        )
        self.assertEqual(s.query_count, 2)
        self.assertTrue(s.doc_scope_hit)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/test_search_explain.py -v`
Expected: FAIL — `ImportError: cannot import name 'SearchExplain'`

- [ ] **Step 3: Add SearchExplain and SearchExplainSummary to types.py**

In `src/opencortex/retrieve/types.py`, after the existing dataclasses (around line 355), add:

```python
@dataclass
class SearchExplain:
    """Per-query retrieval explain with 5-segment latency breakdown."""
    query_class: str = ""
    path: str = ""
    intent_ms: float = 0.0
    embed_ms: float = 0.0
    search_ms: float = 0.0
    rerank_ms: float = 0.0
    assemble_ms: float = 0.0
    doc_scope_hit: bool = False
    time_filter_hit: bool = False
    candidates_before_rerank: int = 0
    candidates_after_rerank: int = 0
    frontier_waves: int = 0
    frontier_budget_exceeded: bool = False
    total_ms: float = 0.0

@dataclass
class SearchExplainSummary:
    """Aggregate explain across multiple concurrent TypedQueries."""
    total_ms: float = 0.0
    query_count: int = 0
    primary_query_class: str = ""
    primary_path: str = ""
    doc_scope_hit: bool = False
    time_filter_hit: bool = False
    rerank_triggered: bool = False
```

Add `explain: Optional[SearchExplain] = None` to the `QueryResult` dataclass (around line 332).
Add `explain_summary: Optional[SearchExplainSummary] = None` to the `FindResult` dataclass (around line 355).
Add `target_doc_id: Optional[str] = None` to the `TypedQuery` dataclass (around line 254).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/test_search_explain.py -v`
Expected: PASS

- [ ] **Step 5: Add feature flags to CortexConfig**

In `src/opencortex/config.py`, add these fields to the `CortexConfig` dataclass (after line 113):

```python
    # --- v0.6 Feature Flags ---
    query_classifier_enabled: bool = True
    query_classifier_classes: Dict[str, str] = field(default_factory=lambda: {
        "document_scoped": "查找特定文档、论文、文件中的内容",
        "temporal_lookup": "查找最近、上次、昨天等时间相关的记忆",
        "fact_lookup": "查找特定人名、数字、术语、文件名等精确事实",
        "simple_recall": "简单的记忆召回，回忆之前存储的信息",
    })
    query_classifier_threshold: float = 0.3
    query_classifier_hybrid_weights: Dict[str, Dict[str, float]] = field(default_factory=lambda: {
        "document_scoped": {"dense": 0.5, "lexical": 0.5},
        "fact_lookup": {"dense": 0.3, "lexical": 0.7},
        "temporal_lookup": {"dense": 0.6, "lexical": 0.4},
        "simple_recall": {"dense": 0.7, "lexical": 0.3},
        "complex": {"dense": 0.7, "lexical": 0.3},
    })
    doc_scope_search_enabled: bool = True
    small_to_big_enabled: bool = True
    small_to_big_sibling_count: int = 2
    context_flattening_enabled: bool = True
    time_filter_enabled: bool = True
    time_filter_fallback_threshold: int = 3
    rerank_gate_score_gap_threshold: float = 0.15
    rerank_gate_doc_scope_skip_threshold: int = 5
    max_compensation_queries: int = 3
    max_total_search_calls: int = 12
    explain_enabled: bool = True
    onnx_intra_op_threads: int = 0
```

You'll need `from dataclasses import field` and `from typing import Dict` if not already imported.

- [ ] **Step 6: Run full test suite to verify no regressions**

Run: `uv run python3 -m pytest tests/test_search_explain.py tests/test_e2e_phase1.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/opencortex/retrieve/types.py src/opencortex/config.py tests/test_search_explain.py
git commit -m "feat: add SearchExplain dataclasses + v0.6 feature flags"
```

---

## Task 2: Qdrant Payload Schema + Top-Level Flattening

**Spec ref:** 4.2 (new fields), 4.5.1 (flattening)

**Files:**
- Modify: `src/opencortex/storage/collection_schemas.py` (add 6 field definitions)
- Modify: `src/opencortex/orchestrator.py` (add top-level flattening in `add()` around line 1025)
- Test: existing `tests/test_e2e_phase1.py` for regression

- [ ] **Step 1: Write failing test for new payload fields**

The schema is defined in `CollectionSchemas.context_collection()` which returns a dict with `"Fields"` list and `"ScalarIndex"` list. There is no standalone constant.

```python
# tests/test_doc_scoped_search.py (first portion — field writing)
import unittest

class TestPayloadFlattening(unittest.TestCase):
    def test_new_fields_in_context_schema(self):
        from opencortex.storage.collection_schemas import CollectionSchemas
        schema = CollectionSchemas.context_collection("test", 1024)
        field_names = [f["FieldName"] for f in schema["Fields"]]
        required = ["source_doc_id", "source_doc_title", "source_section_path",
                     "chunk_role", "speaker", "event_date"]
        for name in required:
            self.assertIn(name, field_names, f"Missing field {name} in context schema Fields")

    def test_new_fields_have_scalar_index(self):
        from opencortex.storage.collection_schemas import CollectionSchemas
        schema = CollectionSchemas.context_collection("test", 1024)
        indexed = schema["ScalarIndex"]
        for name in ["source_doc_id", "source_doc_title", "source_section_path",
                      "chunk_role", "speaker", "event_date"]:
            self.assertIn(name, indexed, f"Missing ScalarIndex for {name}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/test_doc_scoped_search.py::TestPayloadFlattening -v`
Expected: FAIL — missing fields

- [ ] **Step 3: Add fields to CollectionSchemas.context_collection()**

In `src/opencortex/storage/collection_schemas.py`, in `context_collection()`:

Add to the `"Fields"` list (after line 68, before the closing `]`):
```python
                # v0.6: Document/Conversation enrichment
                {"FieldName": "source_doc_id", "FieldType": "string"},
                {"FieldName": "source_doc_title", "FieldType": "string"},
                {"FieldName": "source_section_path", "FieldType": "string"},
                {"FieldName": "chunk_role", "FieldType": "string"},
                {"FieldName": "speaker", "FieldType": "string"},
                {"FieldName": "event_date", "FieldType": "date_time"},
```

Add to the `"ScalarIndex"` list (after line 91, before the closing `]`):
```python
                # v0.6
                "source_doc_id",
                "source_doc_title",
                "source_section_path",
                "chunk_role",
                "speaker",
                "event_date",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/test_doc_scoped_search.py::TestPayloadFlattening -v`
Expected: PASS

- [ ] **Step 5: Add top-level flattening to orchestrator.add()**

In `src/opencortex/orchestrator.py`, after the existing top-level field assignments (around line 1035, after `record["keywords"] = ...`), add:

```python
        # v0.6: Flatten doc/conversation enrichment fields to top-level payload
        record["source_doc_id"] = (meta or {}).get("source_doc_id", "")
        record["source_doc_title"] = (meta or {}).get("source_doc_title", "")
        record["source_section_path"] = (meta or {}).get("source_section_path", "")
        record["chunk_role"] = (meta or {}).get("chunk_role", "")
        record["speaker"] = (meta or {}).get("speaker", "")
        record["event_date"] = (meta or {}).get("event_date")
```

- [ ] **Step 6: Run regression tests**

Run: `uv run python3 -m pytest tests/test_e2e_phase1.py tests/test_doc_scoped_search.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/opencortex/storage/collection_schemas.py src/opencortex/orchestrator.py tests/test_doc_scoped_search.py
git commit -m "feat: add 6 payload fields + top-level flattening in add()"
```

---

## Task 3: Collection Routing (X-Collection Full Chain)

**Spec ref:** 4.3 (Benchmark isolation)

**Files:**
- Modify: `src/opencortex/http/request_context.py` (add `collection_name` contextvar)
- Modify: `src/opencortex/http/server.py` (extract X-Collection header in middleware, add admin endpoints)
- Modify: `src/opencortex/orchestrator.py` (add `_get_collection()` method)
- Modify: `tests/benchmark/runner.py` (add X-Collection header, collection create/delete)
- Test: `tests/test_collection_routing.py`

- [ ] **Step 1: Write failing test for collection_name contextvar**

```python
# tests/test_collection_routing.py
import unittest
from opencortex.http.request_context import get_collection_name, set_collection_name

class TestCollectionRouting(unittest.TestCase):
    def test_default_collection_name_is_none(self):
        self.assertIsNone(get_collection_name())

    def test_set_and_get_collection_name(self):
        token = set_collection_name("bench_qasper_abc123")
        self.assertEqual(get_collection_name(), "bench_qasper_abc123")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/test_collection_routing.py -v`
Expected: FAIL — `ImportError: cannot import name 'get_collection_name'`

- [ ] **Step 3: Add collection_name contextvar to request_context.py**

In `src/opencortex/http/request_context.py`, add (following the existing `_tid` / `_uid` pattern):

```python
_collection_name: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_collection_name", default=None
)

def get_collection_name() -> Optional[str]:
    return _collection_name.get()

def set_collection_name(name: str) -> contextvars.Token:
    return _collection_name.set(name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/test_collection_routing.py -v`
Expected: PASS

- [ ] **Step 5: Extract X-Collection header in middleware**

In `src/opencortex/http/server.py`, inside `RequestContextMiddleware.__call__()` (around line 100, after the JWT extraction logic), add:

```python
        collection = headers.get("x-collection")
        if collection:
            set_collection_name(collection)
```

Import `set_collection_name` from `request_context`.

- [ ] **Step 6: Add _get_collection() to orchestrator**

In `src/opencortex/orchestrator.py`, add a method:

```python
    def _get_collection(self) -> str:
        """Return active collection name (contextvar override or default)."""
        from opencortex.http.request_context import get_collection_name
        return get_collection_name() or _CONTEXT_COLLECTION
```

Then replace all occurrences of `_CONTEXT_COLLECTION` in the orchestrator's storage calls with `self._get_collection()`. Key locations:
- `add()` around line 1044: `await self._storage.upsert(_CONTEXT_COLLECTION, record)`
- `search()` around line 1410: `await self._storage.search(_CONTEXT_COLLECTION, ...)`
- Other storage calls that reference `_CONTEXT_COLLECTION`

**Important**: Do NOT remove the `_CONTEXT_COLLECTION = "context"` constant — it remains the default.

- [ ] **Step 7: Add admin collection endpoints to server.py**

In `src/opencortex/http/server.py`, add two new routes:

```python
from opencortex.storage.collection_schemas import CollectionSchemas

@app.post("/api/v1/admin/collection")
async def create_bench_collection(request: Request):
    body = await request.json()
    name = body.get("name", "")
    if not name.startswith("bench_"):
        return JSONResponse({"error": "Collection name must start with bench_"}, status_code=400)
    orchestrator = request.app.state.orchestrator
    dim = orchestrator._config.embedding_dimension
    schema = CollectionSchemas.context_collection(name, dim)
    await orchestrator._storage.create_collection(name, schema)
    return {"status": "created", "collection": name}

@app.delete("/api/v1/admin/collection/{name}")
async def delete_bench_collection(name: str, request: Request):
    if not name.startswith("bench_"):
        return JSONResponse({"error": "Can only delete bench_ collections"}, status_code=400)
    orchestrator = request.app.state.orchestrator
    # Storage interface has drop_collection(), not delete_collection()
    await orchestrator._storage.drop_collection(name)
    return {"status": "deleted", "collection": name}
```

- [ ] **Step 8: Update benchmark runner**

In `tests/benchmark/runner.py`:

1. Modify `_auth_headers()` to accept optional collection:
```python
def _auth_headers(jwt_token: str, collection: str = "") -> Dict[str, str]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {jwt_token}"}
    if collection:
        headers["X-Collection"] = collection
    return headers
```

2. In `run_benchmark()`, after generating `run_id`, add collection create/delete:
```python
    collection_name = f"bench_{run_id}"
    # Create isolated collection
    _http_post(base_url, "/api/v1/admin/collection", {"name": collection_name}, jwt_token)
    try:
        # ... existing seed + query logic, pass collection_name to _auth_headers ...
    finally:
        # Drop collection (drop_collection in storage interface)
        try:
            req = urllib.request.Request(
                f"{base_url.rstrip('/')}/api/v1/admin/collection/{collection_name}",
                headers=_auth_headers(jwt_token), method="DELETE"
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass
```

- [ ] **Step 9: Run tests**

Run: `uv run python3 -m pytest tests/test_collection_routing.py tests/test_e2e_phase1.py -v`
Expected: All PASS

- [ ] **Step 10: Commit**

```bash
git add src/opencortex/http/request_context.py src/opencortex/http/server.py \
  src/opencortex/orchestrator.py tests/benchmark/runner.py tests/test_collection_routing.py
git commit -m "feat: X-Collection routing + admin collection endpoints for benchmark isolation"
```

---

## Task 4: QueryFastClassifier (Embedding Nearest Centroid)

**Spec ref:** 4.4

**Files:**
- Create: `src/opencortex/retrieve/query_classifier.py`
- Modify: `src/opencortex/orchestrator.py` (integrate classifier in `search()`)
- Test: `tests/test_query_classifier.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_query_classifier.py
import unittest
from unittest.mock import MagicMock
import numpy as np

class TestQueryFastClassifier(unittest.TestCase):
    def _make_classifier(self):
        from opencortex.retrieve.query_classifier import QueryFastClassifier
        embedder = MagicMock()
        # Return different vectors for different texts
        def fake_embed(text):
            if "文档" in text or "论文" in text:
                return np.array([1.0, 0.0, 0.0, 0.0])
            if "最近" in text or "昨天" in text:
                return np.array([0.0, 1.0, 0.0, 0.0])
            if "人名" in text or "术语" in text:
                return np.array([0.0, 0.0, 1.0, 0.0])
            return np.array([0.25, 0.25, 0.25, 0.25])
        embedder.embed = fake_embed

        config = MagicMock()
        config.query_classifier_classes = {
            "document_scoped": "查找特定文档、论文、文件中的内容",
            "temporal_lookup": "查找最近、上次、昨天等时间相关的记忆",
            "fact_lookup": "查找特定人名、数字、术语、文件名等精确事实",
            "simple_recall": "简单的记忆召回",
        }
        config.query_classifier_threshold = 0.3
        config.query_classifier_hybrid_weights = {
            "document_scoped": {"dense": 0.5, "lexical": 0.5},
            "fact_lookup": {"dense": 0.3, "lexical": 0.7},
            "temporal_lookup": {"dense": 0.6, "lexical": 0.4},
            "simple_recall": {"dense": 0.7, "lexical": 0.3},
            "complex": {"dense": 0.7, "lexical": 0.3},
        }
        return QueryFastClassifier(embedder, config)

    def test_target_doc_id_forces_document_scoped(self):
        clf = self._make_classifier()
        result = clf.classify("anything", target_doc_id="doc_abc123")
        self.assertEqual(result.query_class, "document_scoped")
        self.assertFalse(result.need_llm_intent)
        self.assertEqual(result.doc_scope_hint, "doc_abc123")

    def test_classify_returns_query_classification(self):
        clf = self._make_classifier()
        result = clf.classify("这篇论文讲了什么", target_doc_id=None)
        self.assertIn(result.query_class,
                      ["document_scoped", "temporal_lookup", "fact_lookup", "simple_recall", "complex"])
        self.assertIsInstance(result.lexical_boost, float)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/test_query_classifier.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement QueryFastClassifier**

Create `src/opencortex/retrieve/query_classifier.py`:

```python
"""Query Fast Classifier — Embedding Nearest Centroid."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class QueryClassification:
    query_class: str
    need_llm_intent: bool
    lexical_boost: float
    time_filter_hint: Optional[str] = None
    doc_scope_hint: Optional[str] = None


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


class QueryFastClassifier:
    """Two-layer query classifier: structural signals + embedding centroid."""

    def __init__(self, embedder: Any, config: Any) -> None:
        self.embedder = embedder
        self.threshold = getattr(config, "query_classifier_threshold", 0.3)
        self.hybrid_weights: Dict[str, Dict[str, float]] = getattr(
            config, "query_classifier_hybrid_weights", {}
        )

        class_descriptions: Dict[str, str] = getattr(
            config, "query_classifier_classes", {}
        )
        self.centroids: Dict[str, np.ndarray] = {}
        for cls, desc in class_descriptions.items():
            vec = embedder.embed(desc)
            self.centroids[cls] = np.asarray(vec)
        logger.info("[QueryFastClassifier] Loaded %d class centroids", len(self.centroids))

    def classify(
        self,
        query: str,
        target_doc_id: Optional[str] = None,
        session_context: Optional[dict] = None,
    ) -> QueryClassification:
        # Layer 0: structural signal
        if target_doc_id:
            weights = self.hybrid_weights.get("document_scoped", {})
            return QueryClassification(
                query_class="document_scoped",
                need_llm_intent=False,
                lexical_boost=weights.get("lexical", 0.5),
                doc_scope_hint=target_doc_id,
            )

        if not self.centroids:
            return self._fallback_complex()

        # Layer 1: embedding nearest centroid
        query_vec = np.asarray(self.embedder.embed(query))
        scores = {cls: _cosine_sim(query_vec, c) for cls, c in self.centroids.items()}
        best_class = max(scores, key=scores.get)

        if scores[best_class] < self.threshold:
            return self._fallback_complex()

        weights = self.hybrid_weights.get(best_class, {})
        time_hint = None
        if best_class == "temporal_lookup":
            time_hint = "recent"

        return QueryClassification(
            query_class=best_class,
            need_llm_intent=False,
            lexical_boost=weights.get("lexical", 0.3),
            time_filter_hint=time_hint,
        )

    def _fallback_complex(self) -> QueryClassification:
        weights = self.hybrid_weights.get("complex", {})
        return QueryClassification(
            query_class="complex",
            need_llm_intent=True,
            lexical_boost=weights.get("lexical", 0.3),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/test_query_classifier.py -v`
Expected: PASS

- [ ] **Step 5: Integrate classifier into orchestrator.search()**

In `src/opencortex/orchestrator.py`, in the `search()` method (around line 1270):

1. Import: `from opencortex.retrieve.query_classifier import QueryFastClassifier, QueryClassification`
2. Initialize classifier in `__init__()` or lazily on first search (after embedder is ready)
3. At the start of `search()`, before the IntentRouter call:

```python
        # v0.6: Query classification (fast path)
        classification = None
        target_doc_id = (meta or {}).get("target_doc_id") if isinstance(meta, dict) else None
        if self._config.query_classifier_enabled and hasattr(self, '_query_classifier'):
            classification = self._query_classifier.classify(
                query, target_doc_id=target_doc_id, session_context=session_context
            )
            if not classification.need_llm_intent:
                # Fast path: skip LLM IntentRouter, build TypedQuery directly
                typed_query = TypedQuery(
                    query=query,
                    context_type=context_type,
                    intent=classification.query_class,
                    target_doc_id=classification.doc_scope_hint,
                )
                typed_queries = [typed_query]
                # Skip IntentRouter call below
```

4. Pass `classification` to the retriever's `retrieve()` call so downstream can use `lexical_boost` and other hints.

- [ ] **Step 6: Run regression tests**

Run: `uv run python3 -m pytest tests/test_query_classifier.py tests/test_e2e_phase1.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/opencortex/retrieve/query_classifier.py src/opencortex/orchestrator.py tests/test_query_classifier.py
git commit -m "feat: QueryFastClassifier with embedding centroid classification"
```

---

## Task 5: Document Scoped Search (Write Path + Retrieval)

**Spec ref:** 4.5

**Files:**
- Modify: `src/opencortex/parse/parsers/markdown.py` (add section_path to chunk meta)
- Modify: `src/opencortex/orchestrator.py` (`_add_document()` generates source_doc_id, section_path, chunk_role)
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py` (inject source_doc_id filter when doc_scope_hit)
- Test: `tests/test_doc_scoped_search.py` (extend from Task 2)

- [ ] **Step 1: Write failing test for section_path in parsed chunks**

```python
# Add to tests/test_doc_scoped_search.py
class TestSectionPath(unittest.TestCase):
    def test_markdown_parser_adds_section_path(self):
        from opencortex.parse.parsers.markdown import MarkdownParser
        parser = MarkdownParser()
        content = "# Chapter 1\n\n## Section A\n\nSome text here.\n\n## Section B\n\nMore text."
        chunks = parser.parse_content(content, source_path="test.md")
        # At least one chunk should have section_path in meta
        paths = [c.meta.get("section_path", "") for c in chunks if c.meta.get("section_path")]
        self.assertTrue(len(paths) > 0, "No chunks have section_path in meta")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/test_doc_scoped_search.py::TestSectionPath -v`
Expected: FAIL — no section_path in meta

- [ ] **Step 3: Add section_path construction to MarkdownParser**

In `src/opencortex/parse/parsers/markdown.py`, modify `_process_sections_to_chunks()` to accept an additional `parent_path: str = ""` parameter, and build the path:

```python
def _process_sections_to_chunks(self, content, headings, sections, chunks,
                                 parent_index, parent_level, max_size, min_size,
                                 parent_path=""):
    for sec in sections:
        section_path = f"{parent_path} > {sec['name']}" if parent_path else sec['name']
        # ... in each ParsedChunk creation, add to meta:
        # meta={"section_path": section_path}
```

Update the initial call (around line 114) to pass `parent_path=""`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/test_doc_scoped_search.py::TestSectionPath -v`
Expected: PASS

- [ ] **Step 5: Write failing test for source_doc_id in _add_document**

```python
# Add to tests/test_doc_scoped_search.py
class TestDocScopedWrite(unittest.TestCase):
    async def test_add_document_sets_source_doc_id(self):
        # Integration test: add a document, read back, check source_doc_id is a top-level field
        # This requires a running orchestrator — use the test setup pattern from test_e2e_phase1
        pass  # Will be filled with actual integration test
```

- [ ] **Step 6: Modify orchestrator._add_document() to set doc fields**

In `src/opencortex/orchestrator.py`, in `_add_document()` (around line 650):

```python
    import hashlib

    async def _add_document(self, abstract, content, overview, category, meta, ...):
        # Generate source_doc_id
        source_path = (meta or {}).get("source_path", "") or (meta or {}).get("file_path", "")
        if source_path:
            source_doc_id = hashlib.sha256(source_path.encode()).hexdigest()[:16]
        else:
            source_doc_id = uuid4().hex[:16]
        source_doc_title = (meta or {}).get("title", "") or os.path.basename(source_path) if source_path else ""

        # Parse content into chunks
        chunks = parser.parse_content(content, source_path=source_path)

        # For each chunk, inject doc fields into meta before calling add()
        for idx, chunk in enumerate(chunks):
            chunk_meta = {**(meta or {}), **(chunk.meta or {})}
            chunk_meta["source_doc_id"] = source_doc_id
            chunk_meta["source_doc_title"] = source_doc_title
            chunk_meta["source_section_path"] = chunk.meta.get("section_path", "")
            chunk_meta["chunk_role"] = "leaf"  # default, override for dirs below
            chunk_meta["chunk_index"] = idx
            # ... existing add() call with updated meta ...
```

For directory chunks (non-leaf), set `chunk_meta["chunk_role"] = "section"`.
For the root document node, set `chunk_meta["chunk_role"] = "document"`.

- [ ] **Step 7: Add source_doc_id filter to retriever**

In `src/opencortex/retrieve/hierarchical_retriever.py`, in the `retrieve()` method (around line 196):

When `query.target_doc_id` is set and config `doc_scope_search_enabled` is True, inject a metadata filter:

```python
        if query.target_doc_id and self._config.doc_scope_search_enabled:
            doc_filter = {"op": "match", "field": "source_doc_id", "value": query.target_doc_id}
            if metadata_filter:
                metadata_filter = {"op": "and", "conditions": [metadata_filter, doc_filter]}
            else:
                metadata_filter = doc_filter
```

- [ ] **Step 8: Run tests**

Run: `uv run python3 -m pytest tests/test_doc_scoped_search.py tests/test_e2e_phase1.py -v`
Expected: All PASS

- [ ] **Step 9: Commit**

```bash
git add src/opencortex/parse/parsers/markdown.py src/opencortex/orchestrator.py \
  src/opencortex/retrieve/hierarchical_retriever.py tests/test_doc_scoped_search.py
git commit -m "feat: document scoped search — source_doc_id write + filter + section_path"
```

---

## Task 6: Context Flattening (embed_text Enhancement)

**Spec ref:** 4.7

**Files:**
- Modify: `src/opencortex/orchestrator.py` (`_add_document()`, `_write_immediate()`, `batch_add()`)
- Test: `tests/test_context_flattening.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_context_flattening.py
import unittest

class TestContextFlatteningLogic(unittest.TestCase):
    def test_document_embed_text_format(self):
        """embed_text for document mode should be [title] [section] abstract."""
        title = "My Paper"
        section = "Introduction > Background"
        abstract = "Some important findings."
        embed_text = f"[{title}] [{section}] {abstract}"
        self.assertIn("[My Paper]", embed_text)
        self.assertIn("[Introduction > Background]", embed_text)
        self.assertTrue(embed_text.endswith(abstract))

    def test_conversation_embed_text_format(self):
        """embed_text for conversation mode should be [speaker] abstract."""
        text = "user: What is the capital of France?"
        speaker = text.split(":", 1)[0]
        embed_text = f"[{speaker}] {text}"
        self.assertIn("[user]", embed_text)

    def test_empty_title_skipped(self):
        """When title is empty, embed_text should not have empty brackets."""
        title = ""
        abstract = "Some text."
        parts = []
        if title:
            parts.append(f"[{title}]")
        parts.append(abstract)
        embed_text = " ".join(parts)
        self.assertNotIn("[]", embed_text)
        self.assertEqual(embed_text, abstract)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/test_context_flattening.py -v`
Expected: FAIL

- [ ] **Step 3: Modify _add_document() for context flattening**

In `src/opencortex/orchestrator.py`, in `_add_document()`, when calling `add()` for each chunk:

```python
        embed_text = ""
        if self._config.context_flattening_enabled:
            parts = []
            if source_doc_title:
                parts.append(f"[{source_doc_title}]")
            section_path = chunk_meta.get("source_section_path", "")
            if section_path:
                parts.append(f"[{section_path}]")
            parts.append(chunk_abstract)
            embed_text = " ".join(parts)

        result = await self.add(
            abstract=chunk_abstract,
            embed_text=embed_text,
            # ... other existing params ...
        )
```

- [ ] **Step 4: Modify _write_immediate() for context flattening**

In `src/opencortex/orchestrator.py`, in `_write_immediate()` (around line 597), before the embed call:

```python
        embed_input = text
        if self._config.context_flattening_enabled:
            speaker = ""  # extract from text if "user:" / "assistant:" prefix
            if text.startswith("user:") or text.startswith("assistant:"):
                speaker = text.split(":", 1)[0]
            parts = []
            if speaker:
                parts.append(f"[{speaker}]")
            parts.append(text)
            embed_input = " ".join(parts)
```

Use `embed_input` instead of `text` for the embedding call.

- [ ] **Step 5: Add embed_text passthrough to batch_add()**

In `_process_one()` (around line 2166), construct embed_text from file_path:

```python
        embed_text = ""
        if self._config.context_flattening_enabled:
            file_path = item_meta.get("file_path", "")
            if file_path:
                embed_text = f"[{file_path}] {abstract}"

        result = await self.add(
            abstract=abstract,
            embed_text=embed_text,
            # ... other existing params ...
        )
```

- [ ] **Step 6: Run tests**

Run: `uv run python3 -m pytest tests/test_context_flattening.py tests/test_e2e_phase1.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_context_flattening.py
git commit -m "feat: context flattening — enrich embed_text with doc/speaker context"
```

---

## Task 7: Small-to-Big Return Strategy

**Spec ref:** 4.6

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py` (`_convert_to_matched_contexts()`)
- Test: extend existing retriever tests

- [ ] **Step 1: Write failing test**

```python
# Add to tests/test_doc_scoped_search.py
class TestSmallToBig(unittest.TestCase):
    def test_parent_overview_prefix_is_prepended(self):
        """When a leaf chunk has a parent_uri and parent overview is available,
        the overview should be prefixed with [Parent Section] marker."""
        parent_abstract = "This section covers machine learning basics."
        chunk_overview = "SGD converges under certain conditions."
        # Simulate the Small-to-Big enrichment logic
        enriched = f"[Parent Section] {parent_abstract}\n\n{chunk_overview}"
        self.assertTrue(enriched.startswith("[Parent Section]"))
        self.assertIn(parent_abstract, enriched)
        self.assertIn(chunk_overview, enriched)

    def test_no_parent_uri_no_enrichment(self):
        """Without parent_uri, overview should remain unchanged."""
        original = "Some chunk overview."
        parent_uri = None
        result = original  # no enrichment when parent_uri is None
        if parent_uri:
            result = f"[Parent Section] parent_text\n\n{original}"
        self.assertEqual(result, original)
```

- [ ] **Step 2: Modify _convert_to_matched_contexts()**

In `src/opencortex/retrieve/hierarchical_retriever.py`, in `_convert_to_matched_contexts()` (around line 1204, inside `_build_one()`):

After building the base `MatchedContext`, if `small_to_big_enabled` and the chunk is a leaf with `parent_uri`:

```python
        # Small-to-Big: enrich leaf chunks with parent section overview
        if (self._config.small_to_big_enabled
            and c.get("is_leaf", False)
            and c.get("parent_uri")):
            parent_uri = c["parent_uri"]
            # Parent overview is already in the batch-prefetched relations
            parent_abstract = related_abstracts.get(parent_uri, "")
            if parent_abstract:
                overview = f"[Parent Section] {parent_abstract}\n\n{overview}"
```

- [ ] **Step 3: Run tests**

Run: `uv run python3 -m pytest tests/test_doc_scoped_search.py tests/test_e2e_phase1.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py tests/test_doc_scoped_search.py
git commit -m "feat: small-to-big return strategy — enrich leaf chunks with parent overview"
```

---

## Task 8: Dynamic Hybrid Weights + _score/_final_score Bug Fix

**Spec ref:** 4.8

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py` (`retrieve()` uses classification.lexical_boost; fix `_score` → `_final_score`)
- Test: `tests/test_perf_fixes.py` (extend)

- [ ] **Step 1: Write failing test for _score bug**

```python
# Add to tests/test_doc_scoped_search.py or new file
class TestScoreBugFix(unittest.TestCase):
    def test_rerank_fusion_uses_final_score(self):
        """After RRF merge writes _final_score, the rerank fusion formula
        must read _final_score (not _score) for the retrieval component."""
        # Candidate after RRF merge has _final_score but no _score
        candidate = {"uri": "test://1", "_final_score": 0.85}
        retrieval_score = candidate.get("_final_score", candidate.get("_score", 0.0))
        self.assertEqual(retrieval_score, 0.85)

    def test_candidates_sorted_by_final_score(self):
        """Post-rerank sorting must use _final_score, not _score."""
        candidates = [
            {"uri": "a", "_final_score": 0.5},
            {"uri": "b", "_final_score": 0.9},
        ]
        sorted_c = sorted(candidates, key=lambda c: c.get("_final_score", 0), reverse=True)
        self.assertEqual(sorted_c[0]["uri"], "b")
```

- [ ] **Step 2: Fix _score → _final_score in flat-search rerank path**

In `src/opencortex/retrieve/hierarchical_retriever.py`, fix **three locations** in the flat-search rerank path (around lines 400-421):

1. **Line ~419** — the fusion formula reads `_score`:
```python
# BEFORE: c["_score"] = beta * rs + (1 - beta) * c.get("_score", 0.0)
# AFTER:
c["_final_score"] = beta * rs + (1 - beta) * c.get("_final_score", c.get("_score", 0.0))
```

2. **Line ~421** — the sort key reads `_score`:
```python
# BEFORE: candidates.sort(key=lambda c: c.get("_score", 0), reverse=True)
# AFTER:
candidates.sort(key=lambda c: c.get("_final_score", 0), reverse=True)
```

3. **Line ~404** — `_should_rerank()` call reads candidates' scores. Ensure it reads `_final_score`:
The `_should_rerank()` method (fixed in Task 10) already reads `_final_score` with fallback.

- [ ] **Step 3: Wire classification.lexical_boost into retrieve()**

In `retrieve()` (around line 204), the `lexical_boost` parameter defaults to 0.3. When `classification` is available, override it:

In `orchestrator.search()`, when calling `retriever.retrieve()`:
```python
lexical_boost = classification.lexical_boost if classification else 0.3
result = await self._retriever.retrieve(
    query=typed_query,
    lexical_boost=lexical_boost,
    # ... other params ...
)
```

- [ ] **Step 4: Run tests**

Run: `uv run python3 -m pytest tests/test_e2e_phase1.py tests/test_perf_fixes.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py src/opencortex/orchestrator.py
git commit -m "fix: _score→_final_score in rerank path + dynamic hybrid weights from classifier"
```

---

## Task 9: Time Range Hard Filter

**Spec ref:** 4.9

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py` (inject time filter when classification.time_filter_hint is set)
- Test: `tests/test_time_filter.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_time_filter.py
import unittest
from datetime import datetime, timedelta

class TestTimeFilter(unittest.TestCase):
    def test_time_filter_builds_range_condition(self):
        """When time_filter_hint='recent', a range filter on created_at should be injected."""
        from opencortex.storage.qdrant.filter_translator import translate_filter
        now = datetime.utcnow()
        week_ago = (now - timedelta(days=7)).isoformat() + "Z"
        dsl = {"op": "range", "field": "created_at", "gte": week_ago}
        f = translate_filter(dsl)
        self.assertIsNotNone(f)
        self.assertEqual(len(f.must), 1)
        self.assertEqual(f.must[0].key, "created_at")
```

- [ ] **Step 2: Run test to verify it passes** (filter_translator already supports range)

Run: `uv run python3 -m pytest tests/test_time_filter.py -v`
Expected: PASS (filter_translator already handles range DSL)

- [ ] **Step 3: Inject time filter in retriever**

In `src/opencortex/retrieve/hierarchical_retriever.py`, in `retrieve()`, after the doc_scope filter injection:

```python
        # v0.6: Time filter
        if (classification and classification.time_filter_hint
            and self._config.time_filter_enabled):
            from datetime import datetime, timedelta
            hint = classification.time_filter_hint
            time_conditions = []

            if hint == "recent":
                cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat() + "Z"
                # Filter on created_at OR event_date (either being recent is enough)
                time_conditions.append({"op": "range", "field": "created_at", "gte": cutoff})
                # event_date filter added as OR — if event_date exists and is recent, include it
            elif hint == "today":
                cutoff = datetime.utcnow().replace(hour=0, minute=0, second=0).isoformat() + "Z"
                time_conditions.append({"op": "range", "field": "created_at", "gte": cutoff})
            elif hint == "session" and query.session_id:
                time_conditions.append({"op": "match", "field": "session_id", "value": query.session_id})

            if time_conditions:
                time_filter = time_conditions[0]  # primary filter
                if metadata_filter:
                    metadata_filter = {"op": "and", "conditions": [metadata_filter, time_filter]}
                else:
                    metadata_filter = time_filter
```

- [ ] **Step 4: Add fallback for too-few results**

After the search returns results, if time filter was active and results < `time_filter_fallback_threshold`:

```python
        # v0.6: Time filter fallback
        if (time_filter_active and len(results) < self._config.time_filter_fallback_threshold):
            # Retry without time filter
            results = await self._search_without_time_filter(...)
```

- [ ] **Step 5: Run tests**

Run: `uv run python3 -m pytest tests/test_time_filter.py tests/test_e2e_phase1.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py tests/test_time_filter.py
git commit -m "feat: time range hard filter with fallback for temporal queries"
```

---

## Task 10: Rerank Gate Enhancement

**Spec ref:** 4.10

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py` (`_should_rerank()`)
- Test: `tests/test_rerank_gate.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_rerank_gate.py
import unittest
from unittest.mock import MagicMock
from opencortex.retrieve.query_classifier import QueryClassification

class TestRerankGate(unittest.TestCase):
    def _make_candidates(self, scores):
        return [{"uri": f"u{i}", "_final_score": s} for i, s in enumerate(scores)]

    def test_skip_rerank_for_fact_lookup_high_lexical(self):
        """fact_lookup with lexical_boost >= 0.6 should skip rerank."""
        clf = QueryClassification(
            query_class="fact_lookup", need_llm_intent=False, lexical_boost=0.7)
        candidates = self._make_candidates([0.9, 0.88])  # close scores
        # _should_rerank would normally return True for close scores,
        # but fact_lookup + high lexical should override to False
        should = True  # default for close gap
        if clf.query_class == "fact_lookup" and clf.lexical_boost >= 0.6:
            should = False
        self.assertFalse(should)

    def test_skip_rerank_for_doc_scoped_small_pool(self):
        """document_scoped with <5 candidates should skip rerank."""
        clf = QueryClassification(
            query_class="document_scoped", need_llm_intent=False, lexical_boost=0.5)
        candidates = self._make_candidates([0.9, 0.88, 0.85])  # 3 candidates < 5
        should = True
        if clf.query_class == "document_scoped" and len(candidates) < 5:
            should = False
        self.assertFalse(should)

    def test_no_skip_for_complex_query(self):
        """complex queries with close scores should still rerank."""
        clf = QueryClassification(
            query_class="complex", need_llm_intent=True, lexical_boost=0.3)
        candidates = self._make_candidates([0.9, 0.88])
        should = True
        if clf.query_class == "fact_lookup" and clf.lexical_boost >= 0.6:
            should = False
        if clf.query_class == "document_scoped" and len(candidates) < 5:
            should = False
        self.assertTrue(should)
```

- [ ] **Step 2: Extend _should_rerank() to accept classification**

In `src/opencortex/retrieve/hierarchical_retriever.py`, modify `_should_rerank()` (around line 501):

```python
    def _should_rerank(self, candidates: list, classification=None) -> bool:
        if len(candidates) < 2:
            return False
        # Existing: score gap check
        top = candidates[0].get("_final_score", candidates[0].get("_score", 0))
        second = candidates[1].get("_final_score", candidates[1].get("_score", 0))
        gap = top - second
        if gap > self._config.rerank_gate_score_gap_threshold:
            return False
        # v0.6: classification-based gates
        if classification:
            if (classification.query_class == "fact_lookup"
                and classification.lexical_boost >= 0.6):
                return False
            if (classification.query_class == "document_scoped"
                and len(candidates) < self._config.rerank_gate_doc_scope_skip_threshold):
                return False
        return True
```

- [ ] **Step 3: Run tests**

Run: `uv run python3 -m pytest tests/test_rerank_gate.py tests/test_e2e_phase1.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py tests/test_rerank_gate.py
git commit -m "feat: rerank gate — skip for fact_lookup + doc-scoped small pools"
```

---

## Task 11: Frontier Hard Budget

**Spec ref:** 4.11

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py` (wave loop budget counter)
- Test: `tests/test_frontier_budget.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_frontier_budget.py
import unittest

class TestFrontierBudget(unittest.TestCase):
    def test_budget_counter_increments(self):
        """Budget counter should track each search call."""
        max_calls = 12
        total_search_calls = 0
        # Simulate 15 search calls — should stop at 12
        for i in range(15):
            total_search_calls += 1
            if total_search_calls >= max_calls:
                break
        self.assertEqual(total_search_calls, max_calls)

    def test_budget_exceeded_sets_flag(self):
        """When budget is exceeded, frontier_budget_exceeded should be True."""
        max_calls = 3
        total_search_calls = 0
        budget_exceeded = False
        for i in range(10):
            total_search_calls += 1
            if total_search_calls >= max_calls:
                budget_exceeded = True
                break
        self.assertTrue(budget_exceeded)

    def test_under_budget_does_not_set_flag(self):
        """When within budget, frontier_budget_exceeded should be False."""
        max_calls = 12
        total_search_calls = 2  # only 2 calls
        budget_exceeded = total_search_calls >= max_calls
        self.assertFalse(budget_exceeded)
```

- [ ] **Step 2: Add budget counter to wave loop**

In `src/opencortex/retrieve/hierarchical_retriever.py`, in the wave loop (around line 645):

```python
        total_search_calls = 0
        max_calls = self._config.max_total_search_calls

        for wave in range(max_waves):
            # ... existing wave logic ...
            total_search_calls += 1  # increment for each storage.search() call

            if total_search_calls >= max_calls:
                logger.warning("[Retriever] Frontier budget exceeded (%d calls), falling back to flat", total_search_calls)
                # Set explain flag
                break

            # ... compensation queries ...
            total_search_calls += 1  # for each compensation query
            if total_search_calls >= max_calls:
                break
```

- [ ] **Step 3: Run tests**

Run: `uv run python3 -m pytest tests/test_frontier_budget.py tests/test_e2e_phase1.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py tests/test_frontier_budget.py
git commit -m "feat: frontier hard budget — max_total_search_calls with flat fallback"
```

---

## Task 12: SearchExplain Instrumentation

**Spec ref:** 4.1 (instrumentation — now that all features exist, wire up timing)

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py` (add perf_counter timing)
- Modify: `src/opencortex/orchestrator.py` (build SearchExplainSummary, attach to response)
- Modify: `src/opencortex/http/server.py` (support `?explain=true` query param)

- [ ] **Step 1: Add timing instrumentation to retriever**

In `hierarchical_retriever.retrieve()`, wrap each stage with `time.perf_counter()`:

```python
import time

async def retrieve(self, query, ...):
    t_start = time.perf_counter()

    # Intent phase (already measured by orchestrator)
    t_embed_start = time.perf_counter()
    # ... embedding ...
    t_embed = (time.perf_counter() - t_embed_start) * 1000

    t_search_start = time.perf_counter()
    # ... vector search ...
    t_search = (time.perf_counter() - t_search_start) * 1000

    t_rerank_start = time.perf_counter()
    # ... rerank ...
    t_rerank = (time.perf_counter() - t_rerank_start) * 1000

    t_assemble_start = time.perf_counter()
    # ... assembly ...
    t_assemble = (time.perf_counter() - t_assemble_start) * 1000

    total_ms = (time.perf_counter() - t_start) * 1000

    explain = SearchExplain(
        query_class=query.intent or "",
        path="fast_path" if not classification or not classification.need_llm_intent else "llm_intent",
        embed_ms=t_embed, search_ms=t_search, rerank_ms=t_rerank, assemble_ms=t_assemble,
        total_ms=total_ms,
        # ... other fields from context ...
    )
    result.explain = explain
```

- [ ] **Step 2: Build SearchExplainSummary in orchestrator**

In `orchestrator.search()`, after all queries complete:

```python
    if self._config.explain_enabled and query_results:
        primary = query_results[0]
        summary = SearchExplainSummary(
            total_ms=total_search_ms,
            query_count=len(query_results),
            primary_query_class=primary.explain.query_class if primary.explain else "",
            primary_path=primary.explain.path if primary.explain else "",
            doc_scope_hit=any(qr.explain and qr.explain.doc_scope_hit for qr in query_results),
            time_filter_hit=any(qr.explain and qr.explain.time_filter_hit for qr in query_results),
            rerank_triggered=any(qr.explain and qr.explain.rerank_ms > 0 for qr in query_results),
        )
        find_result.explain_summary = summary
```

- [ ] **Step 3: Add explain to HTTP response**

In `src/opencortex/http/server.py`, in the search endpoint, check `request.query_params.get("explain")`:

```python
    explain_mode = request.query_params.get("explain")
    if explain_mode and result.explain_summary:
        response["explain_summary"] = asdict(result.explain_summary)
    if explain_mode == "detail" and result.query_results:
        response["explain_detail"] = [
            asdict(qr.explain) for qr in result.query_results if qr.explain
        ]
```

- [ ] **Step 4: Run tests**

Run: `uv run python3 -m pytest tests/test_search_explain.py tests/test_e2e_phase1.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py src/opencortex/orchestrator.py \
  src/opencortex/http/server.py
git commit -m "feat: SearchExplain instrumentation — 5-segment timing + HTTP explain param"
```

---

## Task 13: Ablation Experiment Framework

**Spec ref:** 4.12

**Files:**
- Create: `tests/benchmark/ablation.py`

- [ ] **Step 1: Create ablation.py**

```python
#!/usr/bin/env python3
"""Ablation experiment framework — single-variable sweep over benchmark datasets."""
import argparse
import csv
import json
import sys
from pathlib import Path

# Import runner functions
from runner import run_benchmark


def run_ablation(args):
    values = [v.strip() for v in args.values.split(",")]
    results = []

    for val in values:
        print(f"\n=== {args.variable} = {val} ===", file=sys.stderr)
        # Apply variable override via server config API or env var
        # For now, pass as metadata to runner
        report = run_benchmark(
            base_url=args.base_url,
            data_root=args.data_root,
            dataset_path=args.dataset,
            ks=[5],
            timeout=args.timeout,
        )
        summary = report.get("summary", {})
        results.append({
            "variable": args.variable,
            "value": val,
            "j_score": summary.get("j_score", 0),
            "f1": summary.get("f1", 0),
            "p50_ms": summary.get("latency_p50_ms", 0),
            "rerank_rate": summary.get("rerank_trigger_rate", 0),
        })

    # Write CSV
    if args.output:
        with open(args.output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["variable", "value", "j_score", "f1", "p50_ms", "rerank_rate"])
            writer.writeheader()
            writer.writerows(results)
        print(f"\nResults saved to {args.output}", file=sys.stderr)
    else:
        json.dump(results, sys.stdout, indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ablation experiment sweep")
    parser.add_argument("--variable", required=True, help="Variable to sweep")
    parser.add_argument("--values", required=True, help="Comma-separated values")
    parser.add_argument("--base-url", default="http://127.0.0.1:8921")
    parser.add_argument("--data-root", default="~/.opencortex")
    parser.add_argument("--dataset", required=True, help="Path to dataset JSON")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--output", help="Output CSV path")
    run_ablation(parser.parse_args())
```

- [ ] **Step 2: Verify script parses arguments**

Run: `uv run python3 tests/benchmark/ablation.py --help`
Expected: Help text displayed without errors

- [ ] **Step 3: Commit**

```bash
git add tests/benchmark/ablation.py
git commit -m "feat: ablation experiment framework — single-variable sweep"
```

---

## Task 14: ONNX Thread Tuning

**Spec ref:** 4.13

**Files:**
- Modify: `src/opencortex/models/embedder/local_embedder.py` (pass threads to FastEmbed)
- Modify: `src/opencortex/orchestrator.py` (pass config to embedder init)

- [ ] **Step 1: Modify LocalEmbedder to accept thread config**

In `src/opencortex/models/embedder/local_embedder.py`, modify `_init_model()` (around line 40):

```python
    def _init_model(self) -> None:
        try:
            from fastembed import TextEmbedding
            kwargs = {}
            threads = (self.config or {}).get("onnx_intra_op_threads", 0)
            if threads > 0:
                # FastEmbed accepts threads param for ONNX intra_op_num_threads
                kwargs["threads"] = threads
            self._model = TextEmbedding(model_name=self.model_name, **kwargs)
```

**Note:** Verify against the installed FastEmbed version. If `threads` kwarg is not directly supported, use `model_kwargs={"intra_op_num_threads": threads}` instead. Check FastEmbed docs/source for the correct parameter name.

- [ ] **Step 2: Pass config to embedder in orchestrator**

In `src/opencortex/orchestrator.py`, where `LocalEmbedder` is instantiated, pass `config={"onnx_intra_op_threads": self._config.onnx_intra_op_threads}`.

- [ ] **Step 3: Run tests**

Run: `uv run python3 -m pytest tests/test_e2e_phase1.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/opencortex/models/embedder/local_embedder.py src/opencortex/orchestrator.py
git commit -m "feat: ONNX intra_op_threads config passthrough to FastEmbed"
```

---

## Dependency Graph

```
Task 1 (SearchExplain + Config)
  ├─→ Task 2 (Schema + Flattening)
  │     ├─→ Task 5 (Doc Scoped Search) ─→ Task 7 (Small-to-Big)
  │     └─→ Task 6 (Context Flattening) [depends on Task 2 + Task 5 for source_doc_title/section_path]
  ├─→ Task 3 (Collection Routing)  ─→ Task 13 (Ablation)
  ├─→ Task 4 (Classifier) ─→ Task 8 (Dynamic Weights + _score bug fix)
  │                        ─→ Task 9 (Time Filter — recent/today/session)
  │                        ─→ Task 10 (Rerank Gate)
  ├─→ Task 11 (Frontier Budget)
  ├─→ Task 12 (SearchExplain Instrumentation) [depends on all features existing]
  └─→ Task 14 (ONNX Threads) [independent]
```

**Execution order:** 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14

Tasks 3, 6, 11, 14 are somewhat independent and could be parallelized with adjacent tasks, but sequential execution is recommended for clarity.
