"""
Unit tests for benchmark runner metric computation.

Tests use known inputs to verify Recall@k, Precision@k, MRR, and
category-level aggregation from memory_eval.py.
"""

import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "benchmark"))

from opencortex.eval.memory_eval import _query_metrics, _aggregate, compute_report
from runner import _extract_search_attribution


class TestBenchmarkMetrics(unittest.TestCase):
    """Verify metric computation with known inputs."""

    def test_perfect_recall_at_5(self):
        """All expected URIs in top-5 → recall@5 = 1.0."""
        metrics = _query_metrics(
            predicted=["uri_a", "uri_b", "uri_c", "uri_d", "uri_e"],
            expected=["uri_a", "uri_c"],
            ks=[5],
        )
        self.assertAlmostEqual(metrics["recall@5"], 1.0)
        self.assertAlmostEqual(metrics["precision@5"], 0.4)  # 2/5
        self.assertAlmostEqual(metrics["hit_rate@5"], 1.0)
        self.assertAlmostEqual(metrics["mrr"], 1.0)  # first hit at rank 1

    def test_zero_recall(self):
        """No expected URIs in results → recall = 0."""
        metrics = _query_metrics(
            predicted=["uri_x", "uri_y"],
            expected=["uri_a"],
            ks=[5],
        )
        self.assertAlmostEqual(metrics["recall@5"], 0.0)
        self.assertAlmostEqual(metrics["mrr"], 0.0)

    def test_partial_recall(self):
        """One of two expected found → recall@5 = 0.5."""
        metrics = _query_metrics(
            predicted=["uri_x", "uri_a", "uri_y", "uri_z", "uri_w"],
            expected=["uri_a", "uri_b"],
            ks=[5],
        )
        self.assertAlmostEqual(metrics["recall@5"], 0.5)
        self.assertAlmostEqual(metrics["mrr"], 0.5)  # first hit at rank 2

    def test_mrr_rank_position(self):
        """MRR reflects rank of first relevant result."""
        metrics = _query_metrics(
            predicted=["uri_x", "uri_y", "uri_a"],
            expected=["uri_a"],
            ks=[5],
        )
        self.assertAlmostEqual(metrics["mrr"], 1.0 / 3)

    def test_multiple_k_values(self):
        """Different k values produce different metrics."""
        metrics = _query_metrics(
            predicted=["uri_x", "uri_y", "uri_a"],
            expected=["uri_a"],
            ks=[1, 3, 5],
        )
        self.assertAlmostEqual(metrics["recall@1"], 0.0)   # not in top 1
        self.assertAlmostEqual(metrics["recall@3"], 1.0)   # in top 3
        self.assertAlmostEqual(metrics["recall@5"], 1.0)   # in top 5

    def test_aggregate_averages(self):
        """Aggregate averages per-query metrics."""
        row1 = {"recall@5": 1.0, "precision@5": 0.4, "hit_rate@5": 1.0, "accuracy@5": 1.0, "mrr": 1.0}
        row2 = {"recall@5": 0.0, "precision@5": 0.0, "hit_rate@5": 0.0, "accuracy@5": 0.0, "mrr": 0.0}
        agg = _aggregate([row1, row2], ks=[5])
        self.assertAlmostEqual(agg["recall@5"], 0.5)
        self.assertAlmostEqual(agg["mrr"], 0.5)
        self.assertAlmostEqual(agg["count"], 2.0)

    def test_compute_report_with_categories(self):
        """Report groups metrics by category."""
        rows = [
            {"query": "q1", "expected_uris": ["a"], "predicted_uris": ["a", "b"], "category": "preference"},
            {"query": "q2", "expected_uris": ["c"], "predicted_uris": ["x", "y"], "category": "preference"},
            {"query": "q3", "expected_uris": ["d"], "predicted_uris": ["d", "e"], "category": "entity"},
        ]
        report = compute_report(rows, ks=[5])
        self.assertEqual(report["scored_count"], 3)
        self.assertIn("preference", report["by_category"])
        self.assertIn("entity", report["by_category"])
        self.assertAlmostEqual(report["by_category"]["entity"]["recall@5"], 1.0)
        self.assertAlmostEqual(report["by_category"]["preference"]["recall@5"], 0.5)

    def test_empty_predicted_gives_zero(self):
        """No predictions → all metrics zero."""
        metrics = _query_metrics(
            predicted=[],
            expected=["uri_a"],
            ks=[5],
        )
        self.assertAlmostEqual(metrics["recall@5"], 0.0)
        self.assertAlmostEqual(metrics["mrr"], 0.0)

    def test_extract_search_attribution_reads_memory_pipeline(self):
        attribution = _extract_search_attribution(
            {
                "memory_pipeline": {
                    "probe": {
                        "should_recall": True,
                        "evidence": {"candidate_count": 2, "top_score": 0.82},
                    },
                    "planner": {
                        "target_memory_kinds": ["relation", "event"],
                        "retrieval_depth": "l1",
                    },
                    "runtime": {
                        "trace": {
                            "probe": {
                                "should_recall": True,
                                "evidence": {"candidate_count": 2},
                            },
                            "planner": {"retrieval_depth": "l1"},
                            "effective": {
                                "sources": ["memory"],
                                "retrieval_depth": "l1",
                            },
                            "latency_ms": {"execution": 12},
                        },
                        "degrade": {"applied": False, "actions": []},
                    },
                }
            }
        )

        self.assertEqual(attribution["probe"]["evidence"]["candidate_count"], 2)
        self.assertEqual(attribution["planner"]["retrieval_depth"], "l1")
        self.assertEqual(
            attribution["runtime"]["trace"]["effective"]["sources"], ["memory"]
        )
        self.assertFalse(attribution["runtime"]["degrade"]["applied"])

    def test_adapter_meta_can_expose_retrieval_contract(self):
        from benchmarks.adapters.base import EvalAdapter

        class _AdapterStub(EvalAdapter):
            async def ingest(self, oc, **kwargs):
                raise NotImplementedError

            def build_qa_items(self, **kwargs):
                raise NotImplementedError

            def get_baseline_context(self, qa_item):
                raise NotImplementedError

            async def retrieve(self, oc, qa_item, top_k):
                raise NotImplementedError

        adapter = _AdapterStub()
        adapter._retrieve_method = "recall"
        adapter._set_last_retrieval_meta(
            {
                "memory_pipeline": {
                    "probe": {"should_recall": True},
                    "planner": {"retrieval_depth": "l1"},
                    "runtime": {"trace": {}, "degrade": {"applied": False}},
                }
            },
            endpoint="context_recall",
            session_scope=True,
        )

        self.assertEqual(
            adapter.pop_last_retrieval_meta()["retrieval_contract"],
            {
                "method": "recall",
                "endpoint": "context_recall",
                "session_scope": True,
            },
        )

    def test_document_adapter_recall_mode_still_uses_search_payload(self):
        from benchmarks.adapters.document import DocumentAdapter

        class _OCStub:
            def __init__(self):
                self.search_calls = []
                self.recall_calls = []

            async def search_payload(self, **kwargs):
                self.search_calls.append(dict(kwargs))
                return {"results": [{"uri": "opencortex://resource/doc-1/chunk-1"}]}

            async def context_recall(self, **kwargs):
                self.recall_calls.append(dict(kwargs))
                return {"memory": [{"uri": "unexpected"}]}

        adapter = DocumentAdapter()
        adapter._retrieve_method = "recall"
        qa_item = type("QA", (), {"question": "What does the paper conclude?"})()
        oc = _OCStub()

        results, _latency_ms = asyncio.run(adapter.retrieve(oc, qa_item, top_k=4))
        retrieval_meta = adapter.pop_last_retrieval_meta()

        self.assertEqual(
            [item["uri"] for item in results],
            ["opencortex://resource/doc-1/chunk-1"],
        )
        self.assertEqual(len(oc.recall_calls), 0)
        self.assertEqual(oc.search_calls[0]["context_type"], "resource")
        self.assertEqual(
            retrieval_meta["retrieval_contract"],
            {
                "method": "recall",
                "endpoint": "memory_search",
                "session_scope": False,
            },
        )


class TestEvalAdapterBase(unittest.TestCase):
    """Tests for the shared EvalAdapter base class methods."""

    def _make_adapter(self, **overrides):
        """Create a minimal concrete adapter with optional attribute overrides."""
        from benchmarks.adapters.base import EvalAdapter, QAItem

        class _Stub(EvalAdapter):
            async def ingest(self, oc, **kwargs):
                raise NotImplementedError

            def build_qa_items(self, **kwargs):
                raise NotImplementedError

            def get_baseline_context(self, qa_item):
                raise NotImplementedError

        adapter = _Stub()
        for k, v in overrides.items():
            setattr(adapter, k, v)
        return adapter

    def test_default_retrieve_method_is_search(self):
        adapter = self._make_adapter()
        self.assertEqual(adapter._retrieve_method, "search")

    def test_default_ingest_method_is_empty(self):
        adapter = self._make_adapter()
        self.assertEqual(adapter._ingest_method, "")

    # -- retrieve() dispatch --

    def test_retrieve_dispatches_search_by_default(self):
        """Default _retrieve_method='search' calls search_payload."""
        from benchmarks.adapters.base import QAItem

        adapter = self._make_adapter()

        class _OC:
            def __init__(self):
                self.search_called = False

            async def search_payload(self, **kwargs):
                self.search_called = True
                return {"results": [{"uri": "u1"}]}

            async def context_recall(self, **kwargs):
                raise AssertionError("should not be called")

        oc = _OC()
        qa = QAItem(question="test", answer="a")
        results, latency = asyncio.run(adapter.retrieve(oc, qa, top_k=5))
        self.assertTrue(oc.search_called)
        self.assertEqual(len(results), 1)
        self.assertGreater(latency, 0)

    def test_retrieve_dispatches_recall_when_configured(self):
        """_retrieve_method='recall' calls context_recall."""
        from benchmarks.adapters.base import QAItem

        class _ScopedAdapter(self._make_adapter().__class__):
            def _get_retrieval_session_id(self, qa_item):
                return "test-session"

            def _get_retrieval_session_scope(self):
                return True

        adapter = _ScopedAdapter()
        adapter._retrieve_method = "recall"

        class _OC:
            async def search_payload(self, **kwargs):
                raise AssertionError("should not be called")

            async def context_recall(self, **kwargs):
                assert kwargs["session_id"] == "test-session"
                assert kwargs["session_scope"] is True
                return {"memory": [{"uri": "u1"}]}

        oc = _OC()
        qa = QAItem(question="test", answer="a")
        results, _ = asyncio.run(adapter.retrieve(oc, qa, top_k=5))
        self.assertEqual(len(results), 1)

    def test_retrieve_metadata_filter_from_session_id_hook(self):
        """Session-scoped adapters get metadata_filter with session_id."""
        from benchmarks.adapters.base import QAItem

        class _ScopedAdapter(self._make_adapter().__class__):
            def _get_retrieval_session_id(self, qa_item):
                return "sess-123"

        adapter = _ScopedAdapter()

        class _OC:
            def __init__(self):
                self.kwargs = {}

            async def search_payload(self, **kwargs):
                self.kwargs = kwargs
                return {"results": []}

            async def context_recall(self, **kwargs):
                raise AssertionError("should not be called")

        oc = _OC()
        qa = QAItem(question="test", answer="a")
        asyncio.run(adapter.retrieve(oc, qa, top_k=5))
        self.assertEqual(
            oc.kwargs["metadata_filter"],
            {"op": "must", "field": "session_id", "conds": ["sess-123"]},
        )

    def test_retrieve_context_type_from_hook(self):
        """_get_retrieval_context_type hook passes context_type to search."""
        from benchmarks.adapters.base import QAItem

        class _ResourceAdapter(self._make_adapter().__class__):
            def _get_retrieval_context_type(self):
                return "resource"

        adapter = _ResourceAdapter()

        class _OC:
            def __init__(self):
                self.kwargs = {}

            async def search_payload(self, **kwargs):
                self.kwargs = kwargs
                return {"results": []}

            async def context_recall(self, **kwargs):
                raise AssertionError("should not be called")

        oc = _OC()
        qa = QAItem(question="test", answer="a")
        asyncio.run(adapter.retrieve(oc, qa, top_k=5))
        self.assertEqual(oc.kwargs["context_type"], "resource")

    def test_retrieve_post_process_hook(self):
        """_post_process_retrieval hook can filter/dedup results."""
        from benchmarks.adapters.base import QAItem

        class _DedupAdapter(self._make_adapter().__class__):
            def _post_process_retrieval(self, results):
                seen = set()
                deduped = []
                for r in results:
                    uri = r.get("uri", "")
                    if uri not in seen:
                        seen.add(uri)
                        deduped.append(r)
                return deduped

        adapter = _DedupAdapter()

        class _OC:
            async def search_payload(self, **kwargs):
                return {
                    "results": [
                        {"uri": "u1"}, {"uri": "u1"}, {"uri": "u2"},
                    ]
                }

            async def context_recall(self, **kwargs):
                raise AssertionError("should not be called")

        oc = _OC()
        qa = QAItem(question="test", answer="a")
        results, _ = asyncio.run(adapter.retrieve(oc, qa, top_k=5))
        self.assertEqual(len(results), 2)

    def test_retrieve_sets_retrieval_meta(self):
        """retrieve() populates _last_retrieval_meta with contract."""
        from benchmarks.adapters.base import QAItem

        adapter = self._make_adapter()

        class _OC:
            async def search_payload(self, **kwargs):
                return {"results": []}

            async def context_recall(self, **kwargs):
                return {"memory": []}

        oc = _OC()
        qa = QAItem(question="test", answer="a")
        asyncio.run(adapter.retrieve(oc, qa, top_k=5))
        meta = adapter.pop_last_retrieval_meta()
        self.assertEqual(meta["retrieval_contract"]["endpoint"], "memory_search")
        self.assertFalse(meta["retrieval_contract"]["session_scope"])

    # -- load_dataset --

    def test_load_dataset_reads_json(self):
        """Base load_dataset reads JSON and stores in _dataset."""
        import tempfile

        adapter = self._make_adapter()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as f:
            json.dump({"items": [1, 2, 3]}, f)
            f.flush()
            adapter.load_dataset(f.name)
        os.unlink(f.name)
        self.assertEqual(adapter._dataset, {"items": [1, 2, 3]})

    def test_load_dataset_calls_validate_hook(self):
        """_validate_dataset is called after loading."""
        import tempfile

        validated = [False]

        class _ValidatingAdapter(self._make_adapter().__class__):
            def _validate_dataset(self, raw):
                validated[0] = True

        adapter = _ValidatingAdapter()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as f:
            json.dump([1, 2], f)
            f.flush()
            adapter.load_dataset(f.name)
        os.unlink(f.name)
        self.assertTrue(validated[0])


if __name__ == "__main__":
    unittest.main()
