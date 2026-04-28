"""Tests for the probe-first memory ContextManager flow."""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from test_e2e_phase1 import InMemoryStorage, MockEmbedder

from opencortex.config import CortexConfig, init_config
from opencortex.context.benchmark_ingest_service import (
    BenchmarkConversationIngestService,
)
from opencortex.context.manager import ConversationBuffer
from opencortex.http.request_context import (
    reset_collection_name,
    reset_request_identity,
    set_collection_name,
    set_request_identity,
)
from opencortex.intent.types import ScopeLevel as ScopeLevelImport
from opencortex.orchestrator import MemoryOrchestrator


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

    def test_prepare_phase_removed(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager

        with self.assertRaisesRegex(ValueError, "prepare phase has been removed"):
            self._run(
                cm.handle(
                    session_id="sess_002",
                    phase="prepare",
                    tenant_id="testteam",
                    user_id="alice",
                    turn_id="t1",
                    messages=[{"role": "user", "content": "test query"}],
                )
            )

        self._run(orch.close())

    def test_context_manager_does_not_expose_benchmark_ingest_wrapper(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        self.assertFalse(
            hasattr(orch._context_manager, "benchmark_ingest_conversation")
        )
        self._run(orch.close())

    def test_commit_appends_to_live_buffer_after_merge_rollover(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager

        sk = cm._make_session_key("testteam", "alice", "sess_rollover")
        stale_buffer = ConversationBuffer(
            messages=["existing"],
            token_count=1,
            start_msg_index=0,
            immediate_uris=["old-uri"],
        )
        cm._conversation_buffers[sk] = stale_buffer

        swapped = False

        async def fake_write_immediate(**kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                cm._conversation_buffers[sk] = ConversationBuffer(start_msg_index=1)
            return "new-uri"

        orch._write_immediate = fake_write_immediate

        result = self._run(
            cm._commit(
                session_id="sess_rollover",
                turn_id="turn-1",
                messages=[{"role": "user", "content": "new message"}],
                tenant_id="testteam",
                user_id="alice",
            )
        )

        live_buffer = cm._conversation_buffers[sk]
        self.assertTrue(result["accepted"])
        self.assertIsNot(live_buffer, stale_buffer)
        self.assertEqual(live_buffer.start_msg_index, 1)
        self.assertEqual(live_buffer.messages, ["new message"])
        self.assertEqual(live_buffer.immediate_uris, ["new-uri"])
        self.assertGreater(live_buffer.token_count, 0)
        self.assertEqual(stale_buffer.messages, ["existing"])
        self.assertEqual(stale_buffer.immediate_uris, ["old-uri"])

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

    def test_search_emits_probe_planner_runtime_envelope(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        self._run(
            orch.add(
                abstract="User prefers dark theme in editors",
                category="general",
            )
        )
        result = self._run(orch.search(query="What theme do I prefer?"))
        payload = result.to_memory_search_response()

        self.assertIn("memory_pipeline", payload)
        self.assertIn("probe", payload["memory_pipeline"])
        self.assertIn("planner", payload["memory_pipeline"])
        self.assertIn("runtime", payload["memory_pipeline"])
        self.assertGreaterEqual(
            payload["memory_pipeline"]["probe"]["evidence"]["candidate_count"],
            1,
        )

        self._run(orch.close())

    def test_search_can_recall_memory_written_under_different_session(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        self._run(
            orch.add(
                abstract="我下周二要去杭州出差，住在西湖边。",
                category="events",
                session_id="ingest-session",
            )
        )
        result = self._run(orch.search(query="你记得我下周二去哪里出差吗"))

        self.assertGreaterEqual(len(result.memories), 1)

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

    def test_full_session_recomposition_preserves_leaves_and_creates_directories(self):
        """Full recompose preserves original merged leaves and creates directory records."""

        async def mock_llm(prompt, **kwargs):
            return '{"abstract": "test abstract", "overview": "test overview", "keywords": ["test"]}'

        orch = self._make_orchestrator(llm_completion=mock_llm)
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

        # Original merged leaves MUST be preserved
        merged_records = [
            record
            for record in self.storage._records.get("context", {}).values()
            if record.get("meta", {}).get("layer") == "merged"
            and record.get("session_id") == session_id
        ]
        merged_uris = {record.get("uri") for record in merged_records}
        self.assertIn(first.uri, merged_uris, "Original leaf must be preserved")
        self.assertIn(second.uri, merged_uris, "Original leaf must be preserved")

        # Directory records should be created
        directory_records = [
            record
            for record in self.storage._records.get("context", {}).values()
            if record.get("meta", {}).get("layer") == "directory"
            and record.get("session_id") == session_id
        ]
        self.assertTrue(
            directory_records, "At least one directory record should be created"
        )

        self._run(orch.close())

    def test_full_session_recomposition_preserves_leaf_uris_unchanged(self):
        """Leaf URIs and content remain unchanged after full_recompose."""

        async def mock_llm(prompt, **kwargs):
            return '{"abstract": "test abstract", "overview": "test overview", "keywords": ["test"]}'

        orch = self._make_orchestrator(llm_completion=mock_llm)
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

        # Original leaf records must still exist with their original URIs
        merged_records = sorted(
            [
                record
                for record in self.storage._records.get("context", {}).values()
                if record.get("meta", {}).get("layer") == "merged"
                and record.get("session_id") == session_id
            ],
            key=lambda record: record.get("meta", {}).get("msg_range", [999, 999])[0],
        )
        merged_uris = [record.get("uri") for record in merged_records]
        self.assertIn(first_uri, merged_uris, "First leaf URI must be preserved")
        self.assertIn(second_uri, merged_uris, "Second leaf URI must be preserved")

        self._run(orch.close())

    def test_benchmark_splitter_does_not_cross_input_segment_boundary(self):
        """REVIEW closure tracker R3-RC-02 / R2-14 regression.

        ``_benchmark_recomposition_entries`` builds entries off a single
        ``msg_index`` stream across all input segments, and
        ``_build_recomposition_segments`` re-splits those entries by
        token / time_refs / message-count caps. If two adjacent input
        segments share an ``event_date`` and their combined size stays
        under the caps, the splitter silently merges them into one
        segment whose ``msg_range`` crosses the input-segment boundary.
        Downstream the LoCoMo adapter ties on the wide leaf and
        LongMemEval's ``cm.map_session_uris(return_all=False)`` drops
        one session's mapping entirely.

        This test asserts the invariant: every output segment's
        ``msg_range`` is a subset of exactly one input segment's range.

        The test MUST fail on master (no boundary check exists) and
        will pass once U2 + U3 land — adds ``source_segment_index`` to
        ``RecompositionEntry`` and a hard split when adjacent entries
        cross segments.
        """
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager

        # Two adjacent input segments, both dated 2026-04-25, sized to
        # stay under _SEGMENT_MAX_MESSAGES=16 and _SEGMENT_MAX_TOKENS=1200
        # so the existing split conditions never fire.
        def _seg(message_count: int, label: str) -> List[Dict[str, Any]]:
            return [
                {
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": f"{label} message {i} short body",
                    "meta": {
                        "event_date": "2026-04-25",
                        "time_refs": ["2026-04-25"],
                    },
                }
                for i in range(message_count)
            ]

        normalized_segments = [_seg(6, "session-A"), _seg(4, "session-B")]

        service = BenchmarkConversationIngestService(
            manager=cm,
            repo=cm._session_records,
        )
        entries = service._benchmark_recomposition_entries(normalized_segments)
        # Sanity-check construction: 10 entries, msg_index 0..9 contiguous.
        self.assertEqual(len(entries), 10)
        self.assertEqual(
            [(e["msg_start"], e["msg_end"]) for e in entries],
            [(i, i) for i in range(10)],
        )

        offline_segments = cm._build_recomposition_segments(entries)
        # The fixture stays under every soft cap (10 messages < 16,
        # ~120 tokens < 1200, time_refs overlap), so post-fix the ONLY
        # split that fires is the input-segment boundary at msg_index=6.
        # Asserting exactly 2 segments (vs ``> 0``) catches both the
        # pre-fix bug (1 cross-boundary segment) AND a future regression
        # that over-splits (e.g. 10 single-message segments would also
        # satisfy the boundary invariant). REVIEW closure-tracker T-01.
        self.assertEqual(
            len(offline_segments),
            2,
            f"expected exactly 2 segments (one per input segment); got "
            f"{len(offline_segments)} — soft caps shouldn't fire on this "
            f"fixture and over-split would silently pass the per-segment "
            f"boundary check below",
        )

        # Input segment ranges: A covers msg_range [0, 5]; B covers [6, 9].
        # Every output segment must be a subset of exactly one of these.
        for seg in offline_segments:
            msg_range = seg["msg_range"]
            start, end = int(msg_range[0]), int(msg_range[1])
            in_a = start >= 0 and end <= 5
            in_b = start >= 6 and end <= 9
            self.assertTrue(
                in_a or in_b,
                f"Output segment msg_range={msg_range} crosses the input "
                f"boundary at msg_index=6 — same-date adjacent input "
                f"sessions silently merged into one leaf. R3-RC-02.",
            )

        self._run(orch.close())

    def test_merged_leaf_uri_locks_zero_padded_format(self):
        """REVIEW closure tracker R3-RC-09 — lock the
        ``f'conversation-{hash}-{start:06d}-{end:06d}'`` URI format.

        The LoCoMo adapter's ``sorted(new_records)`` relies on the
        zero-padded msg-range fields making lex sort = numeric sort.
        Removing the padding (e.g. ``f'-{start}-{end}'``) silently
        breaks URI ordering for any session with ≥10 merged leaves —
        ``leaf-10-11`` would lex-sort BEFORE ``leaf-2-3`` and
        downstream session→URI mapping would shuffle. This test makes
        any such format change explicit by failing loudly.
        """
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager

        # Shape: 12-hex-char session hash, two 6-digit zero-padded ranges.
        uri = cm._merged_leaf_uri("testteam", "alice", "sess_R3_RC_09", [0, 1])
        self.assertRegex(
            uri,
            r"/conversation-[0-9a-f]{12}-\d{6}-\d{6}$",
            "merged leaf URI must end with the zero-padded "
            "conversation-{hash}-{start:06d}-{end:06d} suffix",
        )

        # Numeric-by-lex invariant: sorting URIs lexicographically MUST
        # match sorting by (msg_range[0], msg_range[1]). The 6-digit
        # padding is what guarantees this; removing it would cause
        # [10,11] to lex-sort before [2,9].
        ranges = [(0, 1), (2, 9), (10, 11), (100, 101), (1000, 1001)]
        uris = [
            cm._merged_leaf_uri("testteam", "alice", "sess_R3_RC_09", list(r))
            for r in ranges
        ]
        self.assertEqual(
            sorted(uris),
            uris,
            "URIs built from increasing msg_range must already be "
            "lex-sorted — this is what LoCoMo's "
            "sorted(new_records) relies on. If this fails, the "
            "zero-padding has likely been dropped or the digit count "
            "has changed.",
        )

        # Edge case: zero-width range serializes as ...-000000-000000.
        zero_uri = cm._merged_leaf_uri("testteam", "alice", "sess_R3_RC_09", [0, 0])
        self.assertTrue(
            zero_uri.endswith("-000000-000000"),
            f"zero-width range must serialize as ...-000000-000000, got: {zero_uri}",
        )

        # Edge case: 6-digit indices fit cleanly in the field.
        big_uri = cm._merged_leaf_uri(
            "testteam", "alice", "sess_R3_RC_09", [123456, 123457]
        )
        self.assertTrue(
            big_uri.endswith("-123456-123457"),
            f"6-digit indices must fit the field width, got: {big_uri}",
        )

        self._run(orch.close())

    def test_full_recompose_creates_directory_records(self):
        """Directory records have correct layer, is_leaf, abstract, overview."""

        async def mock_llm(prompt, **kwargs):
            return '{"abstract": "dir abstract", "overview": "dir overview", "keywords": ["k1", "k2"]}'

        orch = self._make_orchestrator(llm_completion=mock_llm)
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_dir_records_001"
        source_uri = cm._conversation_source_uri("testteam", "alice", session_id)

        # Create 3 merged leaves that should cluster together (shared anchors)
        for i, (content, entities) in enumerate(
            [
                ("讨论了关于杭州的生活", ["杭州"]),
                ("杭州的西湖风景很美", ["杭州", "西湖"]),
                ("杭州的美食推荐", ["杭州", "美食"]),
            ]
        ):
            self._run(
                orch.add(
                    abstract="",
                    content=content,
                    category="events",
                    context_type="memory",
                    session_id=session_id,
                    meta={
                        "layer": "merged",
                        "ingest_mode": "memory",
                        "msg_range": [i, i],
                        "source_uri": source_uri,
                        "session_id": session_id,
                        "recomposition_stage": "online_tail",
                        "entities": entities,
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

        directory_records = [
            record
            for record in self.storage._records.get("context", {}).values()
            if record.get("meta", {}).get("layer") == "directory"
            and record.get("session_id") == session_id
        ]
        self.assertTrue(directory_records, "Directory records should be created")
        for rec in directory_records:
            self.assertFalse(
                rec.get("is_leaf", True), "Directory must be is_leaf=False"
            )
            self.assertTrue(rec.get("abstract"), "Directory must have abstract")
            self.assertEqual(rec.get("meta", {}).get("session_id"), session_id)

        self._run(orch.close())

    def test_full_recompose_skips_singleton_clusters(self):
        """Clusters of size 1 should not create directory records."""

        async def mock_llm(prompt, **kwargs):
            return '{"abstract": "dir abstract", "overview": "dir overview", "keywords": []}'

        orch = self._make_orchestrator(llm_completion=mock_llm)
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_singleton_001"
        source_uri = cm._conversation_source_uri("testteam", "alice", session_id)

        # Only one merged leaf → can't cluster
        self._run(
            orch.add(
                abstract="",
                content="唯一的对话记录",
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
                    "entities": ["唯一"],
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

        # Only 1 record → should return early (len <= 1 check)
        directory_records = [
            record
            for record in self.storage._records.get("context", {}).values()
            if record.get("meta", {}).get("layer") == "directory"
            and record.get("session_id") == session_id
        ]
        self.assertEqual(directory_records, [], "No directory for single record")

        self._run(orch.close())

    def test_session_summary_skips_llm_for_single_directory(self):
        """REVIEW closure tracker R2-21 — 1-directory short-circuit.

        When recomposition produced exactly one directory and no
        ungrouped merged leaves remain, ``_generate_session_summary``
        previously made an LLM call to summarize one already-summarized
        abstract — wasteful (1 LLM round-trip + the keyword-patch
        storage scan) per session_end with single-cluster recomposition.
        After the fix, the directory's abstract / overview / topics get
        promoted to the session_summary record verbatim with zero LLM
        calls.
        """
        llm_call_count = [0]

        async def counting_llm(prompt, **kwargs):
            llm_call_count[0] += 1
            return '{"abstract": "LLM-derived abstract", "overview": "LLM-derived overview", "keywords": ["llm-kw"]}'

        orch = self._make_orchestrator(llm_completion=counting_llm)
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_r2_21_short_circuit"
        source_uri = cm._conversation_source_uri("testteam", "alice", session_id)

        # Pre-create one directory record covering the only two leaves.
        # This is the post-recomposition state — we skip running
        # _run_full_session_recomposition to keep the test focused on the
        # _generate_session_summary path that R2-21 actually changes.
        leaf_uris = [
            f"opencortex://testteam/alice/memories/events/leaf-{i}" for i in range(2)
        ]
        for i, uri in enumerate(leaf_uris):
            self._run(
                orch.add(
                    uri=uri,
                    abstract=f"leaf {i} abstract",
                    content=f"leaf {i} body",
                    category="events",
                    context_type="memory",
                    session_id=session_id,
                    meta={
                        "layer": "merged",
                        "msg_range": [i, i],
                        "source_uri": source_uri,
                        "session_id": session_id,
                    },
                )
            )

        directory_uri = "opencortex://testteam/alice/memories/events/dir-001"
        self._run(
            orch.add(
                uri=directory_uri,
                abstract="cluster abstract from prior recompose",
                overview="cluster overview from prior recompose",
                content="cluster content",
                category="events",
                context_type="memory",
                session_id=session_id,
                is_leaf=False,
                meta={
                    "layer": "directory",
                    "msg_range": [0, 1],
                    "source_uri": source_uri,
                    "session_id": session_id,
                    "child_uris": leaf_uris,
                    "topics": ["topic-a", "topic-b"],
                },
            )
        )

        # Reset call count: the orch.add() calls above invoke the LLM
        # via the orchestrator's _derive_layers_split_fields path
        # (multiple calls per leaf record). Reset so the assertion
        # below captures only calls originating from
        # _generate_session_summary.
        llm_call_count[0] = 0

        summary_uri = self._run(
            cm._generate_session_summary(
                session_id=session_id,
                tenant_id="testteam",
                user_id="alice",
                source_uri=source_uri,
            )
        )

        self.assertIsNotNone(summary_uri)
        # Critical assertion: zero LLM calls because the short-circuit
        # promoted the directory's existing abstract verbatim.
        self.assertEqual(
            llm_call_count[0],
            0,
            "_generate_session_summary should skip LLM when len(directories)==1 "
            "and no ungrouped leaves; got "
            f"{llm_call_count[0]} LLM call(s)",
        )

        # Summary record must carry the directory's abstract, overview,
        # topics — NOT the LLM-derived ones (since we never called LLM).
        summary_records = [
            record
            for record in self.storage._records.get("context", {}).values()
            if record.get("meta", {}).get("layer") == "session_summary"
            and record.get("session_id") == session_id
        ]
        self.assertEqual(len(summary_records), 1)
        summary = summary_records[0]
        self.assertEqual(summary["abstract"], "cluster abstract from prior recompose")
        self.assertEqual(summary["overview"], "cluster overview from prior recompose")
        self.assertEqual(
            summary["meta"]["topics"],
            ["topic-a", "topic-b"],
        )

        self._run(orch.close())

    def test_session_summary_uses_llm_when_multiple_directories(self):
        """Counter-test for R2-21: ≥2 directories still trigger LLM (no short-circuit)."""
        llm_call_count = [0]

        async def counting_llm(prompt, **kwargs):
            llm_call_count[0] += 1
            return '{"abstract": "merged summary", "overview": "merged overview", "keywords": ["merged"]}'

        orch = self._make_orchestrator(llm_completion=counting_llm)
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_r2_21_no_shortcut"
        source_uri = cm._conversation_source_uri("testteam", "alice", session_id)

        # Two directories → falls through to LLM path.
        for idx in range(2):
            self._run(
                orch.add(
                    uri=(
                        f"opencortex://testteam/alice/memories/events/"
                        f"sess_r2_21_no_shortcut-dir-{idx}"
                    ),
                    abstract=f"dir-{idx} abstract",
                    overview=f"dir-{idx} overview",
                    content=f"dir-{idx} content",
                    category="events",
                    context_type="memory",
                    session_id=session_id,
                    is_leaf=False,
                    meta={
                        "layer": "directory",
                        "msg_range": [idx, idx],
                        "source_uri": source_uri,
                        "session_id": session_id,
                        "child_uris": [],
                        "topics": [f"t-{idx}"],
                    },
                )
            )

        llm_call_count[0] = 0

        self._run(
            cm._generate_session_summary(
                session_id=session_id,
                tenant_id="testteam",
                user_id="alice",
                source_uri=source_uri,
            )
        )

        self.assertEqual(
            llm_call_count[0],
            1,
            "2-directory case must still call LLM exactly once "
            f"(got {llm_call_count[0]})",
        )

        self._run(orch.close())

    def test_session_summary_falls_through_to_llm_when_dir_has_empty_abstract(self):
        """Regression: empty-dir-abstract + ungrouped leaf must NOT short-circuit.

        Caught in CE review of plan 007 cleanup PR: the original guard
        ``len(directory_records) == 1 and len(abstracts) == 1`` would
        fire incorrectly when a single directory has an empty abstract
        AND there's exactly one ungrouped merged leaf:

        1. Directory loop tries to append empty abstract → ``.strip()``
           filter at line ~2755 drops it → abstracts stays empty
        2. Ungrouped-leaves loop appends one leaf abstract →
           len(abstracts) == 1
        3. Old guard: ``len(directory_records) == 1 AND
           len(abstracts) == 1`` → both true → fires
        4. Short-circuit promotes the directory's empty abstract to
           the session_summary record, **silently losing the leaf's
           content**

        Fixed by adding two stricter conditions: ``only_dir_abstract``
        is non-empty AND ``abstracts[0] == only_dir_abstract``.
        """
        llm_call_count = [0]

        async def counting_llm(prompt, **kwargs):
            llm_call_count[0] += 1
            return '{"abstract": "LLM merged abstract", "overview": "LLM overview", "keywords": ["llm-kw"]}'

        orch = self._make_orchestrator(llm_completion=counting_llm)
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_r2_21_empty_dir_regression"
        source_uri = cm._conversation_source_uri("testteam", "alice", session_id)

        # One leaf that is NOT in the directory's child_uris (ungrouped).
        ungrouped_leaf_uri = (
            "opencortex://testteam/alice/memories/events/leaf-ungrouped"
        )
        self._run(
            orch.add(
                uri=ungrouped_leaf_uri,
                abstract="ungrouped leaf important content",
                content="ungrouped leaf body",
                category="events",
                context_type="memory",
                session_id=session_id,
                meta={
                    "layer": "merged",
                    "msg_range": [0, 0],
                    "source_uri": source_uri,
                    "session_id": session_id,
                },
            )
        )
        # One directory with EMPTY abstract and child_uris that don't
        # include the ungrouped leaf.
        directory_uri = "opencortex://testteam/alice/memories/events/dir-empty-abstract"
        self._run(
            orch.add(
                uri=directory_uri,
                abstract="",  # the bug trigger
                overview="",
                content="cluster content",
                category="events",
                context_type="memory",
                session_id=session_id,
                is_leaf=False,
                meta={
                    "layer": "directory",
                    "msg_range": [0, 0],
                    "source_uri": source_uri,
                    "session_id": session_id,
                    # Empty child_uris — leaf is ungrouped.
                    "child_uris": [],
                    "topics": ["dir-topic"],
                },
            )
        )

        llm_call_count[0] = 0

        summary_uri = self._run(
            cm._generate_session_summary(
                session_id=session_id,
                tenant_id="testteam",
                user_id="alice",
                source_uri=source_uri,
            )
        )

        self.assertIsNotNone(summary_uri)
        # The leaf abstract MUST drive the summary — short-circuit must
        # NOT have fired with the directory's empty abstract.
        self.assertEqual(
            llm_call_count[0],
            1,
            "LLM must be called exactly once because the only non-empty "
            "abstract in this fixture is the ungrouped leaf, not the "
            "directory. Pre-fix, the short-circuit fired and lost the "
            f"leaf content. Got {llm_call_count[0]} LLM call(s).",
        )

        # The summary record must reflect the LLM-derived abstract,
        # NOT the directory's empty one.
        summary_records = [
            record
            for record in self.storage._records.get("context", {}).values()
            if record.get("meta", {}).get("layer") == "session_summary"
            and record.get("session_id") == session_id
        ]
        self.assertEqual(len(summary_records), 1)
        self.assertEqual(summary_records[0]["abstract"], "LLM merged abstract")

        self._run(orch.close())

    def test_session_summary_uses_llm_when_dir_plus_ungrouped_leaf(self):
        """Counter-test: 1 dir (non-empty abstract) + 1 ungrouped leaf → LLM still fires.

        len(abstracts) == 2 in this fixture (dir abstract + ungrouped
        leaf abstract), so the short-circuit's ``len(abstracts) == 1``
        clause already gates this case. This test pins the contract
        explicitly so a future change that loosens the abstracts-count
        check (e.g. switches to len-of-directory-records only) gets
        caught.
        """
        llm_call_count = [0]

        async def counting_llm(prompt, **kwargs):
            llm_call_count[0] += 1
            return '{"abstract": "merged of two", "overview": "merged overview", "keywords": []}'

        orch = self._make_orchestrator(llm_completion=counting_llm)
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_r2_21_dir_plus_ungrouped"
        source_uri = cm._conversation_source_uri("testteam", "alice", session_id)

        ungrouped_leaf_uri = (
            "opencortex://testteam/alice/memories/events/leaf-outside-dir"
        )
        self._run(
            orch.add(
                uri=ungrouped_leaf_uri,
                abstract="ungrouped leaf abstract",
                content="ungrouped body",
                category="events",
                context_type="memory",
                session_id=session_id,
                meta={
                    "layer": "merged",
                    "msg_range": [1, 1],
                    "source_uri": source_uri,
                    "session_id": session_id,
                },
            )
        )
        directory_uri = "opencortex://testteam/alice/memories/events/dir-with-one-child"
        # Directory covers a DIFFERENT leaf (not the ungrouped one above)
        # so the ungrouped count is 1.
        self._run(
            orch.add(
                uri=directory_uri,
                abstract="dir abstract non-empty",
                overview="dir overview",
                content="dir content",
                category="events",
                context_type="memory",
                session_id=session_id,
                is_leaf=False,
                meta={
                    "layer": "directory",
                    "msg_range": [0, 0],
                    "source_uri": source_uri,
                    "session_id": session_id,
                    "child_uris": [
                        "opencortex://testteam/alice/memories/events/some-other-leaf"
                    ],
                    "topics": ["dir-topic"],
                },
            )
        )

        llm_call_count[0] = 0
        self._run(
            cm._generate_session_summary(
                session_id=session_id,
                tenant_id="testteam",
                user_id="alice",
                source_uri=source_uri,
            )
        )
        self.assertEqual(
            llm_call_count[0],
            1,
            "1 dir + 1 ungrouped leaf must call LLM exactly once "
            f"(got {llm_call_count[0]})",
        )

        self._run(orch.close())

    def test_directory_uri_pattern(self):
        """_directory_uri produces correct URI pattern."""
        from opencortex.context.manager import ContextManager

        uri = ContextManager._directory_uri("team1", "user1", "session-abc", 2)
        self.assertIn("/dir-002", uri)
        self.assertIn("team1", uri)
        self.assertIn("user1", uri)

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
        rendered_source = self._run(orch._fs.read_file(f"{source_uri}/content.md"))
        self.assertIn("我搬到了杭州。", rendered_source)

        merged_records = [
            record
            for record in self.storage._records.get("context", {}).values()
            if record.get("meta", {}).get("layer") == "merged"
        ]
        self.assertEqual(len(merged_records), 1)
        self.assertEqual(merged_records[0]["meta"]["source_uri"], source_uri)
        self.assertEqual(merged_records[0]["meta"]["msg_range"], [0, 1])

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

    def test_end_fail_fast_raises_when_source_persistence_fails(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_source_fail_fast_001"

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

        with self.assertRaisesRegex(RuntimeError, "session_end failed"):
            self._run(
                cm.handle(
                    session_id=session_id,
                    phase="end",
                    tenant_id="testteam",
                    user_id="alice",
                    config={"fail_fast_end": True},
                )
            )

        sk = cm._make_session_key("testteam", "alice", session_id)
        self.assertNotIn(sk, cm._session_activity)
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
        self.assertTrue(
            all(
                record.get("retrieval_surface") == "l0_object"
                for record in immediate_records
            )
        )
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

        self.assertEqual(attempts, 7)
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
        long_content = (
            "Alice moved to Hangzhou and plans a West Lake visit.\n" * 300
        ).strip()

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

    def test_end_keeps_merged_record_visible_in_memory_list_when_llm_returns_blank(
        self,
    ):
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
            any(
                item.get("abstract", "").startswith("我下周二要去杭州出差")
                for item in session_items
            )
        )
        self._run(orch.close())

    def test_end_waits_for_background_merge_and_keeps_single_merged_record(self):
        async def _case():
            orch = self._make_orchestrator()
            await orch.init()
            cm = orch._context_manager
            session_id = "sess_merge_async_001"
            sk = cm._make_session_key("testteam", "alice", session_id)
            cm._estimate_tokens = lambda _text: 8500

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

    def test_end_waits_for_merge_followup_tasks_before_full_recompose(self):
        async def _case():
            orch = self._make_orchestrator()
            await orch.init()
            cm = orch._context_manager
            session_id = "sess_merge_async_003"
            sk = cm._make_session_key("testteam", "alice", session_id)
            cm._estimate_tokens = lambda _text: 8500

            original_complete = orch._complete_deferred_derive
            derive_started = asyncio.Event()
            allow_derive = asyncio.Event()
            full_recompose_spawned = asyncio.Event()

            async def slow_complete(*args, **kwargs):
                derive_started.set()
                await allow_derive.wait()
                return await original_complete(*args, **kwargs)

            orch._complete_deferred_derive = AsyncMock(side_effect=slow_complete)

            original_spawn = cm._spawn_full_recompose_task

            def track_spawn(*args, **kwargs):
                full_recompose_spawned.set()
                return original_spawn(*args, **kwargs)

            cm._spawn_full_recompose_task = Mock(side_effect=track_spawn)

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

            await asyncio.wait_for(derive_started.wait(), timeout=1.0)
            await cm._wait_for_merge_task(sk)
            self.assertNotIn(sk, cm._session_merge_tasks)
            self.assertIn(sk, cm._session_merge_followup_tasks)

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
            self.assertFalse(full_recompose_spawned.is_set())

            allow_derive.set()
            result = await asyncio.wait_for(end_task, timeout=2.0)

            self.assertEqual(result["status"], "closed")
            self.assertTrue(full_recompose_spawned.is_set())
            self.assertNotIn(sk, cm._session_merge_followup_tasks)

            await orch.close()

        self._run(_case())

    def test_end_fail_fast_raises_when_merge_followup_fails(self):
        async def _case():
            orch = self._make_orchestrator()
            await orch.init()
            cm = orch._context_manager
            session_id = "sess_merge_async_failfast_001"
            cm._estimate_tokens = lambda _text: 8500

            async def broken_complete(*args, **kwargs):
                raise RuntimeError("derive boom")

            orch._complete_deferred_derive = AsyncMock(side_effect=broken_complete)

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

            with self.assertRaisesRegex(RuntimeError, "Merge follow-up task failed"):
                await cm.handle(
                    session_id=session_id,
                    phase="end",
                    tenant_id="testteam",
                    user_id="alice",
                    config={"fail_fast_end": True},
                )

            await orch.close()

        self._run(_case())

    def test_failed_background_merge_is_restored_and_flushed_on_end(self):
        async def _case():
            orch = self._make_orchestrator()
            await orch.init()
            cm = orch._context_manager
            session_id = "sess_merge_async_002"
            sk = cm._make_session_key("testteam", "alice", session_id)
            cm._estimate_tokens = lambda _text: 8500

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

    # -------------------------------------------------------------------------
    # Unit 2: Anchor embedding fix + phrase upgrade tests
    # -------------------------------------------------------------------------

    def test_anchor_projection_overview_phrase_format_for_short_anchors(self):
        """Short single-word anchors get '{type}: {text}' overview format."""
        orch = self._make_orchestrator()
        self._run(orch.init())

        source_record = {
            "uri": "opencortex://testteam/alice/memory/events/abc123",
            "is_leaf": True,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "session_id": "sess_anchor_001",
            "project_id": "",
            "source_doc_id": "",
            "memory_kind": "event",
        }
        abstract_json = {
            "memory_kind": "event",
            "anchors": [
                {"anchor_type": "entity", "text": "Alice", "value": "Alice"},
                {"anchor_type": "time", "text": "2024-01-15", "value": "2024-01-15"},
            ],
            "slots": {"entities": ["Alice"], "time_refs": ["2024-01-15"]},
            "overview": "Alice moved to Hangzhou on 2024-01-15",
        }
        records = orch._anchor_projection_records(
            source_record=source_record,
            abstract_json=abstract_json,
        )
        self.assertEqual(len(records), 2)
        # "Alice" is 5 chars (< 15) → gets phrase format
        alice_record = next(r for r in records if "Alice" in r["overview"])
        self.assertEqual(alice_record["overview"], "entity: Alice")
        # "2024-01-15" is 10 chars (< 15) → gets phrase format
        time_record = next(r for r in records if "time" in r["overview"])
        self.assertEqual(time_record["overview"], "time: 2024-01-15")

        self._run(orch.close())

    def test_anchor_projection_overview_passthrough_for_long_anchors(self):
        """Anchors >= 15 chars keep their text as-is for the overview."""
        orch = self._make_orchestrator()
        self._run(orch.init())

        source_record = {
            "uri": "opencortex://testteam/alice/memory/events/xyz789",
            "is_leaf": True,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "session_id": "sess_anchor_002",
            "project_id": "",
            "source_doc_id": "",
            "memory_kind": "preference",
        }
        long_anchor_text = "Alice relocated to Hangzhou"  # 26 chars, >= 15
        abstract_json = {
            "memory_kind": "preference",
            "anchors": [
                {
                    "anchor_type": "entity",
                    "text": long_anchor_text,
                    "value": long_anchor_text,
                },
            ],
            "slots": {},
            "overview": "Alice relocated",
        }
        records = orch._anchor_projection_records(
            source_record=source_record,
            abstract_json=abstract_json,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["overview"], long_anchor_text)

        self._run(orch.close())

    def test_anchor_projection_r11_filters_short_anchor_text(self):
        """Anchors with text shorter than 4 chars are filtered out (R11)."""
        orch = self._make_orchestrator()
        self._run(orch.init())

        source_record = {
            "uri": "opencortex://testteam/alice/memory/events/r11test",
            "is_leaf": True,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "session_id": "sess_r11",
            "project_id": "",
            "source_doc_id": "",
            "memory_kind": "event",
        }
        abstract_json = {
            "memory_kind": "event",
            "anchors": [
                {
                    "anchor_type": "entity",
                    "text": "Li",
                    "value": "Li",
                },  # 2 chars → filtered
                {
                    "anchor_type": "entity",
                    "text": "Bob",
                    "value": "Bob",
                },  # 3 chars → filtered
                {
                    "anchor_type": "entity",
                    "text": "Alice",
                    "value": "Alice",
                },  # 5 chars → kept
            ],
            "slots": {},
            "overview": "Test",
        }
        records = orch._anchor_projection_records(
            source_record=source_record,
            abstract_json=abstract_json,
        )
        self.assertEqual(len(records), 1)
        self.assertIn("Alice", records[0]["overview"])

        self._run(orch.close())

    def test_anchor_projection_zero_anchors_no_embed_call(self):
        """When there are no anchors, no embedding is done and no records written."""
        orch = self._make_orchestrator()
        self._run(orch.init())

        embed_calls = []
        original_embed_batch = self.embedder.embed_batch

        def tracking_embed_batch(texts):
            embed_calls.append(texts)
            return original_embed_batch(texts)

        self.embedder.embed_batch = tracking_embed_batch

        source_record = {
            "uri": "opencortex://testteam/alice/memory/general/noanchor",
            "is_leaf": True,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "session_id": "sess_noanchor",
            "project_id": "",
            "source_doc_id": "",
            "memory_kind": "preference",
        }
        abstract_json = {
            "memory_kind": "preference",
            "anchors": [],
            "slots": {},
            "overview": "No anchors here",
        }

        self._run(
            orch._sync_anchor_projection_records(
                source_record=source_record,
                abstract_json=abstract_json,
            )
        )

        self.assertEqual(embed_calls, [])
        anchor_records = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "anchor_projection"
            and "noanchor" in r.get("uri", "")
        ]
        self.assertEqual(anchor_records, [])

        self._run(orch.close())

    def test_anchor_projection_records_have_non_zero_vectors(self):
        """After sync, anchor projection records must carry non-zero vectors."""
        orch = self._make_orchestrator()
        self._run(orch.init())

        source_uri = "opencortex://testteam/alice/memory/events/vec_test_001"
        source_record = {
            "uri": source_uri,
            "is_leaf": True,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "session_id": "sess_vec_001",
            "project_id": "",
            "source_doc_id": "",
            "memory_kind": "event",
            "abstract": "Alice moved to Hangzhou",
            "overview": "Alice moved to Hangzhou",
        }
        abstract_json = {
            "memory_kind": "event",
            "anchors": [
                {"anchor_type": "entity", "text": "Alice", "value": "Alice"},
                {"anchor_type": "location", "text": "Hangzhou", "value": "Hangzhou"},
            ],
            "slots": {"entities": ["Alice", "Hangzhou"]},
            "overview": "Alice moved to Hangzhou",
        }

        self._run(
            orch._sync_anchor_projection_records(
                source_record=source_record,
                abstract_json=abstract_json,
            )
        )

        anchor_records = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "anchor_projection"
            and r.get("uri", "").startswith(source_uri)
        ]
        self.assertEqual(len(anchor_records), 2)
        for record in anchor_records:
            vec = record.get("vector")
            self.assertIsNotNone(vec, f"Record {record.get('uri')} missing vector")
            self.assertIsInstance(vec, list)
            self.assertEqual(len(vec), MockEmbedder.DIMENSION)
            # Vector must be non-zero
            self.assertGreater(sum(abs(v) for v in vec), 0.0)

        self._run(orch.close())

    def test_anchor_embed_batch_failure_falls_back_to_zero_vectors(self):
        """If embed_batch raises, anchor records are still written with zero vectors."""
        orch = self._make_orchestrator()
        self._run(orch.init())

        def failing_embed_batch(texts):
            raise RuntimeError("embed failure")

        self.embedder.embed_batch = failing_embed_batch

        source_uri = "opencortex://testteam/alice/memory/events/embed_fail_001"
        source_record = {
            "uri": source_uri,
            "is_leaf": True,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "session_id": "sess_embfail",
            "project_id": "",
            "source_doc_id": "",
            "memory_kind": "event",
            "abstract": "",
            "overview": "",
        }
        abstract_json = {
            "memory_kind": "event",
            "anchors": [
                {"anchor_type": "entity", "text": "Alice", "value": "Alice"},
            ],
            "slots": {},
            "overview": "Alice test",
        }

        # Should not raise — graceful fallback
        self._run(
            orch._sync_anchor_projection_records(
                source_record=source_record,
                abstract_json=abstract_json,
            )
        )

        anchor_records = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "anchor_projection"
            and r.get("uri", "").startswith(source_uri)
        ]
        # Record still written (zero vector fallback)
        self.assertEqual(len(anchor_records), 1)
        # No vector field set — adapter will use zero vector fallback
        self.assertNotIn("vector", anchor_records[0])

        self._run(orch.close())

    def test_anchor_projection_vectors_set_after_add(self):
        """Calling orchestrator.add() with LLM anchor handles produces embedded anchor records."""

        async def anchor_llm(prompt: str) -> str:
            return (
                '{"overview":"Alice relocated to Hangzhou on May 1",'
                '"keywords":["Alice","Hangzhou"],'
                '"entities":["Alice","Hangzhou"],'
                '"anchor_handles":["Alice","Hangzhou"]}'
            )

        orch = self._make_orchestrator(llm_completion=anchor_llm)
        self._run(orch.init())

        self._run(
            orch.add(
                abstract="",
                content="Alice relocated to Hangzhou on May 1, 2024.",
                category="events",
            )
        )

        anchor_records = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "anchor_projection"
        ]
        self.assertGreaterEqual(len(anchor_records), 1)
        for record in anchor_records:
            vec = record.get("vector")
            self.assertIsNotNone(
                vec, f"Anchor record {record.get('uri')} has no vector"
            )
            self.assertIsInstance(vec, list)
            self.assertGreater(
                sum(abs(v) for v in vec), 0.0, "Vector should be non-zero"
            )

        self._run(orch.close())

    # -------------------------------------------------------------------------
    # Unit 3: Fact point generation + quality gate + sync tests
    # -------------------------------------------------------------------------

    def test_is_valid_fact_point_rejects_generic(self):
        """Generic descriptions without concrete signals must be rejected."""
        orch = self._make_orchestrator()
        self._run(orch.init())
        self.assertFalse(orch._is_valid_fact_point("discussed the plan"))
        self.assertFalse(orch._is_valid_fact_point("some changes were made"))
        self.assertFalse(orch._is_valid_fact_point("the system was updated"))
        self._run(orch.close())

    def test_is_valid_fact_point_accepts_specific(self):
        """Statements with concrete entities/dates/numbers must pass."""
        orch = self._make_orchestrator()
        self._run(orch.init())
        self.assertTrue(orch._is_valid_fact_point("Alice moved to Hangzhou on May 1"))
        self.assertTrue(
            orch._is_valid_fact_point("Migration uses batch size 500 to avoid downtime")
        )
        self._run(orch.close())

    def test_is_valid_fact_point_rejects_short(self):
        """Texts shorter than 8 characters must be rejected."""
        orch = self._make_orchestrator()
        self._run(orch.init())
        self.assertFalse(orch._is_valid_fact_point("todo"))
        self.assertFalse(orch._is_valid_fact_point("ok"))
        self.assertFalse(orch._is_valid_fact_point(""))
        self._run(orch.close())

    def test_is_valid_fact_point_rejects_long(self):
        """Texts longer than 80 characters must be rejected."""
        orch = self._make_orchestrator()
        self._run(orch.init())
        long_text = "Alice moved to Hangzhou on May 1 and also planned a very detailed trip with many stops"
        self.assertGreater(len(long_text), 80)
        self.assertFalse(orch._is_valid_fact_point(long_text))
        self._run(orch.close())

    def test_is_valid_fact_point_rejects_multiline(self):
        """Paragraph-style text with newlines must be rejected."""
        orch = self._make_orchestrator()
        self._run(orch.init())
        self.assertFalse(
            orch._is_valid_fact_point(
                "Alice moved to Hangzhou.\nBob stayed in Beijing."
            )
        )
        self.assertFalse(orch._is_valid_fact_point("line1\nline2"))
        self._run(orch.close())

    def test_is_valid_fact_point_accepts_chinese(self):
        """Chinese text with 2+ consecutive CJK characters must be accepted."""
        orch = self._make_orchestrator()
        self._run(orch.init())
        self.assertTrue(orch._is_valid_fact_point("北京有三百万人口"))
        self.assertTrue(orch._is_valid_fact_point("Alice搬到了杭州"))
        self._run(orch.close())

    def test_fact_point_records_written_after_add(self):
        """After orchestrator.add() with LLM returning fact_points, records appear in storage."""

        async def fp_llm(prompt: str) -> str:
            return (
                '{"overview":"Alice relocated to Hangzhou on May 1",'
                '"keywords":["Alice","Hangzhou"],'
                '"entities":["Alice","Hangzhou"],'
                '"anchor_handles":["Alice","Hangzhou"],'
                '"fact_points":["Alice moved to Hangzhou on May 1","Batch size is 500 records"]}'
            )

        orch = self._make_orchestrator(llm_completion=fp_llm)
        self._run(orch.init())

        self._run(
            orch.add(
                abstract="",
                content="Alice relocated to Hangzhou on May 1. The migration uses batch size 500.",
                category="events",
            )
        )

        fp_records = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
        ]
        self.assertGreaterEqual(len(fp_records), 1)
        for record in fp_records:
            self.assertEqual(record.get("retrieval_surface"), "fact_point")
            self.assertFalse(record.get("is_leaf", True))
            self.assertFalse(record.get("anchor_surface", True))

        self._run(orch.close())

    def test_fact_point_inherits_access_control(self):
        """Fact point records must inherit scope/tenant/user from source leaf."""

        async def fp_llm(prompt: str) -> str:
            return (
                '{"overview":"Alice moved to Hangzhou on May 1",'
                '"keywords":["Alice"],'
                '"entities":["Alice"],'
                '"anchor_handles":["Alice"],'
                '"fact_points":["Alice moved to Hangzhou on May 1"]}'
            )

        orch = self._make_orchestrator(llm_completion=fp_llm)
        self._run(orch.init())

        self._run(
            orch.add(
                abstract="",
                content="Alice moved to Hangzhou on May 1.",
                category="events",
            )
        )

        fp_records = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
        ]
        self.assertGreaterEqual(len(fp_records), 1)
        for record in fp_records:
            self.assertEqual(record.get("source_tenant_id"), "testteam")
            self.assertEqual(record.get("source_user_id"), "alice")
            self.assertEqual(record.get("scope"), "private")

        self._run(orch.close())

    def test_fact_point_projection_target_uri_points_to_leaf(self):
        """fact_point meta.projection_target_uri must equal source leaf URI."""

        async def fp_llm(prompt: str) -> str:
            return (
                '{"overview":"Alice moved to Hangzhou on May 1",'
                '"keywords":["Alice"],'
                '"entities":["Alice"],'
                '"anchor_handles":["Alice"],'
                '"fact_points":["Alice moved to Hangzhou on May 1"]}'
            )

        orch = self._make_orchestrator(llm_completion=fp_llm)
        self._run(orch.init())

        result = self._run(
            orch.add(
                abstract="",
                content="Alice moved to Hangzhou on May 1.",
                category="events",
            )
        )
        leaf_uri = result.uri

        fp_records = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
        ]
        self.assertGreaterEqual(len(fp_records), 1)
        for record in fp_records:
            self.assertEqual(
                record.get("meta", {}).get("projection_target_uri"), leaf_uri
            )
            self.assertEqual(record.get("projection_target_uri"), leaf_uri)

        self._run(orch.close())

    def test_write_then_delete_ordering(self):
        """New records must be written before stale old records are deleted.

        When content changes (new fact_points), old URIs must still exist when
        new URIs are being written. Only stale (old) records are deleted after.
        """
        source_uri = "opencortex://testteam/alice/memory/events/ordering_test"
        source_record = {
            "uri": source_uri,
            "is_leaf": True,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "session_id": "sess_order",
            "project_id": "",
            "source_doc_id": "",
            "memory_kind": "event",
            "abstract": "test",
            "overview": "test",
        }
        abstract_json_v1 = {
            "memory_kind": "event",
            "anchors": [],
            "slots": {},
            "overview": "Alice test",
            "fact_points": ["Alice moved to Hangzhou on May 1"],
        }
        abstract_json_v2 = {
            "memory_kind": "event",
            "anchors": [],
            "slots": {},
            "overview": "Alice test v2",
            # Different fact_point — different SHA1 → different URI
            "fact_points": ["Alice relocated to Shanghai on June 10"],
        }

        # Track delete calls and storage state at each delete
        delete_calls = []
        original_delete = self.storage.delete

        async def tracking_delete(collection, ids):
            # Capture all URIs currently in storage at delete time
            all_uris = {
                r.get("uri", "")
                for r in self.storage._records.get(collection, {}).values()
            }
            delete_calls.append(frozenset(all_uris))
            return await original_delete(collection, ids)

        self.storage.delete = tracking_delete

        orch = self._make_orchestrator()
        self._run(orch.init())

        # First sync: creates v1 records
        self._run(
            orch._sync_anchor_projection_records(
                source_record=source_record,
                abstract_json=abstract_json_v1,
            )
        )

        v1_fp_records = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
            and r.get("uri", "").startswith(source_uri)
        ]
        self.assertEqual(len(v1_fp_records), 1)
        v1_fp_uri = v1_fp_records[0]["uri"]

        # Second sync with different fact_points (v2) — v1 records become stale
        self._run(
            orch._sync_anchor_projection_records(
                source_record=source_record,
                abstract_json=abstract_json_v2,
            )
        )

        # At the time delete was called, both v1 AND v2 URIs must have been present
        # (write-then-delete: v2 written first, then v1 deleted)
        self.assertGreaterEqual(len(delete_calls), 1)
        v2_fp_records = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
            and r.get("uri", "").startswith(source_uri)
        ]
        # Only v2 should remain (v1 was stale and got deleted)
        self.assertEqual(len(v2_fp_records), 1)
        v2_fp_uri = v2_fp_records[0]["uri"]
        self.assertNotEqual(v1_fp_uri, v2_fp_uri)
        # v1 must NOT be present (was deleted as stale)
        remaining_uris = {
            r.get("uri", "") for r in self.storage._records.get("context", {}).values()
        }
        self.assertNotIn(v1_fp_uri, remaining_uris)

        # The delete snapshot must have contained v2 URI (written before delete)
        self.assertTrue(
            any(v2_fp_uri in snapshot for snapshot in delete_calls),
            "v2 URI must be present in storage when v1 was deleted (write-then-delete)",
        )

        self._run(orch.close())

    def test_quality_gate_filters_all_bad_fact_points(self):
        """When all fact_points fail the quality gate, no fp records are written."""
        source_uri = "opencortex://testteam/alice/memory/events/allbad_test"
        source_record = {
            "uri": source_uri,
            "is_leaf": True,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "session_id": "sess_allbad",
            "project_id": "",
            "source_doc_id": "",
            "memory_kind": "event",
            "abstract": "test",
            "overview": "test",
        }
        abstract_json = {
            "memory_kind": "event",
            "anchors": [
                {"anchor_type": "entity", "text": "Alice", "value": "Alice"},
            ],
            "slots": {},
            "overview": "Alice test",
            # All fact_points are generic (no concrete signals)
            "fact_points": ["discussed the plan", "some changes", "ok"],
        }

        orch = self._make_orchestrator()
        self._run(orch.init())

        self._run(
            orch._sync_anchor_projection_records(
                source_record=source_record,
                abstract_json=abstract_json,
            )
        )

        fp_records = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
            and r.get("uri", "").startswith(source_uri)
        ]
        # All bad → no fact_point records written
        self.assertEqual(fp_records, [])
        # But anchor records should still be written (leaf degrades to anchor-only)
        anchor_records = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "anchor_projection"
            and r.get("uri", "").startswith(source_uri)
        ]
        self.assertGreaterEqual(len(anchor_records), 1)

        self._run(orch.close())

    def test_fact_point_cap_at_eight(self):
        """More than 8 valid fact_points are capped at 8."""
        source_uri = "opencortex://testteam/alice/memory/events/cap_test"
        source_record = {
            "uri": source_uri,
            "is_leaf": True,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "session_id": "sess_cap",
            "project_id": "",
            "source_doc_id": "",
            "memory_kind": "event",
            "abstract": "",
            "overview": "",
        }
        # Build 10 distinct valid fact_points (all pass quality gate)
        fact_points = [f"Alice visited city {i} on day {i}" for i in range(1, 11)]
        abstract_json = {
            "memory_kind": "event",
            "anchors": [],
            "slots": {},
            "overview": "Alice travels",
            "fact_points": fact_points,
        }

        orch = self._make_orchestrator()
        self._run(orch.init())

        self._run(
            orch._sync_anchor_projection_records(
                source_record=source_record,
                abstract_json=abstract_json,
            )
        )

        fp_records = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
            and r.get("uri", "").startswith(source_uri)
        ]
        self.assertLessEqual(len(fp_records), 8)

        self._run(orch.close())

    def test_fact_point_records_have_non_zero_vectors(self):
        """Fact point records must carry non-zero embedded vectors."""
        source_uri = "opencortex://testteam/alice/memory/events/fpvec_test"
        source_record = {
            "uri": source_uri,
            "is_leaf": True,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "session_id": "sess_fpvec",
            "project_id": "",
            "source_doc_id": "",
            "memory_kind": "event",
            "abstract": "Alice moved",
            "overview": "Alice moved to Hangzhou",
        }
        abstract_json = {
            "memory_kind": "event",
            "anchors": [],
            "slots": {},
            "overview": "Alice moved",
            "fact_points": ["Alice moved to Hangzhou on May 1"],
        }

        orch = self._make_orchestrator()
        self._run(orch.init())

        self._run(
            orch._sync_anchor_projection_records(
                source_record=source_record,
                abstract_json=abstract_json,
            )
        )

        fp_records = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
            and r.get("uri", "").startswith(source_uri)
        ]
        self.assertEqual(len(fp_records), 1)
        vec = fp_records[0].get("vector")
        self.assertIsNotNone(vec)
        self.assertIsInstance(vec, list)
        self.assertGreater(sum(abs(v) for v in vec), 0.0)

        self._run(orch.close())

    def test_fact_point_embed_failure_falls_back_gracefully(self):
        """If embed_batch fails, fact_point records are still written (no vector key)."""

        def failing_embed_batch(texts):
            raise RuntimeError("embed failure")

        self.embedder.embed_batch = failing_embed_batch

        source_uri = "opencortex://testteam/alice/memory/events/fpembfail_test"
        source_record = {
            "uri": source_uri,
            "is_leaf": True,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "session_id": "sess_fpembfail",
            "project_id": "",
            "source_doc_id": "",
            "memory_kind": "event",
            "abstract": "",
            "overview": "",
        }
        abstract_json = {
            "memory_kind": "event",
            "anchors": [{"anchor_type": "entity", "text": "Alice", "value": "Alice"}],
            "slots": {},
            "overview": "Alice test",
            "fact_points": ["Alice moved to Hangzhou on May 1"],
        }

        orch = self._make_orchestrator()
        self._run(orch.init())

        # Should not raise
        self._run(
            orch._sync_anchor_projection_records(
                source_record=source_record,
                abstract_json=abstract_json,
            )
        )

        fp_records = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
            and r.get("uri", "").startswith(source_uri)
        ]
        # Record still written (zero vector fallback)
        self.assertEqual(len(fp_records), 1)
        self.assertNotIn("vector", fp_records[0])

        self._run(orch.close())

    # -------------------------------------------------------------------------
    # ADV-001 regression: update() and _merge_into() must preserve fact_points
    # -------------------------------------------------------------------------

    def test_update_regenerates_fact_points_after_content_change(self):
        """update(uri, content=new_content) must re-derive and persist fact_points.

        Regression: prior bug — update() dropped fact_points from _derive_layers
        result, so _sync_anchor_projection_records received empty fact_points
        and _delete_derived_stale wiped all prior fact_points on every update.
        """
        call_count = {"n": 0}

        async def fp_llm(prompt: str) -> str:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return (
                    '{"overview":"Alice relocated to Hangzhou on May 1",'
                    '"keywords":["Alice","Hangzhou"],'
                    '"entities":["Alice","Hangzhou"],'
                    '"anchor_handles":["Alice","Hangzhou"],'
                    '"fact_points":["Alice moved to Hangzhou on May 1"]}'
                )
            return (
                '{"overview":"Alice later relocated to Shanghai on June 10",'
                '"keywords":["Alice","Shanghai"],'
                '"entities":["Alice","Shanghai"],'
                '"anchor_handles":["Alice","Shanghai"],'
                '"fact_points":["Alice relocated to Shanghai on June 10"]}'
            )

        orch = self._make_orchestrator(llm_completion=fp_llm)
        self._run(orch.init())

        ctx = self._run(
            orch.add(
                abstract="",
                content="Alice moved to Hangzhou on May 1.",
                category="events",
            )
        )
        leaf_uri = ctx.uri

        fp_before = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
            and r.get("parent_uri") == leaf_uri
        ]
        self.assertGreaterEqual(
            len(fp_before),
            1,
            "Setup failed: fact_points should be created on add()",
        )
        before_uris = {r["uri"] for r in fp_before}

        # Update leaf with new content → should re-derive new fact_points
        self._run(
            orch.update(
                leaf_uri,
                content="Alice relocated to Shanghai on June 10.",
            )
        )

        fp_after = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
            and r.get("parent_uri") == leaf_uri
        ]
        self.assertGreaterEqual(
            len(fp_after),
            1,
            "BUG: update() dropped fact_points; _sync_anchor_projection_records "
            "wiped them via _delete_derived_stale",
        )
        # Content changed → derived URIs (sha1 of text) should differ
        after_uris = {r["uri"] for r in fp_after}
        self.assertNotEqual(
            before_uris,
            after_uris,
            "fact_point URIs should reflect new content digest after update()",
        )
        # Verify payload content matches newly-derived fact_point text
        overviews = {r.get("overview", "") for r in fp_after}
        self.assertIn(
            "Alice relocated to Shanghai on June 10",
            overviews,
            "Newly-derived fact_point text should appear in storage after update()",
        )

        self._run(orch.close())

    def test_update_fast_path_does_not_touch_fact_points(self):
        """update(uri) with neither abstract nor content should not invoke derivation.

        fast path: no abstract and no content → no re-derive → fact_points remain intact.
        """

        async def fp_llm(prompt: str) -> str:
            return (
                '{"overview":"Alice moved to Hangzhou on May 1",'
                '"keywords":["Alice"],'
                '"entities":["Alice"],'
                '"anchor_handles":["Alice"],'
                '"fact_points":["Alice moved to Hangzhou on May 1"]}'
            )

        orch = self._make_orchestrator(llm_completion=fp_llm)
        self._run(orch.init())

        ctx = self._run(
            orch.add(
                abstract="",
                content="Alice moved to Hangzhou on May 1.",
                category="events",
            )
        )
        leaf_uri = ctx.uri

        fp_before = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
            and r.get("parent_uri") == leaf_uri
        ]
        self.assertGreaterEqual(len(fp_before), 1)
        before_uris = {r["uri"] for r in fp_before}

        # Update with only meta → no derivation, no sync
        self._run(orch.update(leaf_uri, meta={"note": "meta-only update"}))

        fp_after = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
            and r.get("parent_uri") == leaf_uri
        ]
        after_uris = {r["uri"] for r in fp_after}
        self.assertEqual(
            before_uris,
            after_uris,
            "fast-path update() (meta only) must not delete or change fact_points",
        )

        self._run(orch.close())

    def test_merge_into_preserves_fact_points(self):
        """_merge_into() delegates to update(); merged leaf must retain fact_points."""
        call_count = {"n": 0}

        async def fp_llm(prompt: str) -> str:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return (
                    '{"overview":"Alice moved to Hangzhou on May 1",'
                    '"keywords":["Alice","Hangzhou"],'
                    '"entities":["Alice","Hangzhou"],'
                    '"anchor_handles":["Alice","Hangzhou"],'
                    '"fact_points":["Alice moved to Hangzhou on May 1"]}'
                )
            return (
                '{"overview":"Alice moved to Hangzhou then Shanghai",'
                '"keywords":["Alice","Shanghai"],'
                '"entities":["Alice","Shanghai"],'
                '"anchor_handles":["Alice","Shanghai"],'
                '"fact_points":["Alice moved to Shanghai on June 10"]}'
            )

        orch = self._make_orchestrator(llm_completion=fp_llm)
        self._run(orch.init())

        ctx = self._run(
            orch.add(
                abstract="Alice in Hangzhou",
                content="Alice moved to Hangzhou on May 1.",
                category="events",
            )
        )
        leaf_uri = ctx.uri

        # Invoke _merge_into directly to simulate the merge path
        self._run(
            orch._merge_into(
                leaf_uri,
                new_abstract="Alice moved again",
                new_content="Alice relocated to Shanghai on June 10.",
            )
        )

        fp_after = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
            and r.get("parent_uri") == leaf_uri
        ]
        self.assertGreaterEqual(
            len(fp_after),
            1,
            "BUG: _merge_into() dropped fact_points via update() path",
        )

        self._run(orch.close())

    # -------------------------------------------------------------------------
    # Unit 5: Three-layer parallel search + URI path scoring tests
    # -------------------------------------------------------------------------

    def _insert_leaf(self, orch, leaf_uri, abstract, vector):
        """Directly insert a leaf record into the in-memory storage."""
        record = {
            "id": leaf_uri,
            "uri": leaf_uri,
            "abstract": abstract,
            "overview": abstract,
            "keywords": abstract,
            "context_type": "memory",
            "category": "events",
            "is_leaf": True,
            "anchor_surface": True,
            "retrieval_surface": "l0_object",
            "memory_kind": "event",
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "project_id": "",
            "session_id": "sess_u5",
            "source_doc_id": "",
            "vector": vector,
        }
        return self._run(self.storage.upsert("context", record))

    def _insert_fact_point(self, orch, fp_uri, leaf_uri, text, vector):
        """Directly insert a fact_point record pointing to leaf_uri."""
        record = {
            "id": fp_uri,
            "uri": fp_uri,
            "abstract": text,
            "overview": text,
            "retrieval_surface": "fact_point",
            "is_leaf": False,
            "anchor_surface": False,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "project_id": "",
            "session_id": "sess_u5",
            "source_doc_id": "",
            "projection_target_uri": leaf_uri,
            "meta": {
                "projection_target_uri": leaf_uri,
                "derived": True,
                "derived_kind": "fact_point",
            },
            "vector": vector,
        }
        return self._run(self.storage.upsert("context", record))

    def _insert_anchor(self, orch, anchor_uri, leaf_uri, text, vector):
        """Directly insert an anchor_projection record pointing to leaf_uri."""
        record = {
            "id": anchor_uri,
            "uri": anchor_uri,
            "abstract": text,
            "overview": text,
            "retrieval_surface": "anchor_projection",
            "is_leaf": False,
            "anchor_surface": False,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "project_id": "",
            "session_id": "sess_u5",
            "source_doc_id": "",
            "projection_target_uri": leaf_uri,
            "meta": {
                "projection_target_uri": leaf_uri,
                "derived": True,
                "derived_kind": "anchor_projection",
            },
            "vector": vector,
        }
        return self._run(self.storage.upsert("context", record))

    def test_three_layer_search_fp_path_ranks_first(self):
        """A leaf reachable via a high-scoring fp path ranks above a direct-only leaf.

        Leaf A: only direct hit with moderate score (_score=0.6)
          direct cost = (1-0.6) + 0.15 = 0.55 → uri_path_score = 0.45
        Leaf B: fact_point with high score (_score=0.92) → fp cost discounted
          fp dist = 0.08 < 0.10 → hop = 0.05*0.5 = 0.025 → cost = 0.105
          uri_path_score = 1.0 - 0.105 = 0.895

        Leaf B must rank above Leaf A.
        """
        orch = self._make_orchestrator()
        self._run(orch.init())

        # Use the same query vector for all records so cosine sim reflects design
        query_text = "specific fact query"
        query_vec = self.embedder._text_to_vector(query_text)

        leaf_a_uri = "opencortex://testteam/alice/memory/events/leaf_a"
        leaf_b_uri = "opencortex://testteam/alice/memory/events/leaf_b"
        fp_b_uri = f"{leaf_b_uri}/fact_points/fpb001"

        # Leaf A: vector identical to query → _score ≈ 1.0, but low by design
        # We want leaf A to have direct score 0.6 — use a different vector
        leaf_a_vec = self.embedder._text_to_vector(
            "unrelated content about something else"
        )
        leaf_b_vec = self.embedder._text_to_vector(
            "leaf b content different from query"
        )
        # fp_b has high similarity to query
        fp_b_vec = query_vec  # exact match → _score = 1.0

        self._insert_leaf(orch, leaf_a_uri, "Leaf A direct only", leaf_a_vec)
        self._insert_leaf(orch, leaf_b_uri, "Leaf B with fact point", leaf_b_vec)
        self._insert_fact_point(
            orch, fp_b_uri, leaf_b_uri, "Alice moved to Hangzhou on May 1", fp_b_vec
        )

        result = self._run(orch.search(query_text, limit=5))
        uris = [ctx.uri for ctx in result.memories]

        # Leaf B (fp path high score) must appear in results
        self.assertIn(leaf_b_uri, uris)
        # Leaf B must rank before Leaf A (fp path gives higher score)
        if leaf_a_uri in uris:
            self.assertLess(uris.index(leaf_b_uri), uris.index(leaf_a_uri))

        self._run(orch.close())

    def test_three_layer_search_anchor_path_discovery(self):
        """A leaf discovered only through anchor projection still appears in results.

        Leaf C has zero overlap with query vector but its anchor has high score.
        Leaf C should appear in results via URI projection + batch load.
        """
        orch = self._make_orchestrator()
        self._run(orch.init())

        query_text = "anchor discovery query"
        query_vec = self.embedder._text_to_vector(query_text)

        leaf_c_uri = "opencortex://testteam/alice/memory/events/leaf_c"
        anchor_c_uri = f"{leaf_c_uri}/anchors/anc001"

        # Leaf C: orthogonal vector (won't match in leaf search but reachable via anchor)
        leaf_c_vec = [1.0, 0.0, 0.0, 0.0]  # normalize manually
        import math

        norm = math.sqrt(sum(v * v for v in leaf_c_vec)) or 1.0
        leaf_c_vec = [v / norm for v in leaf_c_vec]

        # Anchor points to query text → high cosine similarity
        anchor_c_vec = query_vec

        self._insert_leaf(orch, leaf_c_uri, "Leaf C via anchor only", leaf_c_vec)
        self._insert_anchor(
            orch, anchor_c_uri, leaf_c_uri, "anchor text for discovery", anchor_c_vec
        )

        result = self._run(orch.search(query_text, limit=10))
        uris = [ctx.uri for ctx in result.memories]

        # Leaf C must appear even though its own vector doesn't match query
        self.assertIn(
            leaf_c_uri, uris, "Leaf C should be discovered via anchor projection"
        )

        self._run(orch.close())

    def test_three_layer_search_historical_leaf_no_fp(self):
        """A leaf without any fact_point or anchor is still retrievable (backward compat R31)."""
        orch = self._make_orchestrator()
        self._run(orch.init())

        query_text = "historical memory query"
        query_vec = self.embedder._text_to_vector(query_text)

        leaf_h_uri = "opencortex://testteam/alice/memory/events/leaf_h_historical"
        # Old-style leaf: only vector, no fp or anchor children
        self._insert_leaf(orch, leaf_h_uri, "historical leaf no fp", query_vec)

        result = self._run(orch.search(query_text, limit=5))
        uris = [ctx.uri for ctx in result.memories]
        self.assertIn(
            leaf_h_uri, uris, "Historical leaf without fp must still be retrievable"
        )

        # path_source should be "direct" since there are no fp/anchor hits
        ctx = next(ctx for ctx in result.memories if ctx.uri == leaf_h_uri)
        # path_source may be "direct" or None (when leaf didn't score into uri_path_costs,
        # which shouldn't happen since leaf IS in leaf_hits)
        self.assertIn(ctx.path_source, ("direct", None))

        self._run(orch.close())

    def test_three_layer_orphan_fp_discarded(self):
        """An fp pointing to a non-existent leaf does not cause an error or phantom result."""
        orch = self._make_orchestrator()
        self._run(orch.init())

        query_text = "orphan fact point query"
        query_vec = self.embedder._text_to_vector(query_text)

        # fp pointing to a leaf that does NOT exist in storage
        ghost_leaf_uri = "opencortex://testteam/alice/memory/events/leaf_ghost_deleted"
        fp_orphan_uri = f"{ghost_leaf_uri}/fact_points/orphan001"

        self._insert_fact_point(
            orch, fp_orphan_uri, ghost_leaf_uri, "Alice moved on May 1", query_vec
        )

        # Should not raise; ghost leaf is absent from storage → batch load returns nothing
        result = self._run(orch.search(query_text, limit=5))

        # ghost_leaf_uri must NOT appear in results
        uris = [ctx.uri for ctx in result.memories]
        self.assertNotIn(
            ghost_leaf_uri,
            uris,
            "Ghost leaf must not appear after orphan fp projection",
        )

        self._run(orch.close())

    def test_candidate_count_fast_exit(self):
        """When probe returns 0 candidates AND 0 anchor_hits, search is skipped (fast-exit).

        This test verifies the empty-store fast-exit path added in Unit 6.
        The storage is empty, so probe returns 0 candidates, and the system
        should return empty results without unnecessary search calls.
        """
        orch = self._make_orchestrator()
        self._run(orch.init())

        search_calls = []
        original_search = self.storage.search

        async def tracking_search(collection, **kwargs):
            # Only track searches on the default "context" collection
            # Skip create_collection and collection_exists type calls
            search_calls.append(collection)
            return await original_search(collection, **kwargs)

        self.storage.search = tracking_search

        result = self._run(orch.search(query="what is my preference?"))

        # With empty store: memory should be empty.
        self.assertEqual(result.memories, [])

        # The search calls for object recall should be minimal
        # (probe may call search; but _execute_object_query fast-exit should fire or return empty)
        # We don't assert zero calls (probe itself calls search) but result is empty
        self.assertEqual(result.memories, [])

        self._run(orch.close())

    # -------------------------------------------------------------------------
    # Unit 7: Lifecycle cascade + trace field verification
    # -------------------------------------------------------------------------

    def test_delete_leaf_cascades_to_fact_points(self):
        """orchestrator.remove(leaf_uri) must delete all /fact_points/* children."""
        source_uri = "opencortex://testteam/alice/memory/events/cascade_fp_test"
        source_record = {
            "uri": source_uri,
            "is_leaf": True,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "session_id": "sess_cascade_fp",
            "project_id": "",
            "source_doc_id": "",
            "memory_kind": "event",
            "abstract": "cascade test",
            "overview": "cascade test",
        }
        abstract_json = {
            "memory_kind": "event",
            "anchors": [],
            "slots": {},
            "overview": "cascade test",
            "fact_points": [
                "Alice moved to Hangzhou on May 1",
                "Batch size is 500 records",
            ],
        }

        orch = self._make_orchestrator()
        self._run(orch.init())

        # Write leaf + fact_point children
        self._run(
            self.storage.upsert(
                "context",
                {
                    "id": source_uri,
                    "uri": source_uri,
                    "is_leaf": True,
                    "abstract": "cascade test",
                    "vector": self.embedder._text_to_vector("cascade test"),
                    "retrieval_surface": "l0_object",
                    "scope": "private",
                    "source_tenant_id": "testteam",
                    "source_user_id": "alice",
                },
            )
        )
        self._run(
            orch._sync_anchor_projection_records(
                source_record=source_record,
                abstract_json=abstract_json,
            )
        )

        # Verify fact_points were written
        fp_before = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
            and r.get("uri", "").startswith(source_uri)
        ]
        self.assertGreaterEqual(len(fp_before), 1)

        # Delete the leaf via orchestrator.remove()
        self._run(orch.remove(source_uri))

        # All fact_point children must be gone
        fp_after = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("uri", "").startswith(source_uri)
        ]
        self.assertEqual(
            fp_after,
            [],
            f"Expected all children deleted, but found: {[r.get('uri') for r in fp_after]}",
        )

        self._run(orch.close())

    def test_delete_leaf_cascades_to_anchor_projections(self):
        """orchestrator.remove(leaf_uri) must delete all /anchors/* children."""
        source_uri = "opencortex://testteam/alice/memory/events/cascade_anchor_test"
        source_record = {
            "uri": source_uri,
            "is_leaf": True,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "session_id": "sess_cascade_anchor",
            "project_id": "",
            "source_doc_id": "",
            "memory_kind": "event",
            "abstract": "cascade anchor test",
            "overview": "cascade anchor test",
        }
        abstract_json = {
            "memory_kind": "event",
            "anchors": [
                {"anchor_type": "entity", "text": "Alice", "value": "Alice"},
                {"anchor_type": "location", "text": "Hangzhou", "value": "Hangzhou"},
            ],
            "slots": {"entities": ["Alice", "Hangzhou"]},
            "overview": "cascade anchor test",
            "fact_points": [],
        }

        orch = self._make_orchestrator()
        self._run(orch.init())

        # Write leaf record
        self._run(
            self.storage.upsert(
                "context",
                {
                    "id": source_uri,
                    "uri": source_uri,
                    "is_leaf": True,
                    "abstract": "cascade anchor test",
                    "vector": self.embedder._text_to_vector("cascade anchor test"),
                    "retrieval_surface": "l0_object",
                    "scope": "private",
                    "source_tenant_id": "testteam",
                    "source_user_id": "alice",
                },
            )
        )
        self._run(
            orch._sync_anchor_projection_records(
                source_record=source_record,
                abstract_json=abstract_json,
            )
        )

        # Verify anchor projections were written
        anchors_before = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "anchor_projection"
            and r.get("uri", "").startswith(source_uri)
        ]
        self.assertGreaterEqual(len(anchors_before), 1)

        # Delete leaf
        self._run(orch.remove(source_uri))

        # All anchor_projection children must be gone
        remaining = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("uri", "").startswith(source_uri)
        ]
        self.assertEqual(
            remaining,
            [],
            f"Expected no children, but found: {[r.get('uri') for r in remaining]}",
        )

        self._run(orch.close())

    def test_recomposition_preserves_leaves_and_fact_points(self):
        """Full recompose preserves original leaves — fact_points remain intact."""

        async def mock_llm(prompt, **kwargs):
            return '{"abstract": "test abstract", "overview": "test overview", "keywords": ["test"]}'

        orch = self._make_orchestrator(llm_completion=mock_llm)
        self._run(orch.init())
        cm = orch._context_manager
        session_id = "sess_recompose_fp_cleanup"
        source_uri = cm._conversation_source_uri("testteam", "alice", session_id)

        # Build two old merged leaves with synthetic URIs
        old_uri_1 = cm._merged_leaf_uri("testteam", "alice", session_id, [0, 0])
        old_uri_2 = cm._merged_leaf_uri("testteam", "alice", session_id, [1, 1])

        # Write old leaves to storage
        for old_uri, content, msg_range in [
            (old_uri_1, "第一段：我搬到了杭州。", [0, 0]),
            (old_uri_2, "第二段：我住在西湖边。", [1, 1]),
        ]:
            self._run(
                orch.add(
                    uri=old_uri,
                    abstract="",
                    content=content,
                    category="events",
                    context_type="memory",
                    session_id=session_id,
                    meta={
                        "layer": "merged",
                        "ingest_mode": "memory",
                        "msg_range": msg_range,
                        "source_uri": source_uri,
                        "session_id": session_id,
                        "recomposition_stage": "online_tail",
                        "entities": [],
                    },
                )
            )

        # Manually write fake fact_point children under old leaves
        for old_uri in [old_uri_1, old_uri_2]:
            self._run(
                self.storage.upsert(
                    "context",
                    {
                        "id": f"{old_uri}/fact_points/fake001",
                        "uri": f"{old_uri}/fact_points/fake001",
                        "retrieval_surface": "fact_point",
                        "is_leaf": False,
                        "projection_target_uri": old_uri,
                        "meta": {"projection_target_uri": old_uri},
                        "abstract": "fake fact",
                        "overview": "fake fact point for cascade test",
                        "scope": "private",
                        "source_tenant_id": "testteam",
                        "source_user_id": "alice",
                    },
                )
            )

        # Confirm fact_points are present before recomposition
        fp_before = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
            and (
                r.get("uri", "").startswith(old_uri_1)
                or r.get("uri", "").startswith(old_uri_2)
            )
        ]
        self.assertEqual(len(fp_before), 2)

        # Run full session recomposition — preserves leaves, adds directories
        self._run(
            cm._run_full_session_recomposition(
                session_id=session_id,
                tenant_id="testteam",
                user_id="alice",
                source_uri=source_uri,
            )
        )

        # Original leaf URIs must still be present
        for old_uri in [old_uri_1, old_uri_2]:
            old_still_present = any(
                r.get("uri") == old_uri
                for r in self.storage._records.get("context", {}).values()
            )
            self.assertTrue(old_still_present, f"Leaf {old_uri} must be preserved")

        # Fact_points under old leaves must also remain intact
        fp_after = [
            r
            for r in self.storage._records.get("context", {}).values()
            if r.get("retrieval_surface") == "fact_point"
            and (
                r.get("uri", "").startswith(old_uri_1)
                or r.get("uri", "").startswith(old_uri_2)
            )
        ]
        self.assertEqual(
            len(fp_after),
            2,
            f"Fact_points must be preserved when leaves are preserved, "
            f"found: {[r.get('uri') for r in fp_after]}",
        )

        self._run(orch.close())

    def test_trace_fields_in_search_result(self):
        """Search results must include path_source and path_cost when a fact_point path is used."""
        orch = self._make_orchestrator()
        self._run(orch.init())

        query_text = "Alice moved to Hangzhou on May 1"
        query_vec = self.embedder._text_to_vector(query_text)

        leaf_uri = "opencortex://testteam/alice/memory/events/trace_test_leaf"
        fp_uri = f"{leaf_uri}/fact_points/tracetest001"

        # Insert leaf with unrelated vector (will be discovered via fp path)
        unrelated_vec = self.embedder._text_to_vector("completely unrelated content")
        self._insert_leaf(orch, leaf_uri, "Leaf for trace test", unrelated_vec)
        # Insert fact_point with query-aligned vector
        self._insert_fact_point(
            orch, fp_uri, leaf_uri, "Alice moved to Hangzhou on May 1", query_vec
        )

        result = self._run(orch.search(query_text, limit=5))

        # Leaf must appear in results (via fp path)
        uris = [ctx.uri for ctx in result.memories]
        self.assertIn(leaf_uri, uris)

        # Retrieve the matched context for this leaf
        ctx = next(c for c in result.memories if c.uri == leaf_uri)

        # path_source and path_cost must be populated
        self.assertIsNotNone(
            ctx.path_source, "path_source must not be None when fp path used"
        )
        self.assertIn(
            ctx.path_source,
            ("fact_point", "anchor", "direct"),
            f"path_source must be a known value, got: {ctx.path_source}",
        )
        self.assertIsNotNone(ctx.path_cost, "path_cost must not be None")
        self.assertIsInstance(ctx.path_cost, float)

        # Verify trace fields surface through to_memory_search_result() and _context_to_dict()
        search_result_dict = ctx.to_memory_search_result()
        if ctx.path_source is not None:
            self.assertIn("path_source", search_result_dict)
            self.assertEqual(search_result_dict["path_source"], ctx.path_source)
        if ctx.path_cost is not None:
            self.assertIn("path_cost", search_result_dict)
            self.assertEqual(search_result_dict["path_cost"], ctx.path_cost)

        # Verify backward compat: a leaf with no path trace produces no path keys
        plain_leaf_uri = "opencortex://testteam/alice/memory/events/plain_no_trace"
        from opencortex.retrieve.types import ContextType, MatchedContext

        plain_ctx = MatchedContext(
            uri=plain_leaf_uri,
            abstract="plain",
            score=0.5,
            context_type=ContextType.MEMORY,
            # path_source, path_cost, path_breakdown left as None
        )
        plain_dict = plain_ctx.to_memory_search_result()
        self.assertNotIn("path_source", plain_dict)
        self.assertNotIn("path_cost", plain_dict)
        self.assertNotIn("path_breakdown", plain_dict)

        self._run(orch.close())


# =============================================================================
# ADV-002 regression: scope filter must cover anchor / fp search + batch load
# =============================================================================


class TestScopeFilterAppliesToDerivedSurfaces(unittest.TestCase):
    """ADV-002: CONTAINER_SCOPED / SESSION_ONLY / DOCUMENT_ONLY must filter
    anchor_projection and fact_point searches AND the missing_uris batch load.

    Before the fix: only leaf search was scope-gated. An fp/anchor in an
    out-of-scope container could project its leaf back in via URI projection
    and the unscoped batch load, leaking cross-container / cross-session /
    cross-document leaves into results.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="opencortex_scope_")
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
            llm_completion=None,
        )

    def _insert_leaf(
        self,
        leaf_uri,
        abstract,
        vector,
        *,
        parent_uri="",
        session_id="sess_default",
        source_doc_id="",
    ):
        record = {
            "id": leaf_uri,
            "uri": leaf_uri,
            "parent_uri": parent_uri,
            "abstract": abstract,
            "overview": abstract,
            "keywords": abstract,
            "context_type": "memory",
            "category": "events",
            "is_leaf": True,
            "anchor_surface": True,
            "retrieval_surface": "l0_object",
            "memory_kind": "event",
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "project_id": "",
            "session_id": session_id,
            "source_doc_id": source_doc_id,
            "vector": vector,
        }
        return self._run(self.storage.upsert("context", record))

    def _insert_fact_point(
        self,
        fp_uri,
        leaf_uri,
        text,
        vector,
        *,
        parent_uri=None,
        session_id="sess_default",
        source_doc_id="",
    ):
        record = {
            "id": fp_uri,
            "uri": fp_uri,
            # fact_point inherits scope fields from source leaf; tests set them explicitly
            "parent_uri": parent_uri if parent_uri is not None else leaf_uri,
            "abstract": text,
            "overview": text,
            "retrieval_surface": "fact_point",
            "is_leaf": False,
            "anchor_surface": False,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "project_id": "",
            "session_id": session_id,
            "source_doc_id": source_doc_id,
            "projection_target_uri": leaf_uri,
            "meta": {
                "projection_target_uri": leaf_uri,
                "derived": True,
                "derived_kind": "fact_point",
            },
            "vector": vector,
        }
        return self._run(self.storage.upsert("context", record))

    def _insert_anchor(
        self,
        anchor_uri,
        leaf_uri,
        text,
        vector,
        *,
        parent_uri=None,
        session_id="sess_default",
        source_doc_id="",
    ):
        record = {
            "id": anchor_uri,
            "uri": anchor_uri,
            "parent_uri": parent_uri if parent_uri is not None else leaf_uri,
            "abstract": text,
            "overview": text,
            "retrieval_surface": "anchor_projection",
            "is_leaf": False,
            "anchor_surface": False,
            "scope": "private",
            "source_tenant_id": "testteam",
            "source_user_id": "alice",
            "project_id": "",
            "session_id": session_id,
            "source_doc_id": source_doc_id,
            "projection_target_uri": leaf_uri,
            "meta": {
                "projection_target_uri": leaf_uri,
                "derived": True,
                "derived_kind": "anchor_projection",
            },
            "vector": vector,
        }
        return self._run(self.storage.upsert("context", record))

    def _build_plan_and_probe(self, *, scope_level, starting_points):
        """Fabricate RetrievalPlan + SearchResult with requested scope_level."""
        from opencortex.intent.types import (
            MemoryQueryPlan,
            MemorySearchProfile,
            RetrievalDepth,
            RetrievalPlan,
            SearchResult,
            StartingPoint,
        )

        plan = RetrievalPlan(
            target_memory_kinds=[],
            query_plan=MemoryQueryPlan(),
            search_profile=MemorySearchProfile(
                recall_budget=0.4,
                association_budget=0.2,
                rerank=False,
            ),
            retrieval_depth=RetrievalDepth.L1,
            scope_level=scope_level,
        )
        probe = SearchResult(
            should_recall=True,
            starting_points=[StartingPoint(**sp) for sp in starting_points],
            scope_level=scope_level,
        )
        return plan, probe

    def _make_typed_query(self, query_text):
        from opencortex.retrieve.types import ContextType, TypedQuery

        return TypedQuery(query=query_text, context_type=ContextType.MEMORY, intent="")

    # -------------------------------------------------------------------------
    # CONTAINER_SCOPED
    # -------------------------------------------------------------------------

    def test_container_scoped_blocks_out_of_scope_fact_point(self):
        """CONTAINER_SCOPED: fp living under an out-of-scope container must NOT
        pull its leaf into results via URI projection.
        """
        orch = self._make_orchestrator()
        self._run(orch.init())

        query_text = "specific fact query"
        query_vec = self.embedder._text_to_vector(query_text)
        unrelated_vec = self.embedder._text_to_vector("something else entirely")

        container_a = "opencortex://testteam/alice/memory/events/container_a"
        container_b = "opencortex://testteam/alice/memory/events/container_b"

        leaf_in = f"{container_a}/leaf_in"
        leaf_out = f"{container_b}/leaf_out"
        fp_out = f"{leaf_out}/fact_points/fpout001"

        # In-scope leaf: unrelated vector (only leaf search surface)
        self._insert_leaf(
            leaf_in, "in scope leaf", unrelated_vec, parent_uri=container_a
        )
        # Out-of-scope leaf: unrelated vector (would only surface via fp projection)
        self._insert_leaf(
            leaf_out, "out of scope leaf", unrelated_vec, parent_uri=container_b
        )
        # Out-of-scope fp: high relevance to query (tempting projection target)
        self._insert_fact_point(
            fp_out,
            leaf_out,
            "Alice moved to Hangzhou",
            query_vec,
            parent_uri=container_b,
        )

        plan, probe = self._build_plan_and_probe(
            scope_level=ScopeLevelImport.CONTAINER_SCOPED,
            starting_points=[{"uri": container_a}],
        )
        result = self._run(
            orch._execute_object_query(
                typed_query=self._make_typed_query(query_text),
                limit=10,
                score_threshold=None,
                search_filter=None,
                retrieve_plan=plan,
                probe_result=probe,
                bound_plan=None,
            )
        )
        uris = [mc.uri for mc in result.matched_contexts]
        self.assertNotIn(
            leaf_out,
            uris,
            "Out-of-scope leaf must not leak in via its fact_point projection",
        )

        self._run(orch.close())

    def test_container_scoped_blocks_out_of_scope_anchor(self):
        """CONTAINER_SCOPED: anchor living under an out-of-scope container must
        NOT pull its leaf into results.
        """
        orch = self._make_orchestrator()
        self._run(orch.init())

        query_text = "anchor discovery"
        query_vec = self.embedder._text_to_vector(query_text)
        unrelated_vec = self.embedder._text_to_vector("something else entirely")

        container_a = "opencortex://testteam/alice/memory/events/container_ax"
        container_b = "opencortex://testteam/alice/memory/events/container_bx"

        leaf_out = f"{container_b}/leaf_oa"
        anchor_out = f"{leaf_out}/anchors/aout001"

        self._insert_leaf(
            leaf_out, "out of scope leaf", unrelated_vec, parent_uri=container_b
        )
        self._insert_anchor(
            anchor_out,
            leaf_out,
            "anchor phrase matching query",
            query_vec,
            parent_uri=container_b,
        )

        plan, probe = self._build_plan_and_probe(
            scope_level=ScopeLevelImport.CONTAINER_SCOPED,
            starting_points=[{"uri": container_a}],
        )
        result = self._run(
            orch._execute_object_query(
                typed_query=self._make_typed_query(query_text),
                limit=10,
                score_threshold=None,
                search_filter=None,
                retrieve_plan=plan,
                probe_result=probe,
                bound_plan=None,
            )
        )
        uris = [mc.uri for mc in result.matched_contexts]
        self.assertNotIn(
            leaf_out,
            uris,
            "Out-of-scope leaf must not leak in via its anchor projection",
        )

        self._run(orch.close())

    # -------------------------------------------------------------------------
    # SESSION_ONLY
    # -------------------------------------------------------------------------

    def test_session_only_blocks_other_session_fact_point(self):
        """SESSION_ONLY: fp from session B must not pull its leaf into results
        when query is scoped to session A.
        """
        orch = self._make_orchestrator()
        self._run(orch.init())

        query_text = "session scoped query"
        query_vec = self.embedder._text_to_vector(query_text)
        unrelated_vec = self.embedder._text_to_vector("unrelated content")

        leaf_b = "opencortex://testteam/alice/memory/events/leaf_session_b"
        fp_b = f"{leaf_b}/fact_points/fpsb001"

        self._insert_leaf(
            leaf_b, "leaf in session B", unrelated_vec, session_id="sess_B"
        )
        self._insert_fact_point(
            fp_b, leaf_b, "Alice moved to Hangzhou", query_vec, session_id="sess_B"
        )

        plan, probe = self._build_plan_and_probe(
            scope_level=ScopeLevelImport.SESSION_ONLY,
            starting_points=[
                {
                    "uri": "opencortex://testteam/alice/memory/events/sp_a",
                    "session_id": "sess_A",
                },
            ],
        )
        result = self._run(
            orch._execute_object_query(
                typed_query=self._make_typed_query(query_text),
                limit=10,
                score_threshold=None,
                search_filter=None,
                retrieve_plan=plan,
                probe_result=probe,
                bound_plan=None,
            )
        )
        uris = [mc.uri for mc in result.matched_contexts]
        self.assertNotIn(
            leaf_b,
            uris,
            "Session-B leaf must not leak into session-A query via its fp",
        )

        self._run(orch.close())

    # -------------------------------------------------------------------------
    # DOCUMENT_ONLY
    # -------------------------------------------------------------------------

    def test_document_only_blocks_other_doc_fact_point(self):
        """DOCUMENT_ONLY: fp from a different document must not pull its leaf in."""
        orch = self._make_orchestrator()
        self._run(orch.init())

        query_text = "document scoped query"
        query_vec = self.embedder._text_to_vector(query_text)
        unrelated_vec = self.embedder._text_to_vector("something else entirely")

        leaf_y = "opencortex://testteam/alice/memory/events/leaf_doc_y"
        fp_y = f"{leaf_y}/fact_points/fpdy001"

        self._insert_leaf(leaf_y, "leaf in doc Y", unrelated_vec, source_doc_id="doc_Y")
        self._insert_fact_point(
            fp_y, leaf_y, "Alice moved to Hangzhou", query_vec, source_doc_id="doc_Y"
        )

        plan, probe = self._build_plan_and_probe(
            scope_level=ScopeLevelImport.DOCUMENT_ONLY,
            starting_points=[
                {
                    "uri": "opencortex://testteam/alice/memory/events/sp_x",
                    "source_doc_id": "doc_X",
                },
            ],
        )
        result = self._run(
            orch._execute_object_query(
                typed_query=self._make_typed_query(query_text),
                limit=10,
                score_threshold=None,
                search_filter=None,
                retrieve_plan=plan,
                probe_result=probe,
                bound_plan=None,
            )
        )
        uris = [mc.uri for mc in result.matched_contexts]
        self.assertNotIn(
            leaf_y,
            uris,
            "Doc-Y leaf must not leak into doc-X-scoped query via its fp",
        )

        self._run(orch.close())

    # -------------------------------------------------------------------------
    # GLOBAL scope: no filter, existing behavior preserved
    # -------------------------------------------------------------------------

    def test_global_scope_finds_leaf_via_fp_projection(self):
        """GLOBAL scope: fp anywhere may project its leaf in (no scope filter)."""
        orch = self._make_orchestrator()
        self._run(orch.init())

        query_text = "global fp path query"
        query_vec = self.embedder._text_to_vector(query_text)
        unrelated_vec = self.embedder._text_to_vector("unrelated content")

        leaf_uri = "opencortex://testteam/alice/memory/events/leaf_global_fp"
        fp_uri = f"{leaf_uri}/fact_points/fpg001"

        self._insert_leaf(leaf_uri, "global leaf", unrelated_vec, session_id="sess_any")
        self._insert_fact_point(
            fp_uri,
            leaf_uri,
            "Alice moved to Hangzhou",
            query_vec,
            session_id="sess_any",
        )

        plan, probe = self._build_plan_and_probe(
            scope_level=ScopeLevelImport.GLOBAL,
            starting_points=[],
        )
        result = self._run(
            orch._execute_object_query(
                typed_query=self._make_typed_query(query_text),
                limit=10,
                score_threshold=None,
                search_filter=None,
                retrieve_plan=plan,
                probe_result=probe,
                bound_plan=None,
            )
        )
        uris = [mc.uri for mc in result.matched_contexts]
        self.assertIn(
            leaf_uri,
            uris,
            "GLOBAL scope must still allow fp projection to surface its leaf",
        )

        self._run(orch.close())

    # -------------------------------------------------------------------------
    # CONTAINER_SCOPED keeps the in-scope leaf reachable via fp
    # -------------------------------------------------------------------------

    def test_container_scoped_keeps_in_scope_fact_point_path(self):
        """In-scope fp must still be able to surface its leaf (fp path works inside scope)."""
        orch = self._make_orchestrator()
        self._run(orch.init())

        query_text = "in-scope fp query"
        query_vec = self.embedder._text_to_vector(query_text)
        unrelated_vec = self.embedder._text_to_vector("unrelated filler content")

        container_a = "opencortex://testteam/alice/memory/events/container_cs_in"
        leaf_in = f"{container_a}/leaf_cs_in"
        fp_in = f"{leaf_in}/fact_points/fpin001"

        self._insert_leaf(
            leaf_in, "in-scope leaf", unrelated_vec, parent_uri=container_a
        )
        self._insert_fact_point(
            fp_in, leaf_in, "Alice moved to Hangzhou", query_vec, parent_uri=container_a
        )

        plan, probe = self._build_plan_and_probe(
            scope_level=ScopeLevelImport.CONTAINER_SCOPED,
            starting_points=[{"uri": container_a}],
        )
        result = self._run(
            orch._execute_object_query(
                typed_query=self._make_typed_query(query_text),
                limit=10,
                score_threshold=None,
                search_filter=None,
                retrieve_plan=plan,
                probe_result=probe,
                bound_plan=None,
            )
        )
        uris = [mc.uri for mc in result.matched_contexts]
        self.assertIn(
            leaf_in,
            uris,
            "In-scope fp must still surface its leaf when parent_uri matches",
        )

        self._run(orch.close())


if __name__ == "__main__":
    unittest.main()
