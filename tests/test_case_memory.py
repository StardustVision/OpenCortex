"""Structured Case Memory tests."""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

from opencortex.session.types import ExtractedMemory
from opencortex.session.extractor import MemoryExtractor, validate_case_meta, CASE_META_SCHEMA_VERSION


class TestCaseMemory(unittest.TestCase):
    """Structured Case Memory — 8 tests."""

    def test_extracted_memory_meta_default(self):
        """T0.1: ExtractedMemory.meta initializes to empty dict."""
        m = ExtractedMemory(abstract="test")
        self.assertIsInstance(m.meta, dict)
        self.assertEqual(m.meta, {})

    def test_prompt_contains_case_meta_keywords(self):
        """T0.2: LLM prompt includes case meta 6-field keywords."""
        ext = MemoryExtractor(llm_completion=AsyncMock())
        from opencortex.session.types import Message
        prompt = ext._build_extraction_prompt(
            [Message(role="user", content="test")], 0.8, ""
        )
        for kw in ["task_objective", "action_path", "result", "evaluation", "error_cause", "improvement"]:
            self.assertIn(kw, prompt, f"Missing keyword: {kw}")

    def test_parse_case_with_full_meta(self):
        """T0.3: Parse case with complete meta produces correct ExtractedMemory."""
        ext = MemoryExtractor(llm_completion=AsyncMock())
        data = [{
            "abstract": "Fixed import error",
            "content": "details",
            "category": "cases",
            "context_type": "case",
            "confidence": 0.8,
            "meta": {
                "schema_version": 1,
                "task_objective": "Fix import",
                "action_path": ["check path", "add src"],
                "result": "resolved",
                "evaluation": {"status": "success", "score": 0.9},
                "error_cause": "",
                "improvement": "auto-setup",
            }
        }]
        memories = ext._convert_to_memories(data)
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].meta["task_objective"], "Fix import")
        self.assertEqual(memories[0].meta["evaluation"]["status"], "success")

    def test_validate_case_meta_fills_defaults(self):
        """T0.4: validate_case_meta fills missing fields."""
        meta = validate_case_meta({})
        self.assertEqual(meta["schema_version"], CASE_META_SCHEMA_VERSION)
        self.assertEqual(meta["task_objective"], "")
        self.assertEqual(meta["action_path"], [])
        self.assertEqual(meta["evaluation"]["status"], "unknown")

    def test_schema_version_injected(self):
        """T0.5: schema_version=1 auto-injected."""
        meta = validate_case_meta({"task_objective": "test"})
        self.assertEqual(meta["schema_version"], 1)

    def test_session_manager_passes_meta(self):
        """T0.6: SessionManager passes meta to store_fn."""
        from opencortex.session.manager import SessionManager

        captured = {}
        async def mock_store(**kwargs):
            captured.update(kwargs)
            result = MagicMock()
            result.meta = {"dedup_action": "created"}
            return result

        mgr = SessionManager(store_fn=mock_store)
        mem = ExtractedMemory(
            abstract="test", confidence=0.5,
            context_type="case",
            meta={"task_objective": "debug"}
        )
        asyncio.run(mgr._store_memory(mem))
        self.assertIn("meta", captured)
        self.assertEqual(captured["meta"]["task_objective"], "debug")

    def test_full_chain_case_storage(self):
        """T0.7: Full chain: extract → store → meta preserved."""
        ext = MemoryExtractor(llm_completion=AsyncMock())
        data = [{
            "abstract": "Deploy fix",
            "content": "details",
            "category": "cases",
            "context_type": "case",
            "confidence": 0.9,
            "meta": {
                "task_objective": "Fix deploy",
                "action_path": ["rollback", "patch", "redeploy"],
                "result": "deployed",
                "evaluation": {"status": "success", "score": 1.0},
                "error_cause": "",
                "improvement": "add CI check",
            }
        }]
        memories = ext._convert_to_memories(data)
        self.assertEqual(len(memories), 1)
        m = memories[0]
        self.assertEqual(m.context_type, "case")
        self.assertEqual(len(m.meta["action_path"]), 3)
        self.assertEqual(m.meta["schema_version"], 1)  # auto-injected

    def test_meta_backward_compat(self):
        """T0.8: Old records without meta don't break."""
        ext = MemoryExtractor(llm_completion=AsyncMock())
        data = [{
            "abstract": "Old memory",
            "content": "no meta field",
            "category": "events",
            "context_type": "memory",
            "confidence": 0.6,
        }]
        memories = ext._convert_to_memories(data)
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].meta, {})


if __name__ == "__main__":
    unittest.main()
