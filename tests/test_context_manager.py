"""
Tests for the Memory Context Protocol ContextManager.

Validates the three-phase lifecycle (prepare/commit/end), idempotency,
fallback, cited_uris RL reward, and session cleanup.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.config import CortexConfig, init_config
from opencortex.http.request_context import set_request_identity, reset_request_identity
from opencortex.orchestrator import MemoryOrchestrator

# Reuse MockEmbedder and InMemoryStorage from e2e tests
from tests.test_e2e_phase1 import MockEmbedder, InMemoryStorage


# =============================================================================
# Test Suite
# =============================================================================

class TestContextManager(unittest.TestCase):
    """Test the ContextManager three-phase lifecycle."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="opencortex_ctx_")
        self.config = CortexConfig(
            data_root=self.temp_dir,
            embedding_dimension=MockEmbedder.DIMENSION,
            rerank_provider="disabled",
        )
        init_config(self.config)
        self._identity_tokens = set_request_identity("testteam", "alice")
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()

    def tearDown(self):
        reset_request_identity(self._identity_tokens)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.run(coro)

    def _make_orchestrator(self):
        return MemoryOrchestrator(
            config=self.config,
            storage=self.storage,
            embedder=self.embedder,
        )

    # -----------------------------------------------------------------
    # 1. Full lifecycle: prepare → commit → end
    # -----------------------------------------------------------------

    def test_01_full_lifecycle(self):
        """prepare → commit → end produces correct responses."""
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager

        # prepare
        result = self._run(cm.handle(
            session_id="sess_001",
            phase="prepare",
            tenant_id="testteam",
            user_id="alice",
            turn_id="t1",
            messages=[{"role": "user", "content": "hello"}],
        ))
        self.assertEqual(result["session_id"], "sess_001")
        self.assertEqual(result["turn_id"], "t1")
        self.assertIn("intent", result)
        self.assertIn("memory", result)
        self.assertIn("knowledge", result)
        self.assertIn("instructions", result)

        # commit
        result = self._run(cm.handle(
            session_id="sess_001",
            phase="commit",
            tenant_id="testteam",
            user_id="alice",
            turn_id="t1",
            messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ],
        ))
        self.assertTrue(result["accepted"])
        self.assertEqual(result["write_status"], "ok")
        self.assertEqual(result["session_turns"], 1)

        # end
        result = self._run(cm.handle(
            session_id="sess_001",
            phase="end",
            tenant_id="testteam",
            user_id="alice",
        ))
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["total_turns"], 1)

        self._run(orch.close())

    # -----------------------------------------------------------------
    # 2. Prepare idempotency
    # -----------------------------------------------------------------

    def test_02_prepare_idempotent(self):
        """Same (session_id, turn_id) returns cached result."""
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager

        args = dict(
            session_id="sess_002",
            phase="prepare",
            tenant_id="testteam",
            user_id="alice",
            turn_id="t1",
            messages=[{"role": "user", "content": "test query"}],
        )

        r1 = self._run(cm.handle(**args))
        r2 = self._run(cm.handle(**args))
        self.assertEqual(r1, r2)

        self._run(orch.close())

    # -----------------------------------------------------------------
    # 3. Commit idempotency
    # -----------------------------------------------------------------

    def test_03_commit_idempotent(self):
        """Same turn_id committed twice returns duplicate."""
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager

        commit_args = dict(
            session_id="sess_003",
            phase="commit",
            tenant_id="testteam",
            user_id="alice",
            turn_id="t1",
            messages=[
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ],
        )

        r1 = self._run(cm.handle(**commit_args))
        self.assertEqual(r1["write_status"], "ok")

        r2 = self._run(cm.handle(**commit_args))
        self.assertEqual(r2["write_status"], "duplicate")

        self._run(orch.close())

    # -----------------------------------------------------------------
    # 4. recall_mode=never skips retrieval
    # -----------------------------------------------------------------

    def test_04_recall_mode_never(self):
        """prepare with recall_mode=never returns empty memory/knowledge."""
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager

        result = self._run(cm.handle(
            session_id="sess_004",
            phase="prepare",
            tenant_id="testteam",
            user_id="alice",
            turn_id="t1",
            messages=[{"role": "user", "content": "test query"}],
            config={"recall_mode": "never"},
        ))
        self.assertEqual(result["memory"], [])
        self.assertEqual(result["knowledge"], [])
        self.assertFalse(result["intent"]["should_recall"])

        self._run(orch.close())

    # -----------------------------------------------------------------
    # 5. recall_mode=always forces retrieval
    # -----------------------------------------------------------------

    def test_05_recall_mode_always(self):
        """prepare with recall_mode=always sets should_recall=True."""
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager

        result = self._run(cm.handle(
            session_id="sess_005",
            phase="prepare",
            tenant_id="testteam",
            user_id="alice",
            turn_id="t1",
            messages=[{"role": "user", "content": "test query"}],
            config={"recall_mode": "always"},
        ))
        self.assertTrue(result["intent"]["should_recall"])

        self._run(orch.close())

    # -----------------------------------------------------------------
    # 6. Commit with cited_uris triggers RL reward
    # -----------------------------------------------------------------

    def test_06_cited_uris_reward(self):
        """commit with cited_uris calls orchestrator.feedback()."""
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager

        # Mock feedback to track calls
        feedback_calls = []
        original_feedback = orch.feedback
        async def mock_feedback(uri, reward):
            feedback_calls.append((uri, reward))
        orch.feedback = mock_feedback

        result = self._run(cm.handle(
            session_id="sess_006",
            phase="commit",
            tenant_id="testteam",
            user_id="alice",
            turn_id="t1",
            messages=[
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ],
            cited_uris=[
                "opencortex://testteam/user/alice/memory/entities/abc",
                "invalid-uri-ignored",
            ],
        ))
        self.assertTrue(result["accepted"])

        # Wait for async reward tasks to complete
        async def wait_pending():
            if cm._pending_tasks:
                await asyncio.gather(*cm._pending_tasks, return_exceptions=True)
        self._run(wait_pending())

        # Only the valid opencortex:// URI should have gotten a reward
        self.assertEqual(len(feedback_calls), 1)
        self.assertEqual(feedback_calls[0][0], "opencortex://testteam/user/alice/memory/entities/abc")
        self.assertAlmostEqual(feedback_calls[0][1], 0.1)

        orch.feedback = original_feedback
        self._run(orch.close())

    # -----------------------------------------------------------------
    # 7. Unknown phase raises ValueError
    # -----------------------------------------------------------------

    def test_07_unknown_phase(self):
        """handle() with unknown phase raises ValueError."""
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager

        with self.assertRaises(ValueError) as ctx:
            self._run(cm.handle(
                session_id="sess_007",
                phase="unknown",
                tenant_id="testteam",
                user_id="alice",
            ))
        self.assertIn("Unknown phase", str(ctx.exception))

        self._run(orch.close())

    # -----------------------------------------------------------------
    # 8. End cleans up all session state
    # -----------------------------------------------------------------

    def test_08_end_cleanup(self):
        """end cleans up prepare cache, committed_turns, session state."""
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager

        sk = ("testteam", "alice", "sess_008")

        # prepare + commit to populate state
        self._run(cm.handle(
            session_id="sess_008", phase="prepare",
            tenant_id="testteam", user_id="alice",
            turn_id="t1",
            messages=[{"role": "user", "content": "q"}],
        ))
        self._run(cm.handle(
            session_id="sess_008", phase="commit",
            tenant_id="testteam", user_id="alice",
            turn_id="t1",
            messages=[
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ],
        ))

        # Verify state exists before end
        self.assertIn(sk, cm._session_activity)
        self.assertIn(sk, cm._committed_turns)

        # end
        self._run(cm.handle(
            session_id="sess_008", phase="end",
            tenant_id="testteam", user_id="alice",
        ))

        # All session state should be cleaned
        self.assertNotIn(sk, cm._session_activity)
        self.assertNotIn(sk, cm._committed_turns)
        self.assertNotIn(sk, cm._session_cache_keys)
        self.assertNotIn(sk, cm._session_locks)

        # Prepare cache entry should also be gone
        cache_key = ("testteam", "alice", "sess_008", "t1")
        self.assertNotIn(cache_key, cm._prepare_cache)

        self._run(orch.close())


if __name__ == "__main__":
    unittest.main()
