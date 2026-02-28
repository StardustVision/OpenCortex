# SPDX-License-Identifier: Apache-2.0
"""Tests for memory retrieval evaluation helpers."""

import tempfile
import unittest
import importlib.util
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "opencortex"
    / "eval"
    / "memory_eval.py"
)
SPEC = importlib.util.spec_from_file_location("memory_eval_module", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Failed to load module from {MODULE_PATH}")
memory_eval = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(memory_eval)

compute_report = memory_eval.compute_report
evaluate_dataset = memory_eval.evaluate_dataset
load_dataset = memory_eval.load_dataset
parse_ks = memory_eval.parse_ks


class TestMemoryEvalHelpers(unittest.TestCase):
    def test_parse_ks(self):
        self.assertEqual(parse_ks("5,1,3,3"), [1, 3, 5])
        with self.assertRaises(ValueError):
            parse_ks("")
        with self.assertRaises(ValueError):
            parse_ks("0")

    def test_compute_report_basic_metrics(self):
        rows = [
            {
                "query": "q1",
                "expected_uris": ["opencortex://t/m/a"],
                "predicted_uris": ["opencortex://t/m/a", "opencortex://t/m/b"],
                "category": "preferences",
                "difficulty": "easy",
            },
            {
                "query": "q2",
                "expected_uris": ["opencortex://t/m/c"],
                "predicted_uris": ["opencortex://t/m/b", "opencortex://t/m/c"],
                "category": "errors",
                "difficulty": "hard",
            },
        ]

        report = compute_report(rows, ks=[1, 2])
        summary = report["summary"]

        self.assertEqual(report["scored_count"], 2)
        self.assertEqual(report["skipped_count"], 0)

        self.assertAlmostEqual(summary["recall@1"], 0.5)
        self.assertAlmostEqual(summary["precision@1"], 0.5)
        self.assertAlmostEqual(summary["hit_rate@1"], 0.5)
        self.assertAlmostEqual(summary["accuracy@1"], 0.5)

        self.assertAlmostEqual(summary["recall@2"], 1.0)
        self.assertAlmostEqual(summary["precision@2"], 0.5)
        self.assertAlmostEqual(summary["hit_rate@2"], 1.0)
        self.assertAlmostEqual(summary["accuracy@2"], 1.0)
        self.assertAlmostEqual(summary["mrr"], 0.75)

    def test_compute_report_multi_label_recall(self):
        rows = [
            {
                "query": "q1",
                "expected_uris": ["u/a", "u/b"],
                "predicted_uris": ["u/a", "u/x", "u/b"],
                "category": "patterns",
                "difficulty": "medium",
            }
        ]
        report = compute_report(rows, ks=[2, 3])
        summary = report["summary"]
        self.assertAlmostEqual(summary["recall@2"], 0.5)
        self.assertAlmostEqual(summary["recall@3"], 1.0)
        self.assertAlmostEqual(summary["precision@3"], 2.0 / 3.0)

    def test_evaluate_dataset_with_fake_search(self):
        dataset = [
            {
                "query": "dark theme",
                "expected_uris": ["u/prefs/theme"],
                "category": "preferences",
                "difficulty": "easy",
            },
            {
                "query": "timeout fix",
                "expected_uris": ["u/errors/timeout"],
                "category": "errors",
                "difficulty": "hard",
            },
        ]

        mapping = {
            "dark theme": ["u/prefs/theme", "u/other"],
            "timeout fix": ["u/other", "u/errors/timeout"],
        }

        def fake_search(item, max_k):
            return mapping[item["query"]][:max_k]

        report = evaluate_dataset(dataset, ks=[1, 2], search_fn=fake_search)
        self.assertEqual(report["scored_count"], 2)
        self.assertIn("preferences", report["by_category"])
        self.assertIn("hard", report["by_difficulty"])
        self.assertAlmostEqual(report["summary"]["mrr"], 0.75)

    def test_token_comparison(self):
        rows = [
            {
                "query": "q1",
                "expected_uris": ["u/1"],
                "predicted_uris": ["u/1"],
                "tokens_with_memory": 400,
                "tokens_without_memory": 800,
            },
            {
                "query": "q2",
                "expected_uris": ["u/2"],
                "predicted_uris": ["u/2"],
                "tokens_with_memory": 500,
                "tokens_without_memory": 1000,
            },
        ]
        report = compute_report(rows, ks=[1])
        token_comp = report["token_comparison"]
        self.assertEqual(token_comp["count"], 2.0)
        self.assertEqual(token_comp["total_tokens_with_memory"], 900.0)
        self.assertEqual(token_comp["total_tokens_without_memory"], 1800.0)
        self.assertEqual(token_comp["token_reduction"], 900.0)
        self.assertAlmostEqual(token_comp["token_reduction_ratio"], 0.5)

    def test_load_dataset_json_and_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            json_path = root / "dataset.json"
            json_path.write_text(
                '[{"query":"q1","expected_uris":["u/1"]}]',
                encoding="utf-8",
            )
            loaded_json = load_dataset(str(json_path))
            self.assertEqual(len(loaded_json), 1)

            json_obj_path = root / "dataset_obj.json"
            json_obj_path.write_text(
                '{"queries":[{"query":"q2","expected_uris":["u/2"]}]}',
                encoding="utf-8",
            )
            loaded_obj = load_dataset(str(json_obj_path))
            self.assertEqual(len(loaded_obj), 1)

            jsonl_path = root / "dataset.jsonl"
            jsonl_path.write_text(
                '{"query":"q3","expected_uris":["u/3"]}\n{"query":"q4","expected_uris":["u/4"]}\n',
                encoding="utf-8",
            )
            loaded_jsonl = load_dataset(str(jsonl_path))
            self.assertEqual(len(loaded_jsonl), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
