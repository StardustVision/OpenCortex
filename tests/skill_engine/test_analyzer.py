import unittest
from unittest.mock import AsyncMock, MagicMock
from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, SkillStatus, SkillOrigin,
    EvolutionSuggestion, make_source_fingerprint,
)
from opencortex.skill_engine.analyzer import SkillAnalyzer
from opencortex.skill_engine.adapters.source_adapter import MemoryCluster, MemoryRecord


class TestSkillAnalyzer(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.source = AsyncMock()
        self.llm = AsyncMock()
        self.store = AsyncMock()
        self.store.find_by_fingerprint = AsyncMock(return_value=None)
        self.store.load_active = AsyncMock(return_value=[])
        self.analyzer = SkillAnalyzer(
            source=self.source, llm=self.llm, store=self.store,
        )

    def _make_cluster(self):
        return MemoryCluster(
            cluster_id="c1", theme="deployment",
            memory_ids=["m1", "m2", "m3"],
            centroid_embedding=[0.1] * 4, avg_score=0.8,
        )

    def _make_memories(self):
        return [
            MemoryRecord(
                memory_id=f"m{i}", abstract=f"Deploy step {i}",
                overview=f"Overview {i}", content=f"Content {i}",
                context_type="memory", category="events",
            )
            for i in range(1, 4)
        ]

    async def test_skips_cluster_with_existing_fingerprint(self):
        """If fingerprint already exists, skip extraction."""
        self.store.find_by_fingerprint = AsyncMock(
            return_value=MagicMock(skill_id="existing")
        )
        self.source.scan_memories = AsyncMock(return_value=[self._make_cluster()])
        results = await self.analyzer.extract_candidates("t", "u")
        self.assertEqual(len(results), 0)
        self.llm.complete.assert_not_called()

    async def test_calls_llm_for_new_cluster(self):
        """New cluster triggers LLM analysis."""
        cluster = self._make_cluster()
        self.source.scan_memories = AsyncMock(return_value=[cluster])
        self.source.get_cluster_memories = AsyncMock(return_value=self._make_memories())
        self.llm.complete = AsyncMock(return_value='[]')

        results = await self.analyzer.extract_candidates("t", "u")
        self.llm.complete.assert_called_once()

    async def test_parses_llm_suggestions(self):
        """LLM returns valid skill -> produces EvolutionSuggestion."""
        cluster = self._make_cluster()
        self.source.scan_memories = AsyncMock(return_value=[cluster])
        self.source.get_cluster_memories = AsyncMock(return_value=self._make_memories())
        self.llm.complete = AsyncMock(return_value='[{"name": "deploy-flow", "description": "Standard deploy", "category": "workflow", "confidence": 0.9, "content": "# Deploy", "source_memory_ids": ["m1", "m2"]}]')

        results = await self.analyzer.extract_candidates("t", "u")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].evolution_type, SkillOrigin.CAPTURED)
        self.assertEqual(results[0].direction, "deploy-flow")

    async def test_handles_llm_error_gracefully(self):
        """LLM failure returns empty list."""
        cluster = self._make_cluster()
        self.source.scan_memories = AsyncMock(return_value=[cluster])
        self.source.get_cluster_memories = AsyncMock(return_value=self._make_memories())
        self.llm.complete = AsyncMock(side_effect=Exception("LLM down"))

        results = await self.analyzer.extract_candidates("t", "u")
        self.assertEqual(len(results), 0)

    async def test_handles_invalid_json_gracefully(self):
        """Invalid JSON from LLM returns empty list."""
        cluster = self._make_cluster()
        self.source.scan_memories = AsyncMock(return_value=[cluster])
        self.source.get_cluster_memories = AsyncMock(return_value=self._make_memories())
        self.llm.complete = AsyncMock(return_value="not valid json")

        results = await self.analyzer.extract_candidates("t", "u")
        self.assertEqual(len(results), 0)


if __name__ == "__main__":
    unittest.main()
