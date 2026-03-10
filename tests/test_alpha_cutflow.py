"""Tests for Cortex Alpha cut-flow toggle (use_alpha_pipeline)."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from opencortex.config import CortexConfig, CortexAlphaConfig


class TestCutFlowConfig(unittest.TestCase):

    def test_default_off(self):
        cfg = CortexAlphaConfig()
        self.assertFalse(cfg.use_alpha_pipeline)

    def test_toggle_on(self):
        cfg = CortexAlphaConfig(use_alpha_pipeline=True)
        self.assertTrue(cfg.use_alpha_pipeline)

    def test_nested_in_cortex_config(self):
        cfg = CortexConfig(
            cortex_alpha=CortexAlphaConfig(use_alpha_pipeline=True)
        )
        self.assertTrue(cfg.cortex_alpha.use_alpha_pipeline)


class TestSessionEndCutFlow(unittest.IsolatedAsyncioTestCase):

    def _make_orchestrator(self, use_alpha=False):
        """Create a minimal orchestrator mock for testing cut-flow."""
        from opencortex.orchestrator import MemoryOrchestrator
        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        orch._initialized = True
        orch._config = CortexConfig(
            cortex_alpha=CortexAlphaConfig(use_alpha_pipeline=use_alpha)
        )
        orch._session_manager = AsyncMock()
        orch._session_manager.end = AsyncMock(return_value=MagicMock(
            session_id="s1",
            stored_count=2,
            merged_count=1,
            skipped_count=0,
            quality_score=0.5,
            memories=[],
        ))
        orch._observer = None
        orch._trace_splitter = None
        orch._trace_store = None
        orch._archivist = None
        orch._knowledge_store = None
        return orch

    @patch("opencortex.orchestrator.get_effective_identity", return_value=("team", "hugo"))
    async def test_legacy_mode_calls_session_manager(self, _):
        orch = self._make_orchestrator(use_alpha=False)
        result = await orch.session_end("s1")
        orch._session_manager.end.assert_called_once_with("s1", 0.5)
        self.assertEqual(result["stored_count"], 2)

    @patch("opencortex.orchestrator.get_effective_identity", return_value=("team", "hugo"))
    async def test_alpha_mode_skips_session_manager(self, _):
        orch = self._make_orchestrator(use_alpha=True)
        result = await orch.session_end("s1")
        orch._session_manager.end.assert_not_called()
        self.assertEqual(result["stored_count"], 0)
        self.assertEqual(result["session_id"], "s1")


class TestSkillLookupCutFlow(unittest.IsolatedAsyncioTestCase):

    def _make_orchestrator(self, use_alpha=False):
        from opencortex.orchestrator import MemoryOrchestrator
        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        orch._initialized = True
        orch._config = CortexConfig(
            cortex_alpha=CortexAlphaConfig(use_alpha_pipeline=use_alpha)
        )
        # Skillbook mock
        orch._skillbook = AsyncMock()
        skill_mock = MagicMock()
        skill_mock.status = "active"
        skill_mock.confidence_score = 0.8
        skill_mock.to_dict.return_value = {"id": "s1", "content": "test"}
        orch._skillbook.search = AsyncMock(return_value=[skill_mock])
        # Knowledge store mock
        orch._knowledge_store = AsyncMock()
        orch._knowledge_store.search = AsyncMock(return_value=[
            {"knowledge_id": "k1", "knowledge_type": "sop"},
        ])
        return orch

    @patch("opencortex.orchestrator.get_effective_identity", return_value=("team", "hugo"))
    async def test_legacy_mode_uses_skillbook(self, _):
        orch = self._make_orchestrator(use_alpha=False)
        result = await orch.skill_lookup("deploy")
        orch._skillbook.search.assert_called_once()
        orch._knowledge_store.search.assert_not_called()
        self.assertEqual(len(result), 1)

    @patch("opencortex.orchestrator.get_effective_identity", return_value=("team", "hugo"))
    async def test_alpha_mode_uses_knowledge_store(self, _):
        orch = self._make_orchestrator(use_alpha=True)
        result = await orch.skill_lookup("deploy")
        orch._knowledge_store.search.assert_called_once()
        orch._skillbook.search.assert_not_called()
        self.assertEqual(result[0]["knowledge_type"], "sop")

    @patch("opencortex.orchestrator.get_effective_identity", return_value=("team", "hugo"))
    async def test_alpha_mode_error_fixes_section(self, _):
        orch = self._make_orchestrator(use_alpha=True)
        await orch.skill_lookup("fix error", section="error_fixes")
        call_args = orch._knowledge_store.search.call_args
        types = call_args[1].get("types") or call_args[0][3] if len(call_args[0]) > 3 else call_args[1].get("types")
        self.assertEqual(types, ["root_cause"])


if __name__ == "__main__":
    unittest.main()
