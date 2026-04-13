"""Tests for the probe-first memory ContextManager flow."""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from opencortex.config import CortexConfig, init_config
from opencortex.context.manager import ConversationBuffer
from opencortex.http.request_context import reset_request_identity, set_request_identity
from opencortex.orchestrator import MemoryOrchestrator
from test_e2e_phase1 import InMemoryStorage, MockEmbedder


class TestContextManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="opencortex_ctx_")
        self.config = CortexConfig(
            data_root=self.temp_dir,
            embedding_dimension=MockEmbedder.DIMENSION,
            rerank_provider="disabled",
            merged_event_ttl_hours=48,
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

    def test_prepare_idempotent(self):
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

    def test_recall_mode_never_skips_probe_and_planner(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager
        orch.probe_memory = AsyncMock()
        orch.plan_memory = Mock()

        result = self._run(
            cm.handle(
                session_id="sess_004",
                phase="prepare",
                tenant_id="testteam",
                user_id="alice",
                turn_id="t1",
                messages=[{"role": "user", "content": "test query"}],
                config={"recall_mode": "never"},
            )
        )
        self.assertEqual(result["memory"], [])
        self.assertEqual(result["knowledge"], [])
        self.assertFalse(result["intent"]["should_recall"])
        orch.probe_memory.assert_not_awaited()
        orch.plan_memory.assert_not_called()

        self._run(orch.close())

    def test_prepare_emits_probe_planner_runtime_envelope(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        self._run(
            orch.add(
                abstract="User prefers dark theme in editors",
                category="general",
            )
        )
        cm = orch._context_manager

        result = self._run(
            cm.handle(
                session_id="sess_100",
                phase="prepare",
                tenant_id="testteam",
                user_id="alice",
                turn_id="t1",
                messages=[{"role": "user", "content": "What theme do I prefer?"}],
            )
        )

        self.assertIn("memory_pipeline", result["intent"])
        self.assertIn("probe", result["intent"]["memory_pipeline"])
        self.assertIn("planner", result["intent"]["memory_pipeline"])
        self.assertIn("runtime", result["intent"]["memory_pipeline"])
        self.assertIn("probe_mode", result["intent"]["memory_pipeline"]["runtime"]["trace"])
        self.assertIn("probe_trace", result["intent"]["memory_pipeline"]["runtime"]["trace"])
        self.assertGreaterEqual(
            result["intent"]["probe_candidate_count"],
            1,
        )

        self._run(orch.close())

    def test_merge_buffer_replaces_immediate_records_with_merged_object(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_merge_001"
        sk = cm._make_session_key("testteam", "alice", session_id)

        uri1 = self._run(
            orch._write_immediate(
                session_id=session_id,
                msg_index=0,
                text="我下周二要去杭州出差。",
            )
        )
        uri2 = self._run(
            orch._write_immediate(
                session_id=session_id,
                msg_index=1,
                text="住在西湖边，不吃辣。",
            )
        )
        buffer = cm._conversation_buffers.setdefault(sk, ConversationBuffer())
        buffer.messages = [
            "我下周二要去杭州出差。",
            "住在西湖边，不吃辣。",
        ]
        buffer.immediate_uris = [uri1, uri2]
        buffer.tool_calls_per_turn = []
        buffer.token_count = 1200

        self._run(cm._merge_buffer(sk, session_id, "testteam", "alice"))

        immediate_records = self._run(
            self.storage.filter(
                "context",
                {"op": "must", "field": "uri", "conds": [uri1, uri2]},
                limit=10,
            )
        )
        self.assertEqual(immediate_records, [])

        merged_records = [
            record
            for record in self.storage._records.get("context", {}).values()
            if record.get("meta", {}).get("layer") == "merged"
        ]
        self.assertEqual(len(merged_records), 1)
        self.assertEqual(merged_records[0].get("memory_kind"), "event")
        self.assertIn("abstract_json", merged_records[0])

        self._run(orch.close())


if __name__ == "__main__":
    unittest.main()
