"""Tests for the probe-first memory ContextManager flow."""

import asyncio
import httpx
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
from opencortex.http.models import ContextPrepareResponse
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
        self.assertEqual(r1["session_id"], r2["session_id"])
        self.assertEqual(r1["turn_id"], r2["turn_id"])
        self.assertEqual(r1["memory"], r2["memory"])
        self.assertEqual(r1["knowledge"], r2["knowledge"])
        self.assertEqual(r1["instructions"], r2["instructions"])
        self.assertFalse(
            r1["intent"]["memory_pipeline"]["runtime"]["trace"].get("cache_hit", False)
        )
        self.assertTrue(
            r2["intent"]["memory_pipeline"]["runtime"]["trace"]["cache_hit"]
        )

        self._run(orch.close())

    def test_prepare_cache_hit_rewrites_stage_timing(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        self._run(
            orch.add(
                abstract="User prefers dark theme in editors",
                category="general",
            )
        )
        cm = orch._context_manager

        args = dict(
            session_id="sess_cache",
            phase="prepare",
            tenant_id="testteam",
            user_id="alice",
            turn_id="t1",
            messages=[{"role": "user", "content": "What theme do I prefer?"}],
        )

        first = self._run(cm.handle(**args))
        first["intent"]["memory_pipeline"]["runtime"]["trace"]["stage_timing_ms"][
            "total"
        ] = 999

        cached = self._run(cm.handle(**args))
        cache_trace = cached["intent"]["memory_pipeline"]["runtime"]["trace"]

        self.assertNotEqual(
            cache_trace["stage_timing_ms"]["total"],
            999,
        )
        self.assertEqual(
            cache_trace["stage_timing_ms"]["overhead"],
            cache_trace["stage_timing_ms"]["total"],
        )
        self.assertTrue(cache_trace["cache_hit"])

        self._run(orch.close())

    def test_prepare_cache_isolated_by_collection(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        self._run(
            self.storage.create_collection(
                "bench_ctx_a", {"vector_dim": MockEmbedder.DIMENSION}
            )
        )
        self._run(
            self.storage.create_collection(
                "bench_ctx_b", {"vector_dim": MockEmbedder.DIMENSION}
            )
        )
        cm = orch._context_manager

        collection_token = set_collection_name("bench_ctx_a")
        try:
            self._run(
                orch.add(
                    abstract="alpha-cache-token-hangzhou",
                    category="general",
                )
            )
            first = self._run(
                cm.handle(
                    session_id="shared-prepare-session",
                    phase="prepare",
                    tenant_id="testteam",
                    user_id="alice",
                    turn_id="shared-turn",
                    messages=[
                        {
                            "role": "user",
                            "content": "alpha-cache-token-hangzhou",
                        }
                    ],
                )
            )
        finally:
            reset_collection_name(collection_token)

        collection_token = set_collection_name("bench_ctx_b")
        try:
            self._run(
                orch.add(
                    abstract="beta-cache-token-beijing",
                    category="general",
                )
            )
            second = self._run(
                cm.handle(
                    session_id="shared-prepare-session",
                    phase="prepare",
                    tenant_id="testteam",
                    user_id="alice",
                    turn_id="shared-turn",
                    messages=[
                        {
                            "role": "user",
                            "content": "beta-cache-token-beijing",
                        }
                    ],
                )
            )
        finally:
            reset_collection_name(collection_token)

        self.assertGreaterEqual(len(first["memory"]), 1)
        self.assertGreaterEqual(len(second["memory"]), 1)
        self.assertEqual(first["memory"][0]["abstract"], "alpha-cache-token-hangzhou")
        self.assertEqual(second["memory"][0]["abstract"], "beta-cache-token-beijing")
        self.assertFalse(
            first["intent"]["memory_pipeline"]["runtime"]["trace"].get(
                "cache_hit", False
            )
        )
        self.assertFalse(
            second["intent"]["memory_pipeline"]["runtime"]["trace"].get(
                "cache_hit", False
            )
        )

        self._run(orch.close())

    def test_idle_auto_close_supports_collection_scoped_session_key(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager
        cm._session_idle_ttl = 0.0
        cm._idle_check_interval = 0.01

        session_key = ("bench_ctx_idle", "testteam", "alice", "sess_idle_001")
        cm._session_activity[session_key] = 0.0
        closed_calls = []

        async def fake_end(session_id, tenant_id, user_id):
            closed_calls.append((session_id, tenant_id, user_id))
            cm._session_activity.pop(session_key, None)
            return {"status": "closed"}

        cm._end = AsyncMock(side_effect=fake_end)

        async def _case():
            await cm.start()
            try:
                await asyncio.sleep(0.05)
            finally:
                await cm.close()

        self._run(_case())

        self.assertEqual(
            closed_calls,
            [("sess_idle_001", "testteam", "alice")],
        )
        self.assertNotIn(session_key, cm._session_activity)

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
        parsed = ContextPrepareResponse.model_validate(result)

        self.assertIn("memory_pipeline", result["intent"])
        self.assertIn("probe", result["intent"]["memory_pipeline"])
        self.assertIn("planner", result["intent"]["memory_pipeline"])
        self.assertIn("runtime", result["intent"]["memory_pipeline"])
        self.assertEqual(parsed.turn_id, "t1")
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

    def test_merge_buffer_splits_clear_temporal_segments(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_merge_segments_001"
        sk = cm._make_session_key("testteam", "alice", session_id)

        items = [
            (
                0,
                "[1 May, 2023] [Alice]: 我搬到了杭州。",
                {
                    "speaker": "Alice",
                    "event_date": "2023-05-01T09:00:00Z",
                    "time_refs": ["1 May, 2023", "2023-05-01"],
                    "entities": ["杭州"],
                    "topics": ["搬家"],
                },
            ),
            (
                1,
                "[1 May, 2023] [Bob]: 你住在西湖边。",
                {
                    "speaker": "Bob",
                    "event_date": "2023-05-01T09:00:00Z",
                    "time_refs": ["1 May, 2023", "2023-05-01"],
                    "entities": ["西湖"],
                    "topics": ["住处"],
                },
            ),
            (
                2,
                "[3 May, 2023] [Alice]: 我下周去上海见客户。",
                {
                    "speaker": "Alice",
                    "event_date": "2023-05-03T10:00:00Z",
                    "time_refs": ["3 May, 2023", "2023-05-03"],
                    "entities": ["上海"],
                    "topics": ["出差"],
                },
            ),
            (
                3,
                "[3 May, 2023] [Bob]: 记得带电脑。",
                {
                    "speaker": "Bob",
                    "event_date": "2023-05-03T10:00:00Z",
                    "time_refs": ["3 May, 2023", "2023-05-03"],
                    "entities": ["电脑"],
                    "topics": ["提醒"],
                },
            ),
        ]

        buffer = cm._conversation_buffers.setdefault(sk, ConversationBuffer())
        for msg_index, text, meta in items:
            uri = self._run(
                orch._write_immediate(
                    session_id=session_id,
                    msg_index=msg_index,
                    text=text,
                    meta=meta,
                )
            )
            buffer.messages.append(text)
            buffer.immediate_uris.append(uri)
            buffer.token_count += 300

        self._run(
            cm._merge_buffer(
                sk,
                session_id,
                "testteam",
                "alice",
                flush_all=True,
            )
        )

        merged_records = sorted(
            [
                record
                for record in self.storage._records.get("context", {}).values()
                if record.get("meta", {}).get("layer") == "merged"
            ],
            key=lambda record: record.get("meta", {}).get("msg_range", [999, 999])[0],
        )
        self.assertEqual(len(merged_records), 2)
        self.assertEqual(merged_records[0]["meta"]["msg_range"], [0, 1])
        self.assertEqual(merged_records[1]["meta"]["msg_range"], [2, 3])

        self._run(orch.close())

    def test_merge_buffer_splits_same_day_distinct_specific_time_refs(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_merge_same_day_split_001"
        sk = cm._make_session_key("testteam", "alice", session_id)

        items = [
            (
                0,
                "[2023-05-01] [Alice]: 周三下午三点我和 Caroline 在阿里西溪园区开会。",
                {
                    "speaker": "Alice",
                    "event_date": "2023-05-01",
                    "time_refs": [
                        "2023-05-01",
                        "2023-05-01 afternoon meeting",
                        "周三下午三点",
                    ],
                    "entities": ["Caroline", "阿里西溪园区"],
                    "topics": ["会议"],
                },
            ),
            (
                1,
                "[2023-05-01] [Bob]: 好的，周三下午三点在阿里西溪园区开会。",
                {
                    "speaker": "Bob",
                    "event_date": "2023-05-01",
                    "time_refs": [
                        "2023-05-01",
                        "2023-05-01 afternoon meeting",
                        "周三下午三点",
                    ],
                    "entities": ["Caroline", "阿里西溪园区"],
                    "topics": ["会议"],
                },
            ),
            (
                2,
                "[2023-05-01] [Alice]: 周三晚上 Melanie 和我在湖滨银泰吃饭。",
                {
                    "speaker": "Alice",
                    "event_date": "2023-05-01",
                    "time_refs": [
                        "2023-05-01",
                        "2023-05-01 evening dinner",
                        "周三晚上",
                    ],
                    "entities": ["Melanie", "湖滨银泰"],
                    "topics": ["晚饭"],
                },
            ),
            (
                3,
                "[2023-05-01] [Bob]: 记住了，周三晚上在湖滨银泰吃饭。",
                {
                    "speaker": "Bob",
                    "event_date": "2023-05-01",
                    "time_refs": [
                        "2023-05-01",
                        "2023-05-01 evening dinner",
                        "周三晚上",
                    ],
                    "entities": ["Melanie", "湖滨银泰"],
                    "topics": ["晚饭"],
                },
            ),
        ]

        buffer = cm._conversation_buffers.setdefault(sk, ConversationBuffer())
        for msg_index, text, meta in items:
            uri = self._run(
                orch._write_immediate(
                    session_id=session_id,
                    msg_index=msg_index,
                    text=text,
                    meta=meta,
                )
            )
            buffer.messages.append(text)
            buffer.immediate_uris.append(uri)
            buffer.token_count += 300

        self._run(
            cm._merge_buffer(
                sk,
                session_id,
                "testteam",
                "alice",
                flush_all=True,
            )
        )

        merged_records = sorted(
            [
                record
                for record in self.storage._records.get("context", {}).values()
                if record.get("meta", {}).get("layer") == "merged"
            ],
            key=lambda record: record.get("meta", {}).get("msg_range", [999, 999])[0],
        )
        self.assertEqual(len(merged_records), 2)
        self.assertEqual(merged_records[0]["meta"]["msg_range"], [0, 1])
        self.assertEqual(merged_records[1]["meta"]["msg_range"], [2, 3])

        self._run(orch.close())

    def test_merge_buffer_recomposes_tail_and_replaces_superseded_merged_leaf(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_merge_tail_001"
        sk = cm._make_session_key("testteam", "alice", session_id)
        source_uri = cm._conversation_source_uri("testteam", "alice", session_id)

        old_merged = self._run(
            orch.add(
                abstract="",
                content="我下周二去杭州出差。",
                category="events",
                context_type="memory",
                session_id=session_id,
                meta={
                    "layer": "merged",
                    "ingest_mode": "memory",
                    "msg_range": [0, 1],
                    "source_uri": source_uri,
                    "session_id": session_id,
                    "recomposition_stage": "online_tail",
                    "time_refs": ["2023-05-01"],
                    "entities": ["杭州"],
                },
            )
        )

        uri2 = self._run(
            orch._write_immediate(
                session_id=session_id,
                msg_index=2,
                text="[2023-05-01] 我住在西湖边。",
                meta={
                    "event_date": "2023-05-01T10:00:00Z",
                    "time_refs": ["2023-05-01"],
                    "entities": ["西湖"],
                    "topics": ["住宿"],
                },
            )
        )
        uri3 = self._run(
            orch._write_immediate(
                session_id=session_id,
                msg_index=3,
                text="[2023-05-01] 我不吃辣。",
                meta={
                    "event_date": "2023-05-01T10:00:00Z",
                    "time_refs": ["2023-05-01"],
                    "entities": ["饮食"],
                    "topics": ["偏好"],
                },
            )
        )

        buffer = cm._conversation_buffers.setdefault(sk, ConversationBuffer())
        buffer.messages = ["[2023-05-01] 我住在西湖边。", "[2023-05-01] 我不吃辣。"]
        buffer.immediate_uris = [uri2, uri3]
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

        merged_records = [
            record
            for record in self.storage._records.get("context", {}).values()
            if record.get("meta", {}).get("layer") == "merged"
            and record.get("session_id") == session_id
        ]
        merged_uris = {record.get("uri") for record in merged_records}
        self.assertNotIn(old_merged.uri, merged_uris)
        self.assertTrue(merged_records)
        self.assertTrue(
            all(
                record.get("meta", {}).get("recomposition_stage") == "online_tail"
                for record in merged_records
            )
        )

        self._run(orch.close())

    def test_full_session_recomposition_replaces_merged_set(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_full_recompose_001"
        source_uri = cm._conversation_source_uri("testteam", "alice", session_id)

        first = self._run(
            orch.add(
                abstract="",
                content="第一段对话：我搬到了杭州。",
                category="events",
                context_type="memory",
                session_id=session_id,
                meta={
                    "layer": "merged",
                    "ingest_mode": "memory",
                    "msg_range": [0, 0],
                    "source_uri": source_uri,
                    "session_id": session_id,
                    "recomposition_stage": "online_tail",
                    "time_refs": ["2023-05-01"],
                    "entities": ["杭州"],
                },
            )
        )
        second = self._run(
            orch.add(
                abstract="",
                content="第二段对话：我住在西湖边。",
                category="events",
                context_type="memory",
                session_id=session_id,
                meta={
                    "layer": "merged",
                    "ingest_mode": "memory",
                    "msg_range": [1, 1],
                    "source_uri": source_uri,
                    "session_id": session_id,
                    "recomposition_stage": "online_tail",
                    "time_refs": ["2023-05-01"],
                    "entities": ["西湖"],
                },
            )
        )

        self._run(
            cm._run_full_session_recomposition(
                session_id=session_id,
                tenant_id="testteam",
                user_id="alice",
                source_uri=source_uri,
            )
        )

        merged_records = [
            record
            for record in self.storage._records.get("context", {}).values()
            if record.get("meta", {}).get("layer") == "merged"
            and record.get("session_id") == session_id
        ]
        merged_uris = {record.get("uri") for record in merged_records}
        self.assertNotIn(first.uri, merged_uris)
        self.assertNotIn(second.uri, merged_uris)
        self.assertTrue(merged_records)
        self.assertTrue(
            all(
                record.get("meta", {}).get("recomposition_stage") == "final_full"
                for record in merged_records
            )
        )

        self._run(orch.close())

    def test_full_session_recomposition_reuses_stable_uri_without_self_deletion(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_full_recompose_stable_uri_001"
        source_uri = cm._conversation_source_uri("testteam", "alice", session_id)
        first_uri = cm._merged_leaf_uri("testteam", "alice", session_id, [0, 1])
        second_uri = cm._merged_leaf_uri("testteam", "alice", session_id, [2, 3])

        self._run(
            orch.add(
                uri=first_uri,
                abstract="",
                content="第一段对话：我搬到了杭州。\n\n补充：我住在西湖边。",
                category="events",
                context_type="memory",
                session_id=session_id,
                meta={
                    "layer": "merged",
                    "ingest_mode": "memory",
                    "msg_range": [0, 1],
                    "source_uri": source_uri,
                    "session_id": session_id,
                    "recomposition_stage": "online_tail",
                    "time_refs": ["2023-05-01"],
                    "entities": ["杭州"],
                },
            )
        )
        self._run(
            orch.add(
                uri=second_uri,
                abstract="",
                content="第二段对话：我下周三去上海。\n\n补充：我要住在浦东。",
                category="events",
                context_type="memory",
                session_id=session_id,
                meta={
                    "layer": "merged",
                    "ingest_mode": "memory",
                    "msg_range": [2, 3],
                    "source_uri": source_uri,
                    "session_id": session_id,
                    "recomposition_stage": "online_tail",
                    "time_refs": ["2023-05-03"],
                    "entities": ["上海"],
                },
            )
        )

        self._run(
            cm._run_full_session_recomposition(
                session_id=session_id,
                tenant_id="testteam",
                user_id="alice",
                source_uri=source_uri,
            )
        )

        merged_records = sorted(
            [
                record
                for record in self.storage._records.get("context", {}).values()
                if record.get("meta", {}).get("layer") == "merged"
                and record.get("session_id") == session_id
            ],
            key=lambda record: record.get("meta", {}).get("msg_range", [999, 999])[0],
        )
        self.assertEqual(
            [record.get("uri") for record in merged_records],
            [first_uri, second_uri],
        )
        self.assertTrue(
            all(
                record.get("meta", {}).get("recomposition_stage") == "final_full"
                for record in merged_records
            )
        )

        self._run(orch.close())

    def test_end_persists_conversation_source_and_links_merged_leaf(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_source_001"

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
                        "content": "我搬到了杭州。",
                        "meta": {
                            "speaker": "Alice",
                            "event_date": "2023-05-01T09:00:00Z",
                            "time_refs": ["1 May, 2023"],
                        },
                    },
                    {"role": "assistant", "content": "记住了。"},
                ],
            )
        )

        result = self._run(
            cm.handle(
                session_id=session_id,
                phase="end",
                tenant_id="testteam",
                user_id="alice",
            )
        )

        self.assertEqual(result["status"], "closed")
        source_uri = result["source_uri"]
        self.assertTrue(source_uri)

        source_records = [
            record
            for record in self.storage._records.get("context", {}).values()
            if record.get("uri") == source_uri
        ]
        self.assertEqual(len(source_records), 1)
        self.assertEqual(
            source_records[0].get("meta", {}).get("layer"),
            "conversation_source",
        )
        rendered_source = self._run(
            orch._fs.read_file(f"{source_uri}/content.md")
        )
        self.assertIn("我搬到了杭州。", rendered_source)

        merged_records = [
            record
            for record in self.storage._records.get("context", {}).values()
            if record.get("meta", {}).get("layer") == "merged"
        ]
        self.assertEqual(len(merged_records), 1)
        self.assertEqual(merged_records[0]["meta"]["source_uri"], source_uri)
        self.assertEqual(merged_records[0]["meta"]["msg_range"], [0, 1])

        items = self._run(
            orch.list_memories(
                category="events",
                context_type="memory",
                limit=10,
                offset=0,
                include_payload=True,
            )
        )
        merged_item = next(
            item
            for item in items
            if item.get("source_uri") == source_uri and item.get("msg_range") == [0, 1]
        )
        self.assertIn("overview", merged_item)
        self.assertEqual(merged_item["source_uri"], source_uri)
        self.assertEqual(merged_item["msg_range"], [0, 1])
        self.assertIn(
            merged_item["recomposition_stage"],
            {"online_tail", "final_full"},
        )

        self._run(orch.close())

    def test_end_returns_partial_when_source_persistence_fails(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_source_fail_001"

        self._run(
            cm.handle(
                session_id=session_id,
                phase="commit",
                tenant_id="testteam",
                user_id="alice",
                turn_id="t1",
                messages=[
                    {"role": "user", "content": "我下周去上海。"},
                    {"role": "assistant", "content": "记住了。"},
                ],
            )
        )

        original_add = orch.add

        async def flaky_add(*args, **kwargs):
            meta = kwargs.get("meta") or {}
            if meta.get("layer") == "conversation_source":
                raise RuntimeError("source boom")
            return await original_add(*args, **kwargs)

        orch.add = AsyncMock(side_effect=flaky_add)

        result = self._run(
            cm.handle(
                session_id=session_id,
                phase="end",
                tenant_id="testteam",
                user_id="alice",
            )
        )

        self.assertEqual(result["status"], "partial")
        self.assertGreater(
            len(
                orch._observer.get_transcript(
                    orch._observer_session_id(
                        session_id,
                        tenant_id="testteam",
                        user_id="alice",
                    )
                )
            ),
            0,
        )

        self._run(orch.close())

    def test_commit_and_transcript_are_isolated_by_collection(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        self._run(
            self.storage.create_collection(
                "bench_ctx_a", {"vector_dim": MockEmbedder.DIMENSION}
            )
        )
        self._run(
            self.storage.create_collection(
                "bench_ctx_b", {"vector_dim": MockEmbedder.DIMENSION}
            )
        )
        cm = orch._context_manager
        session_id = "shared-collection-session"

        collection_token = set_collection_name("bench_ctx_a")
        try:
            result_a = self._run(
                cm.handle(
                    session_id=session_id,
                    phase="commit",
                    tenant_id="testteam",
                    user_id="alice",
                    turn_id="shared-turn",
                    messages=[
                        {"role": "user", "content": "alpha transcript only"},
                        {"role": "assistant", "content": "记住 alpha"},
                    ],
                )
            )
            sk_a = cm._make_session_key("testteam", "alice", session_id)
            source_uri_a = self._run(
                cm._persist_conversation_source(
                    session_id=session_id,
                    tenant_id="testteam",
                    user_id="alice",
                )
            )
        finally:
            reset_collection_name(collection_token)

        collection_token = set_collection_name("bench_ctx_b")
        try:
            result_b = self._run(
                cm.handle(
                    session_id=session_id,
                    phase="commit",
                    tenant_id="testteam",
                    user_id="alice",
                    turn_id="shared-turn",
                    messages=[
                        {"role": "user", "content": "beta transcript only"},
                        {"role": "assistant", "content": "记住 beta"},
                    ],
                )
            )
            sk_b = cm._make_session_key("testteam", "alice", session_id)
        finally:
            reset_collection_name(collection_token)

        source_records = self._run(
            self.storage.filter(
                "bench_ctx_a",
                {"op": "must", "field": "uri", "conds": [source_uri_a]},
                limit=1,
            )
        )
        rendered_source = self._run(orch._fs.read_file(f"{source_uri_a}/content.md"))

        self.assertEqual(result_a["write_status"], "ok")
        self.assertEqual(result_b["write_status"], "ok")
        self.assertEqual(result_a["session_turns"], 1)
        self.assertEqual(result_b["session_turns"], 1)
        self.assertNotEqual(sk_a, sk_b)
        self.assertEqual(cm._committed_turns[sk_a], {"shared-turn"})
        self.assertEqual(cm._committed_turns[sk_b], {"shared-turn"})
        self.assertEqual(len(cm._conversation_buffers[sk_a].messages), 2)
        self.assertEqual(len(cm._conversation_buffers[sk_b].messages), 2)
        self.assertEqual(len(source_records), 1)
        self.assertEqual(
            source_records[0].get("meta", {}).get("layer"),
            "conversation_source",
        )
        self.assertIn("alpha transcript only", rendered_source)
        self.assertNotIn("beta transcript only", rendered_source)

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

        self.assertTrue(layers["overview"].startswith("杭州出差，住在西湖边。"))
        self.assertLess(len(layers["overview"]), len(long_content))
        self.assertEqual(layers["abstract"], "杭州出差，住在西湖边。")
        self._run(orch.close())

    def test_derive_layers_retries_transient_503_for_direct_path(self):
        request = httpx.Request("POST", "http://llm.test/chat/completions")
        attempts = 0

        async def flaky_llm(prompt: str) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                response = httpx.Response(
                    503,
                    request=request,
                    json={
                        "error": {
                            "message": "Service temporarily unavailable",
                            "type": "api_error",
                        }
                    },
                )
                raise httpx.HTTPStatusError(
                    "503 Service Unavailable",
                    request=request,
                    response=response,
                )
            return (
                '{"overview":"Alice moved to Hangzhou and plans a West Lake visit.",'
                '"keywords":["Hangzhou"],"entities":["Alice","Hangzhou"],'
                '"anchor_handles":["Hangzhou","West Lake"]}'
            )

        orch = self._make_orchestrator(llm_completion=flaky_llm)
        self._run(orch.init())

        layers = self._run(
            orch._derive_layers(
                "",
                "Alice moved to Hangzhou and plans a West Lake visit.",
            )
        )

        self.assertEqual(attempts, 2)
        self.assertEqual(
            layers["overview"],
            "Alice moved to Hangzhou and plans a West Lake visit.",
        )
        self.assertEqual(
            layers["abstract"],
            "Alice moved to Hangzhou and plans a West Lake visit.",
        )
        self._run(orch.close())

    def test_derive_layers_retries_transient_503_for_chunked_path(self):
        request = httpx.Request("POST", "http://llm.test/chat/completions")
        attempts = 0

        async def flaky_llm(prompt: str) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                response = httpx.Response(
                    503,
                    request=request,
                    json={
                        "error": {
                            "message": "Service temporarily unavailable",
                            "type": "api_error",
                        }
                    },
                )
                raise httpx.HTTPStatusError(
                    "503 Service Unavailable",
                    request=request,
                    response=response,
                )
            if "Compress the following multiple overview sections" in prompt:
                return "Alice moved to Hangzhou and plans a West Lake visit."
            return (
                '{"overview":"Alice moved to Hangzhou and plans a West Lake visit.",'
                '"keywords":["Hangzhou"],"entities":["Alice","Hangzhou"],'
                '"anchor_handles":["Hangzhou","West Lake"]}'
            )

        orch = self._make_orchestrator(llm_completion=flaky_llm)
        self._run(orch.init())
        long_content = ("Alice moved to Hangzhou and plans a West Lake visit.\n" * 300).strip()

        layers = self._run(orch._derive_layers("", long_content))

        self.assertGreaterEqual(attempts, 3)
        self.assertEqual(
            layers["overview"],
            "Alice moved to Hangzhou and plans a West Lake visit.",
        )
        self.assertEqual(
            layers["abstract"],
            "Alice moved to Hangzhou and plans a West Lake visit.",
        )
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
            self.assertGreaterEqual(len(merged_records), 1)
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
            self.assertGreaterEqual(len(merged_records), 1)
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
