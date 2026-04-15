"""
Unit tests for benchmark runner metric computation.

Tests use known inputs to verify Recall@k, Precision@k, MRR, and
category-level aggregation from memory_eval.py.
"""

import asyncio
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


if __name__ == "__main__":
    unittest.main()
