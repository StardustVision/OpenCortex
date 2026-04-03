import unittest
from unittest.mock import AsyncMock, MagicMock
from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, SkillStatus, SkillOrigin,
    EvolutionSuggestion, SkillLineage,
)
from opencortex.skill_engine.evolver import SkillEvolver


class TestSkillEvolver(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.llm = AsyncMock()
        self.store = AsyncMock()
        self.evolver = SkillEvolver(llm=self.llm, store=self.store)

    async def test_evolve_captured_returns_candidate(self):
        """CAPTURED evolution generates a new skill from LLM output."""
        self.llm.complete = AsyncMock(
            return_value="# Deploy Flow\n\n1. Build\n2. Test\n\n<EVOLUTION_COMPLETE>"
        )
        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.CAPTURED,
            category=SkillCategory.WORKFLOW,
            direction="deploy-flow",
            confidence=0.9,
            source_memory_ids=["m1", "m2"],
        )
        result = await self.evolver.evolve(suggestion, tenant_id="t", user_id="u")
        self.assertIsNotNone(result)
        self.assertEqual(result.status, SkillStatus.CANDIDATE)
        self.assertEqual(result.lineage.origin, SkillOrigin.CAPTURED)
        self.assertIn("Deploy Flow", result.content)

    async def test_evolve_fix_creates_candidate_linked_to_parent(self):
        """FIX creates a new CANDIDATE, not in-place update."""
        parent = SkillRecord(
            skill_id="sk-001", name="old-flow",
            description="Old", content="# Old\n1. Step",
            category=SkillCategory.WORKFLOW, status=SkillStatus.ACTIVE,
            tenant_id="t", user_id="u",
        )
        self.store.load_record = AsyncMock(return_value=parent)
        self.llm.complete = AsyncMock(
            return_value="# Fixed Flow\n\n1. Better Step\n\n<EVOLUTION_COMPLETE>"
        )
        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.FIXED,
            target_skill_ids=["sk-001"],
            category=SkillCategory.WORKFLOW,
            direction="Fix outdated step 1",
        )
        result = await self.evolver.evolve(suggestion, tenant_id="t", user_id="u")
        self.assertIsNotNone(result)
        self.assertEqual(result.status, SkillStatus.CANDIDATE)
        self.assertEqual(result.lineage.origin, SkillOrigin.FIXED)
        self.assertIn("sk-001", result.lineage.parent_skill_ids)
        self.assertNotEqual(result.skill_id, "sk-001")

    async def test_evolve_derived_links_to_parent(self):
        """DERIVED creates enhanced version linked to parent."""
        parent = SkillRecord(
            skill_id="sk-001", name="base-flow",
            description="Base", content="# Base\n1. Step",
            category=SkillCategory.WORKFLOW, status=SkillStatus.ACTIVE,
            tenant_id="t", user_id="u",
            lineage=SkillLineage(generation=0),
        )
        self.store.load_record = AsyncMock(return_value=parent)
        self.llm.complete = AsyncMock(
            return_value="# Enhanced Flow\n\n1. Better\n\n<EVOLUTION_COMPLETE>"
        )
        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.DERIVED,
            target_skill_ids=["sk-001"],
            category=SkillCategory.WORKFLOW,
            direction="enhanced-flow",
        )
        result = await self.evolver.evolve(suggestion, tenant_id="t", user_id="u")
        self.assertIsNotNone(result)
        self.assertEqual(result.lineage.origin, SkillOrigin.DERIVED)
        self.assertEqual(result.lineage.generation, 1)

    async def test_evolve_returns_none_on_failure(self):
        """If LLM returns EVOLUTION_FAILED, return None."""
        self.llm.complete = AsyncMock(
            return_value="<EVOLUTION_FAILED> Reason: too vague"
        )
        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.CAPTURED,
            category=SkillCategory.WORKFLOW,
            direction="vague-pattern",
        )
        result = await self.evolver.evolve(suggestion, tenant_id="t", user_id="u")
        self.assertIsNone(result)

    async def test_evolve_returns_none_on_llm_error(self):
        """If LLM throws, return None."""
        self.llm.complete = AsyncMock(side_effect=Exception("LLM down"))
        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.CAPTURED,
            category=SkillCategory.WORKFLOW,
            direction="some-pattern",
        )
        result = await self.evolver.evolve(suggestion, tenant_id="t", user_id="u")
        self.assertIsNone(result)

    async def test_fix_returns_none_without_target(self):
        """FIX with no target_skill_ids returns None."""
        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.FIXED,
            target_skill_ids=[],
            direction="fix something",
        )
        result = await self.evolver.evolve(suggestion, tenant_id="t", user_id="u")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
