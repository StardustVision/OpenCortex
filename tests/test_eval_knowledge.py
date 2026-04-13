"""Tests for knowledge quality evaluation adapter and scoring."""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.adapters.knowledge import KnowledgeAdapter, _parse_json_response


class TestKnowledgeAdapter(unittest.TestCase):
    """Test KnowledgeAdapter dataset loading and QA building."""

    def test_load_gold_standard(self):
        adapter = KnowledgeAdapter()
        adapter.load_dataset("benchmarks/datasets/knowledge/gold_standard.json")
        self.assertGreater(len(adapter._clusters), 0)

    def test_build_qa_items(self):
        adapter = KnowledgeAdapter()
        adapter.load_dataset("benchmarks/datasets/knowledge/gold_standard.json")
        items = adapter.build_qa_items()
        self.assertEqual(len(items), len(adapter._clusters))

        # Each item should have expected knowledge
        for item in items:
            expected = item.meta.get("expected_knowledge", [])
            self.assertGreater(len(expected), 0)
            self.assertIn("cluster_id", item.meta)
            self.assertIn("traces", item.meta)
            self.assertEqual(item.meta["dataset"], "knowledge")

    def test_max_qa_limit(self):
        adapter = KnowledgeAdapter()
        adapter.load_dataset("benchmarks/datasets/knowledge/gold_standard.json")
        items = adapter.build_qa_items(max_qa=3)
        self.assertEqual(len(items), 3)

    def test_baseline_context(self):
        adapter = KnowledgeAdapter()
        adapter.load_dataset("benchmarks/datasets/knowledge/gold_standard.json")
        items = adapter.build_qa_items(max_qa=1)
        ctx = adapter.get_baseline_context(items[0])
        self.assertGreater(len(ctx), 0)
        # Should contain trace abstracts
        self.assertIn("[", ctx)

    def test_all_types_covered(self):
        adapter = KnowledgeAdapter()
        adapter.load_dataset("benchmarks/datasets/knowledge/gold_standard.json")
        items = adapter.build_qa_items()
        types = set()
        for item in items:
            for k in item.meta.get("expected_knowledge", []):
                types.add(k.get("type"))
        self.assertIn("belief", types)
        self.assertIn("sop", types)
        self.assertIn("negative_rule", types)
        self.assertIn("root_cause", types)

    def test_set_llm_fn(self):
        adapter = KnowledgeAdapter()
        fn = lambda prompt, max_tokens, **kwargs: "test"
        adapter.set_llm_fn(fn)
        self.assertEqual(adapter._get_llm_fn(), fn)

    def test_llm_fn_not_set_raises(self):
        adapter = KnowledgeAdapter()
        with self.assertRaises(RuntimeError):
            adapter._get_llm_fn()


class TestParseJsonResponse(unittest.TestCase):
    """Test JSON parsing from LLM responses."""

    def test_plain_json(self):
        result = _parse_json_response('{"match": true, "expected_index": 0}')
        self.assertIsNotNone(result)
        self.assertTrue(result["match"])

    def test_markdown_fenced(self):
        result = _parse_json_response('```json\n{"match": false}\n```')
        self.assertIsNotNone(result)
        self.assertFalse(result["match"])

    def test_invalid_json(self):
        result = _parse_json_response("not json at all")
        self.assertIsNone(result)

    def test_empty_string(self):
        result = _parse_json_response("")
        self.assertIsNone(result)


class TestEvaluateExtraction(unittest.TestCase):
    """Test knowledge extraction evaluation logic."""

    def test_empty_expected(self):
        adapter = KnowledgeAdapter()
        import asyncio
        result = asyncio.run(adapter.evaluate_extraction([], [], None))
        self.assertEqual(result["recall"], 0.0)
        self.assertEqual(result["precision"], 0.0)
        self.assertEqual(result["n_expected"], 0)

    def test_extracted_with_no_expected(self):
        adapter = KnowledgeAdapter()
        extracted = [{"statement": "something", "knowledge_type": "belief"}]
        result = asyncio.run(adapter.evaluate_extraction(extracted, [], None))
        self.assertEqual(result["recall"], 0.0)
        self.assertEqual(result["hallucination_rate"], 1.0)

    def test_matching_with_mock_llm(self):
        adapter = KnowledgeAdapter()

        # Mock LLM that always matches index 0
        async def mock_llm(prompt, max_tokens, **kwargs):
            return json.dumps({"match": True, "expected_index": 0, "reason": "same topic"})

        extracted = [{
            "knowledge_type": "belief",
            "statement": "User prefers vegetarian food",
            "objective": "Recommend vegetarian options",
        }]
        expected = [{
            "type": "belief",
            "statement": "User is vegetarian",
            "objective": "Suggest vegetarian dishes",
            "confidence": 0.9,
        }]

        result = asyncio.run(adapter.evaluate_extraction(extracted, expected, mock_llm))
        self.assertEqual(result["recall"], 1.0)
        self.assertEqual(result["precision"], 1.0)
        self.assertTrue(result["matches"][0]["type_match"])

    def test_type_mismatch_detection(self):
        adapter = KnowledgeAdapter()

        async def mock_llm(prompt, max_tokens, **kwargs):
            return json.dumps({"match": True, "expected_index": 0, "reason": "same topic"})

        extracted = [{
            "knowledge_type": "sop",  # Wrong type
            "statement": "User prefers vegetarian food",
        }]
        expected = [{
            "type": "belief",  # Correct type
            "statement": "User is vegetarian",
        }]

        result = asyncio.run(adapter.evaluate_extraction(extracted, expected, mock_llm))
        self.assertEqual(result["type_accuracy"], 0.0)  # Type mismatch
        self.assertEqual(result["recall"], 1.0)  # Still matched

    def test_hallucination_detection(self):
        adapter = KnowledgeAdapter()

        async def mock_llm(prompt, max_tokens, **kwargs):
            return json.dumps({"match": False, "expected_index": -1, "reason": "unrelated"})

        extracted = [
            {"knowledge_type": "belief", "statement": "Wrong knowledge 1"},
            {"knowledge_type": "sop", "statement": "Wrong knowledge 2"},
        ]
        expected = [
            {"type": "belief", "statement": "Expected knowledge"},
        ]

        result = asyncio.run(adapter.evaluate_extraction(extracted, expected, mock_llm))
        self.assertEqual(result["recall"], 0.0)
        self.assertEqual(result["precision"], 0.0)
        self.assertEqual(result["hallucination_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
