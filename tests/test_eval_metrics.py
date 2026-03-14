"""Unit tests for benchmarks/metrics.py — retrieval, latency, and token reduction metrics."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.metrics import (
    compute_retrieval_metrics,
    compute_latency_metrics,
    compute_token_metrics,
    truncate_to_budget,
    _percentile,
)


class TestPercentile(unittest.TestCase):
    def test_p50_odd(self):
        self.assertAlmostEqual(_percentile([1, 2, 3, 4, 5], 50), 3.0)

    def test_p95_small(self):
        data = list(range(1, 101))  # 1..100
        self.assertAlmostEqual(_percentile(data, 95), 95.05, places=1)

    def test_single_element(self):
        self.assertAlmostEqual(_percentile([42.0], 50), 42.0)

    def test_p99(self):
        data = list(range(1, 101))
        self.assertGreater(_percentile(data, 99), 98.0)


class TestRetrievalMetrics(unittest.TestCase):
    def test_perfect_recall(self):
        records = [
            {"retrieved_uris": ["a", "b", "c"], "expected_uris": ["a", "b"]},
        ]
        m = compute_retrieval_metrics(records, ks=[1, 3])
        self.assertAlmostEqual(m["recall@3"], 1.0)

    def test_zero_recall(self):
        records = [
            {"retrieved_uris": ["x", "y", "z"], "expected_uris": ["a", "b"]},
        ]
        m = compute_retrieval_metrics(records, ks=[1, 3])
        self.assertAlmostEqual(m["recall@1"], 0.0)
        self.assertAlmostEqual(m["mrr"], 0.0)

    def test_mrr_first_hit_at_rank_2(self):
        records = [
            {"retrieved_uris": ["x", "a", "b"], "expected_uris": ["a"]},
        ]
        m = compute_retrieval_metrics(records, ks=[3])
        self.assertAlmostEqual(m["mrr"], 0.5)

    def test_skip_empty_expected(self):
        records = [
            {"retrieved_uris": ["a"], "expected_uris": ["a"]},
            {"retrieved_uris": ["b"], "expected_uris": []},
        ]
        m = compute_retrieval_metrics(records, ks=[1])
        self.assertEqual(m["evaluated_count"], 1)
        self.assertEqual(m["skipped_no_ground_truth"], 1)

    def test_by_category(self):
        records = [
            {"retrieved_uris": ["a"], "expected_uris": ["a"], "category": "easy"},
            {"retrieved_uris": ["x"], "expected_uris": ["a"], "category": "hard"},
        ]
        m = compute_retrieval_metrics(records, ks=[1])
        self.assertAlmostEqual(m["by_category"]["easy"]["recall@1"], 1.0)
        self.assertAlmostEqual(m["by_category"]["hard"]["recall@1"], 0.0)


class TestTokenMetrics(unittest.TestCase):
    def test_reduction(self):
        records = [
            {"oc_prompt_tokens": 200, "baseline_prompt_tokens": 1000},
            {"oc_prompt_tokens": 300, "baseline_prompt_tokens": 1000},
        ]
        m = compute_token_metrics(records)
        self.assertAlmostEqual(m["reduction_pct"], 75.0)
        self.assertEqual(m["oc_total_tokens"], 500)
        self.assertEqual(m["baseline_total_tokens"], 2000)

    def test_no_reduction(self):
        records = [
            {"oc_prompt_tokens": 1000, "baseline_prompt_tokens": 1000},
        ]
        m = compute_token_metrics(records)
        self.assertAlmostEqual(m["reduction_pct"], 0.0)

    def test_empty(self):
        m = compute_token_metrics([])
        self.assertAlmostEqual(m["reduction_pct"], 0.0)


class TestLatencyMetrics(unittest.TestCase):
    def test_basic(self):
        lats = [100.0, 200.0, 300.0, 400.0, 500.0]
        m = compute_latency_metrics(lats)
        self.assertAlmostEqual(m["p50_ms"], 300.0)
        self.assertEqual(m["count"], 5)
        self.assertAlmostEqual(m["mean_ms"], 300.0)

    def test_single(self):
        m = compute_latency_metrics([42.0])
        self.assertAlmostEqual(m["p50_ms"], 42.0)
        self.assertAlmostEqual(m["p99_ms"], 42.0)


class TestTruncateToBudget(unittest.TestCase):
    def test_short_text_unchanged(self):
        text = "hello world"
        self.assertEqual(truncate_to_budget(text, 1000), text)

    def test_long_text_truncated(self):
        text = "a" * 10000  # ~3000 tokens (0.3 per char)
        result = truncate_to_budget(text, 100)
        self.assertLess(len(result), len(text))

    def test_empty_text(self):
        self.assertEqual(truncate_to_budget("", 1000), "")


if __name__ == "__main__":
    unittest.main()
