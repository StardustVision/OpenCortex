"""
RL integration tests for OpenCortex.

Tests the full reinforcement learning pipeline through the Orchestrator:
  feedback → update_reward → get_profile → decay → protect → search boost

Uses real Volcengine embedding API + embedded Qdrant (no external vector DB).
Requires ~/.openviking/ov.conf with embedding.dense credentials.

Run:
    PYTHONPATH=src uv run python -m unittest tests.test_rl_integration -v
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.config import CortexConfig, init_config
from opencortex.orchestrator import MemoryOrchestrator

# Skip the entire module if ov.conf is missing
_OV_CONF = Path.home() / ".openviking" / "ov.conf"
_HAS_CONF = _OV_CONF.exists()


@unittest.skipUnless(_HAS_CONF, "~/.openviking/ov.conf not found — skipping RL integration tests")
class TestRLIntegration(unittest.TestCase):
    """End-to-end RL tests with real embeddings + Qdrant.

    Uses a single event loop for the entire class so AsyncQdrantClient
    stays bound to the same loop across all test methods.
    """

    _loop: asyncio.AbstractEventLoop
    _tmpdir: str
    _orch: MemoryOrchestrator

    @classmethod
    def setUpClass(cls):
        cls._loop = asyncio.new_event_loop()
        cls._tmpdir = tempfile.mkdtemp(prefix="rl_test_")
        qdrant_path = os.path.join(cls._tmpdir, "qdrant")

        init_config(CortexConfig(
            tenant_id="rl_test_team",
            user_id="rl_tester",
        ))

        from opencortex.models.embedder.volcengine_embedders import (
            create_embedder_from_ov_conf,
        )
        embedder = create_embedder_from_ov_conf()

        from opencortex.storage.qdrant.adapter import QdrantStorageAdapter
        storage = QdrantStorageAdapter(path=qdrant_path, embedding_dim=embedder.get_dimension())

        cls._orch = MemoryOrchestrator(embedder=embedder, storage=storage)
        cls._run(cls._orch.init())

        # Seed two memories with distinct content
        ctx_a = cls._run(cls._orch.add(
            abstract="The user strongly prefers dark theme in all code editors",
            category="preferences",
        ))
        ctx_b = cls._run(cls._orch.add(
            abstract="The project uses Python 3.10 with async/await everywhere",
            category="tech_stack",
        ))
        cls._uri_a = ctx_a if isinstance(ctx_a, str) else ctx_a.uri
        cls._uri_b = ctx_b if isinstance(ctx_b, str) else ctx_b.uri

    @classmethod
    def tearDownClass(cls):
        cls._run(cls._orch.close())
        cls._loop.close()
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    @classmethod
    def _run(cls, coro):
        return cls._loop.run_until_complete(coro)

    def run_async(self, coro):
        return self._loop.run_until_complete(coro)

    # ------------------------------------------------------------------
    # 1. Positive reward
    # ------------------------------------------------------------------
    def test_01_update_reward_positive(self):
        """feedback(+1) x2 → reward_score=2, positive_feedback_count=2."""
        self.run_async(self._orch.feedback(self._uri_a, reward=1.0))
        self.run_async(self._orch.feedback(self._uri_a, reward=1.0))

        profile = self.run_async(self._orch.get_profile(self._uri_a))
        self.assertIsNotNone(profile)
        self.assertAlmostEqual(profile["reward_score"], 2.0, places=2)
        self.assertEqual(profile["positive_feedback_count"], 2)

    # ------------------------------------------------------------------
    # 2. Negative reward
    # ------------------------------------------------------------------
    def test_02_update_reward_negative(self):
        """feedback(-1) → negative_feedback_count=1."""
        self.run_async(self._orch.feedback(self._uri_b, reward=-1.0))

        profile = self.run_async(self._orch.get_profile(self._uri_b))
        self.assertIsNotNone(profile)
        self.assertEqual(profile["negative_feedback_count"], 1)
        self.assertAlmostEqual(profile["reward_score"], -1.0, places=2)

    # ------------------------------------------------------------------
    # 3. Profile completeness
    # ------------------------------------------------------------------
    def test_03_get_profile_complete(self):
        """Profile has all 7 expected fields."""
        profile = self.run_async(self._orch.get_profile(self._uri_a))
        self.assertIsNotNone(profile)
        expected_keys = {
            "id", "reward_score", "retrieval_count",
            "positive_feedback_count", "negative_feedback_count",
            "effective_score", "is_protected",
        }
        self.assertTrue(expected_keys.issubset(profile.keys()))

    # ------------------------------------------------------------------
    # 4. Default profile (no feedback yet)
    # ------------------------------------------------------------------
    def test_04_get_profile_default(self):
        """New memory with no feedback has zero scores and not protected."""
        ctx_c = self.run_async(self._orch.add(
            abstract="A fresh memory with no feedback",
            category="misc",
        ))
        uri_c = ctx_c if isinstance(ctx_c, str) else ctx_c.uri
        profile = self.run_async(self._orch.get_profile(uri_c))
        self.assertIsNotNone(profile)
        self.assertAlmostEqual(profile["reward_score"], 0.0)
        self.assertEqual(profile["positive_feedback_count"], 0)
        self.assertEqual(profile["negative_feedback_count"], 0)
        self.assertFalse(profile["is_protected"])

    # ------------------------------------------------------------------
    # 5. Decay on normal record
    # ------------------------------------------------------------------
    def test_05_apply_decay_normal(self):
        """After decay, reward_score of uri_a should shrink by ~0.95."""
        pre = self.run_async(self._orch.get_profile(self._uri_a))
        pre_score = pre["reward_score"]

        result = self.run_async(self._orch.decay())
        self.assertIsNotNone(result)
        self.assertGreater(result["records_processed"], 0)

        post = self.run_async(self._orch.get_profile(self._uri_a))
        expected = pre_score * 0.95
        self.assertAlmostEqual(post["reward_score"], expected, places=2)

    # ------------------------------------------------------------------
    # 6. Decay on protected record
    # ------------------------------------------------------------------
    def test_06_apply_decay_protected(self):
        """Protected record decays slower (rate=0.99)."""
        # Give uri_b a positive reward to get a non-zero score for testing
        self.run_async(self._orch.feedback(self._uri_b, reward=3.0))
        self.run_async(self._orch.protect(self._uri_b, protected=True))

        pre = self.run_async(self._orch.get_profile(self._uri_b))
        pre_score = pre["reward_score"]
        self.assertTrue(pre["is_protected"])

        self.run_async(self._orch.decay())

        post = self.run_async(self._orch.get_profile(self._uri_b))
        expected = pre_score * 0.99
        self.assertAlmostEqual(post["reward_score"], expected, places=2)

    # ------------------------------------------------------------------
    # 7. Set protected
    # ------------------------------------------------------------------
    def test_07_set_protected(self):
        """protect(True) then get_profile confirms is_protected=True."""
        self.run_async(self._orch.protect(self._uri_a, protected=True))
        profile = self.run_async(self._orch.get_profile(self._uri_a))
        self.assertTrue(profile["is_protected"])

        # Unprotect
        self.run_async(self._orch.protect(self._uri_a, protected=False))
        profile = self.run_async(self._orch.get_profile(self._uri_a))
        self.assertFalse(profile["is_protected"])

    # ------------------------------------------------------------------
    # 8. Search reward boost
    # ------------------------------------------------------------------
    def test_08_search_reward_boost(self):
        """Positive feedback should boost search ranking."""
        # Add two very similar memories
        ctx_x = self.run_async(self._orch.add(
            abstract="Team meeting notes from the project kickoff",
            category="meetings",
        ))
        ctx_y = self.run_async(self._orch.add(
            abstract="Team meeting notes from the sprint planning",
            category="meetings",
        ))
        uri_x = ctx_x if isinstance(ctx_x, str) else ctx_x.uri
        uri_y = ctx_y if isinstance(ctx_y, str) else ctx_y.uri

        # Give uri_y strong positive feedback
        for _ in range(5):
            self.run_async(self._orch.feedback(uri_y, reward=1.0))

        # Search for meeting-related content
        results = self.run_async(self._orch.search("team meeting notes"))
        meeting_uris = [m.uri for m in results.memories]

        # uri_y should appear (and ideally before uri_x due to reward boost)
        self.assertIn(uri_y, meeting_uris, "Boosted memory should appear in results")


if __name__ == "__main__":
    unittest.main()
