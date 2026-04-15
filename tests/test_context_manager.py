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
from opencortex.http.request_context import (
    reset_collection_name,
    reset_request_identity,
    set_collection_name,
    set_request_identity,
)
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

    def _make_orchestrator(self, llm_completion=None):
        return MemoryOrchestrator(
            config=self.config,
            storage=self.storage,
            embedder=self.embedder,
            llm_completion=llm_completion,
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

    def test_prepare_can_recall_memory_written_under_different_session(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        self._run(
            orch.add(
                abstract="我下周二要去杭州出差，住在西湖边。",
                category="events",
                session_id="ingest-session",
            )
        )
        cm = orch._context_manager

        result = self._run(
            cm.handle(
                session_id="query-session",
                phase="prepare",
                tenant_id="testteam",
                user_id="alice",
                turn_id="t1",
                messages=[{"role": "user", "content": "你记得我下周二去哪里出差吗"}],
            )
        )

        self.assertGreaterEqual(result["intent"]["probe_candidate_count"], 1)
        self.assertGreaterEqual(len(result["memory"]), 1)

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
                text="[1 May, 2023] 我下周二要去杭州出差。",
                meta={
                    "event_date": "2023-05-01T09:00:00Z",
                    "time_refs": ["1 May, 2023", "2023-05-01"],
                    "entities": ["杭州"],
                    "topics": ["出差"],
                },
            )
        )
        uri2 = self._run(
            orch._write_immediate(
                session_id=session_id,
                msg_index=1,
                text="住在西湖边，不吃辣。",
                meta={
                    "event_date": "2023-05-01T09:00:00Z",
                    "time_refs": ["1 May, 2023"],
                    "entities": ["西湖"],
                    "topics": ["住宿"],
                },
            )
        )
        buffer = cm._conversation_buffers.setdefault(sk, ConversationBuffer())
        buffer.messages = [
            "[1 May, 2023] 我下周二要去杭州出差。",
            "住在西湖边，不吃辣。",
        ]
        buffer.immediate_uris = [uri1, uri2]
        buffer.tool_calls_per_turn = []
        buffer.token_count = 1200

        self._run(
            cm._merge_buffer(
                sk,
                session_id,
                "testteam",
                "alice",
                flush_all=True,
            )
        )

        immediate_records = self._run(
            self.storage.filter(
                "context",
                {"op": "must", "field": "uri", "conds": [uri1, uri2]},
                limit=10,
            )
        )
        self.assertEqual(immediate_records, [])
        immediate_projection_records = self._run(
            self.storage.filter(
                "context",
                {"op": "prefix", "field": "uri", "prefix": f"{uri1}/anchors"},
                limit=10,
            )
        )
        self.assertEqual(immediate_projection_records, [])
        immediate_projection_records = self._run(
            self.storage.filter(
                "context",
                {"op": "prefix", "field": "uri", "prefix": f"{uri2}/anchors"},
                limit=10,
            )
        )
        self.assertEqual(immediate_projection_records, [])

        merged_records = [
            record
            for record in self.storage._records.get("context", {}).values()
            if record.get("meta", {}).get("layer") == "merged"
        ]
        self.assertEqual(len(merged_records), 1)
        self.assertEqual(merged_records[0].get("memory_kind"), "event")
        self.assertEqual(merged_records[0].get("retrieval_surface"), "l0_object")
        self.assertTrue(merged_records[0].get("anchor_surface"))
        self.assertIn("abstract_json", merged_records[0])
        slots = merged_records[0]["abstract_json"]["slots"]
        self.assertIn("1 May, 2023", slots["time_refs"])
        self.assertIn("杭州", slots["entities"])
        self.assertIn("西湖", slots["entities"])
        self.assertIn("出差", slots["topics"])
        self.assertIn("住宿", slots["topics"])

        self._run(orch.close())

    def test_commit_persists_message_meta_into_immediate_records(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_commit_001"
        sk = cm._make_session_key("testteam", "alice", session_id)

        self._run(
            cm.handle(
                session_id=session_id,
                phase="commit",
                tenant_id="testteam",
                user_id="alice",
                turn_id="t1",
                messages=[
                    {
                        "role": "user",
                        "content": "[Alice]: 我搬到了杭州。",
                        "meta": {
                            "speaker": "Alice",
                            "event_date": "2023-05-01T09:00:00Z",
                            "time_refs": ["1 May, 2023", "2023-05-01"],
                        },
                    },
                    {
                        "role": "assistant",
                        "content": "记住了。",
                    },
                ],
            )
        )

        buffer = cm._conversation_buffers[sk]
        self.assertTrue(buffer.messages[0].startswith("[1 May, 2023] [Alice]:"))

        immediate_records = [
            record
            for record in self.storage._records.get("context", {}).values()
            if record.get("meta", {}).get("layer") == "immediate"
        ]
        anchor_projection_records = [
            record
            for record in self.storage._records.get("context", {}).values()
            if record.get("retrieval_surface") == "anchor_projection"
        ]
        self.assertEqual(len(immediate_records), 2)
        self.assertGreaterEqual(len(anchor_projection_records), 1)
        self.assertTrue(all(record.get("retrieval_surface") == "l0_object" for record in immediate_records))
        user_record = next(
            record
            for record in immediate_records
            if record.get("meta", {}).get("msg_index") == 0
        )
        self.assertTrue(user_record.get("anchor_surface"))
        self.assertEqual(user_record["event_date"], "2023-05-01T09:00:00Z")
        self.assertEqual(user_record["speaker"], "Alice")
        self.assertIn(
            "1 May, 2023",
            user_record["abstract_json"]["slots"]["time_refs"],
        )

        self._run(orch.close())

    def test_merge_cleanup_uses_active_collection_override(self):
        collection_token = set_collection_name("bench_ctx")
        orch = None
        try:
            orch = self._make_orchestrator()
            self._run(orch.init())
            cm = orch._context_manager
            session_id = "sess_merge_002"
            sk = cm._make_session_key("testteam", "alice", session_id)

            uri = self._run(
                orch._write_immediate(
                    session_id=session_id,
                    msg_index=0,
                    text="[1 May, 2023] 我下周二要去杭州出差。",
                    meta={
                        "event_date": "2023-05-01T09:00:00Z",
                        "time_refs": ["1 May, 2023"],
                    },
                )
            )
            buffer = cm._conversation_buffers.setdefault(sk, ConversationBuffer())
            buffer.messages = ["[1 May, 2023] 我下周二要去杭州出差。"]
            buffer.immediate_uris = [uri]
            buffer.token_count = 1200

            self._run(
                cm._merge_buffer(
                    sk,
                    session_id,
                    "testteam",
                    "alice",
                    flush_all=True,
                )
            )

            remaining = self._run(
                self.storage.filter(
                    "bench_ctx",
                    {"op": "must", "field": "uri", "conds": [uri]},
                    limit=10,
                )
            )
            self.assertEqual(remaining, [])
            self.assertNotIn("context", self.storage._records)
        finally:
            if orch is not None:
                self._run(orch.close())
            reset_collection_name(collection_token)

    def test_derive_layers_chunked_llm_blank_abstract_falls_back_to_content(self):
        async def blank_llm(prompt: str) -> str:
            if "compress" in prompt.lower():
                return "   "
            return '{"abstract":"   ","overview":"   ","keywords":[],"entities":[]}'

        orch = self._make_orchestrator(llm_completion=blank_llm)
        self._run(orch.init())
        long_content = ("杭州出差，住在西湖边。\n" * 400).strip()

        layers = self._run(orch._derive_layers("", long_content))

        self.assertEqual(layers["abstract"], long_content)
        self.assertEqual(layers["overview"], "")
        self._run(orch.close())

    def test_end_keeps_merged_record_visible_in_memory_list_when_llm_returns_blank(self):
        async def blank_llm(prompt: str) -> str:
            if "compress" in prompt.lower():
                return "   "
            return '{"abstract":"   ","overview":"   ","keywords":[],"entities":[]}'

        orch = self._make_orchestrator(llm_completion=blank_llm)
        self._run(orch.init())
        cm = orch._context_manager
        repeated_fact = "我下周二要去杭州出差，住在西湖边，不吃辣。"
        long_message = repeated_fact * 250

        self._run(
            cm.handle(
                session_id="sess_merge_list_001",
                phase="commit",
                tenant_id="testteam",
                user_id="alice",
                turn_id="t1",
                messages=[
                    {"role": "user", "content": long_message},
                    {"role": "assistant", "content": "记住了。"},
                ],
            )
        )
        self._run(
            cm.handle(
                session_id="sess_merge_list_001",
                phase="end",
                tenant_id="testteam",
                user_id="alice",
            )
        )

        items = self._run(
            orch.list_memories(
                category="events",
                context_type="memory",
                limit=10,
                offset=0,
                include_payload=True,
            )
        )

        session_items = [
            item for item in items if item.get("session_id") == "sess_merge_list_001"
        ]

        self.assertGreaterEqual(len(session_items), 1)
        self.assertTrue(
            any(item.get("abstract", "").startswith("我下周二要去杭州出差") for item in session_items)
        )
        self._run(orch.close())

    def test_end_waits_for_background_merge_and_keeps_single_merged_record(self):
        async def _case():
            orch = self._make_orchestrator()
            await orch.init()
            cm = orch._context_manager
            session_id = "sess_merge_async_001"
            sk = cm._make_session_key("testteam", "alice", session_id)
            cm._estimate_tokens = lambda _text: 1200

            original_add = orch.add
            merge_started = asyncio.Event()
            allow_merge = asyncio.Event()

            async def slow_add(*args, **kwargs):
                merge_started.set()
                await allow_merge.wait()
                return await original_add(*args, **kwargs)

            orch.add = AsyncMock(side_effect=slow_add)

            await cm.handle(
                session_id=session_id,
                phase="commit",
                tenant_id="testteam",
                user_id="alice",
                turn_id="t1",
                messages=[
                    {"role": "user", "content": "我下周二要去杭州出差。"},
                    {"role": "assistant", "content": "记住了，你住在西湖边。"},
                ],
            )

            immediate_uris = [
                record["uri"]
                for record in self.storage._records.get("context", {}).values()
                if record.get("meta", {}).get("layer") == "immediate"
            ]
            self.assertIn(sk, cm._session_merge_tasks)
            await asyncio.wait_for(merge_started.wait(), timeout=1.0)

            end_task = asyncio.create_task(
                cm.handle(
                    session_id=session_id,
                    phase="end",
                    tenant_id="testteam",
                    user_id="alice",
                )
            )
            await asyncio.sleep(0.05)
            self.assertFalse(end_task.done())

            allow_merge.set()
            result = await asyncio.wait_for(end_task, timeout=2.0)
            self.assertEqual(result["status"], "closed")

            merged_records = [
                record
                for record in self.storage._records.get("context", {}).values()
                if record.get("meta", {}).get("layer") == "merged"
            ]
            immediate_records = [
                record
                for record in self.storage._records.get("context", {}).values()
                if record.get("meta", {}).get("layer") == "immediate"
            ]
            self.assertEqual(len(merged_records), 1)
            self.assertEqual(immediate_records, [])
            self.assertFalse(
                any(
                    record.get("uri", "").startswith(f"{uri}/anchors")
                    for uri in immediate_uris
                    for record in self.storage._records.get("context", {}).values()
                )
            )
            self.assertNotIn(sk, cm._session_merge_tasks)

            await orch.close()

        self._run(_case())

    def test_failed_background_merge_is_restored_and_flushed_on_end(self):
        async def _case():
            orch = self._make_orchestrator()
            await orch.init()
            cm = orch._context_manager
            session_id = "sess_merge_async_002"
            sk = cm._make_session_key("testteam", "alice", session_id)
            cm._estimate_tokens = lambda _text: 1200

            original_add = orch.add
            first_call = True

            async def flaky_add(*args, **kwargs):
                nonlocal first_call
                if first_call:
                    first_call = False
                    raise RuntimeError("merge boom")
                return await original_add(*args, **kwargs)

            orch.add = AsyncMock(side_effect=flaky_add)

            await cm.handle(
                session_id=session_id,
                phase="commit",
                tenant_id="testteam",
                user_id="alice",
                turn_id="t1",
                messages=[
                    {"role": "user", "content": "我搬到了杭州。"},
                    {"role": "assistant", "content": "记住了，你现在住在杭州。"},
                ],
            )

            immediate_uris = [
                record["uri"]
                for record in self.storage._records.get("context", {}).values()
                if record.get("meta", {}).get("layer") == "immediate"
            ]
            await cm._wait_for_merge_task(sk)
            restored_buffer = cm._conversation_buffers.get(sk)
            self.assertIsNotNone(restored_buffer)
            self.assertGreater(restored_buffer.token_count, 0)
            self.assertGreater(len(restored_buffer.messages), 0)

            result = await cm.handle(
                session_id=session_id,
                phase="end",
                tenant_id="testteam",
                user_id="alice",
            )
            self.assertEqual(result["status"], "closed")

            merged_records = [
                record
                for record in self.storage._records.get("context", {}).values()
                if record.get("meta", {}).get("layer") == "merged"
            ]
            self.assertEqual(len(merged_records), 1)
            self.assertFalse(
                any(
                    record.get("uri", "").startswith(f"{uri}/anchors")
                    for uri in immediate_uris
                    for record in self.storage._records.get("context", {}).values()
                )
            )
            self.assertNotIn(sk, cm._conversation_buffers)

            await orch.close()

        self._run(_case())


if __name__ == "__main__":
    unittest.main()
