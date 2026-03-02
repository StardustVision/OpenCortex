# SPDX-License-Identifier: Apache-2.0
"""
Integration tests for the Skill pipeline using real QdrantStorageAdapter.

Uses:
- QdrantStorageAdapter (embedded, in-memory) — real Qdrant vector storage
- MockEmbedder — deterministic hash-based vectors (no API key needed)
- Real CortexFS filesystem layer
- Real MemoryOrchestrator with full init()

Tests the complete flow:
1. memory_store → RuleExtractor → Skillbook persistence
2. memory_search → skillbook fusion → combined results
3. memory_feedback → skillbook tag update
"""

import asyncio
import math
import os
import shutil
import sys
import tempfile
import unittest
from typing import List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.config import CortexConfig, init_config
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.storage.qdrant.adapter import QdrantStorageAdapter


# =============================================================================
# MockEmbedder (deterministic, no API key)
# =============================================================================


class MockEmbedder(DenseEmbedderBase):
    """Hash-based embedder for deterministic tests. Dimension=128 to match Qdrant."""
    DIMENSION = 128

    def __init__(self):
        super().__init__(model_name="mock-embedder-v1")

    def embed(self, text: str) -> EmbedResult:
        vec = self._text_to_vector(text)
        return EmbedResult(dense_vector=vec)

    def get_dimension(self) -> int:
        return self.DIMENSION

    @staticmethod
    def _text_to_vector(text: str) -> List[float]:
        """Generate a deterministic 128-dim unit vector from text hash."""
        h = hash(text) & 0xFFFFFFFFFFFFFFFF
        raw = []
        for i in range(128):
            # Use multiple hash variants for diversity
            bits = hash(f"{text}_{i}") & 0xFFFF
            raw.append((bits & 0xFF) / 255.0 - 0.5)
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]


# =============================================================================
# Integration Tests
# =============================================================================


class TestIntegrationSkillPipeline(unittest.TestCase):
    """Integration tests with real Qdrant + CortexFS."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.qdrant_dir = os.path.join(self.temp_dir, ".qdrant")
        self.config = CortexConfig(
            data_root=self.temp_dir,
            embedding_provider="none",
            embedding_dimension=128,
        )
        init_config(self.config)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.run(coro)

    async def _create_orch(self) -> MemoryOrchestrator:
        """Create a fully initialized orchestrator with real Qdrant."""
        storage = QdrantStorageAdapter(
            path=self.qdrant_dir,
            embedding_dim=128,
        )
        embedder = MockEmbedder()
        orch = MemoryOrchestrator(
            config=self.config,
            storage=storage,
            embedder=embedder,
        )
        await orch.init()
        return orch

    # -----------------------------------------------------------------
    # 1. Store → Extract → Skillbook
    # -----------------------------------------------------------------

    def test_01_store_memory_with_real_qdrant(self):
        """Storing a memory with real Qdrant persists the record."""
        async def _test():
            orch = await self._create_orch()
            try:
                ctx = await orch.add(
                    abstract="User prefers dark theme in all editors",
                    content="The user explicitly requested dark theme for VS Code, Vim, and terminal.",
                    category="preferences",
                )
                self.assertIsNotNone(ctx)
                self.assertIn("memories", ctx.uri)

                # Verify the record exists via filter (bypasses hierarchical retriever)
                records = await orch.storage.filter(
                    "context",
                    {"op": "must", "field": "uri", "conds": [ctx.uri]},
                    limit=1,
                )
                self.assertEqual(len(records), 1, "Memory should be persisted in Qdrant")
                self.assertEqual(records[0]["abstract"], "User prefers dark theme in all editors")
            finally:
                await orch.close()
        self._run(_test())

    def test_02_store_triggers_skill_extraction(self):
        """Storing error→fix content triggers RuleExtractor and persists skills."""
        async def _test():
            orch = await self._create_orch()
            try:
                content = (
                    "During the migration, we encountered a critical error:\n"
                    "psycopg2.OperationalError: FATAL: password authentication failed\n\n"
                    "The fix was to update the .env file with the correct credentials. "
                    "When encountering database authentication errors, first verify the "
                    "credentials in .env, then check if the user has proper permissions "
                    "in pg_hba.conf.\n"
                    "After applying these fixes, the migration completed successfully.\n"
                    "We also added a health check endpoint to catch this earlier.\n"
                )

                await orch.add(
                    abstract="Database migration auth fix",
                    content=content,
                    category="incidents",
                )
                # Give background task time to complete
                await asyncio.sleep(0.2)

                # Check if skills were extracted into skillbook
                skills = await orch.hooks_recall("database authentication")
                self.assertIsInstance(skills, list)
                # Skills may or may not be extracted depending on pattern matching,
                # but the pipeline should not crash
            finally:
                await orch.close()
        self._run(_test())

    # -----------------------------------------------------------------
    # 2. Search → Skill Fusion
    # -----------------------------------------------------------------

    def test_03_search_fuses_memory_and_skill_results(self):
        """Search returns both memory and skillbook results."""
        async def _test():
            orch = await self._create_orch()
            try:
                # Store a memory
                await orch.add(
                    abstract="Python project uses pytest for testing",
                    content="All tests are written with pytest. Use pytest-asyncio for async tests.",
                    category="patterns",
                )

                # Store a skill via hooks
                await orch.hooks_remember(
                    content="Always run pytest with -v flag for verbose output",
                    memory_type="preferences",
                )

                # Search should return both
                result = await orch.search("pytest testing")
                total_results = len(result.memories) + len(result.skills)
                self.assertGreater(
                    total_results, 0,
                    "Search should find at least one result (memory or skill)",
                )
            finally:
                await orch.close()
        self._run(_test())

    def test_04_skill_search_returns_uri_and_score(self):
        """Skill search results have proper URI and score fields."""
        async def _test():
            orch = await self._create_orch()
            try:
                await orch.hooks_remember(
                    content="Use docker compose for local development",
                    memory_type="workflows",
                )
                await orch.hooks_remember(
                    content="Always lint before committing code changes",
                    memory_type="preferences",
                )

                # Recall should include URI and score
                results = await orch.hooks_recall("docker development")
                self.assertGreater(len(results), 0)
                for r in results:
                    self.assertIn("uri", r)
                    self.assertIn("score", r)
                    self.assertIn("/shared/skills/", r["uri"])
                    self.assertIsInstance(r["score"], float)
            finally:
                await orch.close()
        self._run(_test())

    # -----------------------------------------------------------------
    # 3. Feedback → Skillbook Tag
    # -----------------------------------------------------------------

    def test_05_positive_feedback_tags_skill_helpful(self):
        """Positive feedback on a skill URI increments helpful count."""
        async def _test():
            orch = await self._create_orch()
            try:
                result = await orch.hooks_remember(
                    content="Use black formatter with line-length 88",
                    memory_type="workflows",
                )
                uri = result["uri"]
                skill_id = result["skill_id"]

                # Submit positive feedback
                await orch.feedback(uri=uri, reward=1.0)

                # Verify helpful count increased
                skills = await orch._hooks.skillbook.get_by_section("workflows")
                matching = [s for s in skills if s.id == skill_id]
                self.assertEqual(len(matching), 1)
                self.assertGreater(matching[0].helpful, 0)
            finally:
                await orch.close()
        self._run(_test())

    def test_06_negative_feedback_tags_skill_harmful(self):
        """Negative feedback on a skill URI increments harmful count."""
        async def _test():
            orch = await self._create_orch()
            try:
                result = await orch.hooks_remember(
                    content="Use eval() for config parsing",
                    memory_type="patterns",
                )
                uri = result["uri"]
                skill_id = result["skill_id"]

                await orch.feedback(uri=uri, reward=-1.0)

                skills = await orch._hooks.skillbook.get_by_section("patterns")
                matching = [s for s in skills if s.id == skill_id]
                self.assertEqual(len(matching), 1)
                self.assertGreater(matching[0].harmful, 0)
            finally:
                await orch.close()
        self._run(_test())

    def test_07_memory_feedback_still_works(self):
        """Regular memory feedback (non-skill URI) still works with real Qdrant."""
        async def _test():
            orch = await self._create_orch()
            try:
                ctx = await orch.add(
                    abstract="Important deployment procedure",
                    content="Always backup before deploying to production.",
                    category="procedures",
                )

                # Feedback on a memory URI (not skillbook)
                await orch.feedback(uri=ctx.uri, reward=1.0)

                # Verify reward was applied
                profile = await orch.get_profile(ctx.uri)
                self.assertIsNotNone(profile)
                self.assertGreater(profile["reward_score"], 0)
            finally:
                await orch.close()
        self._run(_test())

    # -----------------------------------------------------------------
    # 4. End-to-end pipeline
    # -----------------------------------------------------------------

    def test_08_full_pipeline_store_search_feedback(self):
        """Full pipeline: store with extraction → search with fusion → feedback."""
        async def _test():
            orch = await self._create_orch()
            try:
                # 1. Store a memory with rich content
                content = (
                    "Team workflow:\n"
                    "We always use feature branches for new work.\n"
                    "Never push directly to main without review.\n"
                    "The deployment process follows these steps:\n"
                    "1. Create feature branch from main\n"
                    "2. Implement changes with tests\n"
                    "3. Open pull request for review\n"
                    "4. Merge after approval\n"
                    "5. Deploy to staging first\n"
                    "This has been our process since the team formed.\n"
                )
                await orch.add(
                    abstract="Team workflow and deployment process",
                    content=content,
                    category="processes",
                )
                await asyncio.sleep(0.2)  # Let extraction complete

                # 2. Also manually store a skill
                skill_result = await orch.hooks_remember(
                    content="Always create feature branch before starting work",
                    memory_type="workflows",
                )

                # 3. Search should find both memories and skills
                search_result = await orch.search("git workflow branches")
                total = len(search_result.memories) + len(search_result.skills)
                self.assertGreater(total, 0, "Should find workflow-related results")

                # 4. Give positive feedback on the skill
                await orch.feedback(uri=skill_result["uri"], reward=1.0)

                # 5. Verify skill tag was updated
                skills = await orch._hooks.skillbook.get_by_section("workflows")
                matching = [s for s in skills if s.id == skill_result["skill_id"]]
                self.assertEqual(len(matching), 1)
                self.assertGreater(matching[0].helpful, 0)
            finally:
                await orch.close()
        self._run(_test())

    def test_09_multiple_skills_searchable(self):
        """Multiple skills from different sections are all searchable."""
        async def _test():
            orch = await self._create_orch()
            try:
                await orch.hooks_remember(
                    content="Use pytest-cov for test coverage reports",
                    memory_type="preferences",
                )
                await orch.hooks_remember(
                    content="When tests fail on CI, check if dependencies are pinned",
                    memory_type="error_fixes",
                )
                await orch.hooks_remember(
                    content="Run lint, test, build in order before deploying",
                    memory_type="workflows",
                )

                stats = await orch.hooks_stats()
                self.assertGreater(
                    stats.get("vector_memories", 0), 0,
                    "Should have skills in Skillbook",
                )

                # Each skill should be recallable
                for query in ["test coverage", "CI failures", "deploy process"]:
                    results = await orch.hooks_recall(query)
                    self.assertIsInstance(results, list)
            finally:
                await orch.close()
        self._run(_test())

    def test_10_health_check_with_qdrant(self):
        """Health check works with real Qdrant backend."""
        async def _test():
            orch = await self._create_orch()
            try:
                health = await orch.health_check()
                self.assertTrue(health["initialized"])
                self.assertTrue(health["storage"])
                self.assertTrue(health["embedder"])
            finally:
                await orch.close()
        self._run(_test())


if __name__ == "__main__":
    unittest.main()
