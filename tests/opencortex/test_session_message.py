# SPDX-License-Identifier: Apache-2.0
"""Tests for the direct session message store flow."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from opencortex.core.identity import IdentityProfile
from opencortex.store.event.events import StoreEvents
from opencortex.store.schemas import SessionMessage, SessionMessageInput
from opencortex.store.session.buffer import SessionBuffer
from opencortex.store.session.merger import SessionMerger
from opencortex.store.session.store import SessionStore
from opencortex.store.writer.primary_record_writer import PrimaryRecordWriter


class _Events:
    """Capture published events."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish_nowait(self, event: Any) -> None:
        self.events.append(event)


class _Namespace:
    """Namespace fake for session tests."""

    @staticmethod
    def parent(uri: str) -> str:
        """Return a simple URI parent."""
        prefix = "opencortex://"
        path = uri[len(prefix) :].strip("/")
        if "/" not in path:
            return ""
        return f"{prefix}{path.rsplit('/', maxsplit=1)[0]}"

    def parent_chain(self, parent_uri: str) -> list[str]:
        """Return root-to-leaf parent chain."""
        chain = []
        current = parent_uri
        while current:
            chain.append(current)
            current = self.parent(current)
        return list(reversed(chain))


class _Embedder:
    """Fake StoreEmbedder for immediate records."""

    async def embed_context(self, ctx: Any) -> object:
        """Attach a deterministic dense vector."""
        ctx.vector = [0.1, 0.2]
        return SimpleNamespace(sparse_vector=None)


class TestSessionMessageStore(unittest.IsolatedAsyncioTestCase):
    """Verify the session message chain stays direct and small."""

    async def test_message_writes_primary_record_appends_buffer(
        self,
    ) -> None:
        """Message follows raw-write/buffer/event/merge-request order."""
        vector_store = MagicMock()
        vector_store.upsert = AsyncMock(
            side_effect=lambda _collection, record: record["id"]
        )
        memory_events = _Events()
        buffer = SessionBuffer(
            collection_resolver=lambda: "context",
            merge_token_budget=1,
        )

        store = SessionStore(
            buffer=buffer,
            namespace=SimpleNamespace(
                session_immediate_uri=lambda **_kwargs: (
                    "opencortex://default/default/memories/events/immediate-1"
                ),
                session_events_parent=lambda session_id, **_kwargs: (
                    f"opencortex://default/default/memories/events/{session_id}"
                ),
            ),
            embedder=_Embedder(),
            writer=PrimaryRecordWriter(
                vector_store=vector_store,
                collection_resolver=lambda: "context",
                namespace=_Namespace(),
            ),
            events=StoreEvents(memory_events),
            config=SimpleNamespace(
                immediate_event_ttl_hours=24,
                merged_event_ttl_hours=72,
            ),
            ttl_from_hours=lambda hours: f"ttl:{hours}",
        )

        result = await store.message(
            SessionMessageInput(
                session_id="session-1",
                turn_id="turn-1",
                messages=[
                    SessionMessage(
                        role="assistant",
                        content="assistant: Alice moved to Hangzhou.",
                        meta={"topics": ["travel"], "entities": ["Alice"]},
                    )
                ],
                tool_calls=[{"name": "Read"}],
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.write_status, "ok")
        self.assertEqual(len(result.written_uris), 1)
        self.assertTrue(result.merge_requested)

        record = vector_store.upsert.await_args.args[1]
        self.assertEqual(record["context_type"], "memory")
        self.assertEqual(record["category"], "events")
        self.assertEqual(record["session_id"], "session-1")
        self.assertEqual(record["meta"]["layer"], "immediate")
        self.assertEqual(record["meta"]["turn_id"], "turn-1")
        self.assertEqual(record["meta"]["msg_index"], 0)
        self.assertEqual(record["meta"]["role"], "assistant")
        self.assertEqual(record["meta"]["tool_calls"], [{"name": "Read"}])
        self.assertEqual(record["ttl_expires_at"], "ttl:24")
        self.assertEqual(record["derive_status"], "ready")
        self.assertTrue(record["retrieval_ready"])
        self.assertEqual(record["content"], "assistant: Alice moved to Hangzhou.")
        self.assertEqual(record["abstract"], "assistant: Alice moved to Hangzhou.")
        self.assertEqual(record["entities"], ["Alice"])
        self.assertEqual(record["keywords"], "travel")
        self.assertEqual(record["retrieval_surface"], "l0_object")
        self.assertEqual(record["vector"], [0.1, 0.2])
        self.assertGreater(vector_store.upsert.await_count, 1)
        parent_record = vector_store.upsert.await_args_list[-2].args[1]
        self.assertFalse(parent_record["is_leaf"])
        self.assertEqual(parent_record["retrieval_surface"], "directory")

        key = buffer.session_key(
            collection="context",
            tenant_id="default",
            user_id="default",
            session_id="session-1",
        )
        snapshot = buffer.snapshot(key)
        self.assertIsNotNone(snapshot)
        self.assertEqual(
            snapshot.messages,
            ["assistant: Alice moved to Hangzhou."],
        )
        self.assertEqual(snapshot.immediate_uris, [record["uri"]])
        self.assertEqual(snapshot.tool_calls_per_turn, [[{"name": "Read"}]])

        self.assertEqual(
            [event.name for event in memory_events.events],
            [
                "memory_stored",
                "check_update",
                "session_turn_stored",
            ],
        )
        check_update_event = memory_events.events[1]
        self.assertEqual(check_update_event.uri, record["uri"])
        self.assertEqual(check_update_event.record_id, record["id"])
        turn_event = memory_events.events[2]
        self.assertEqual(turn_event.session_id, "session-1")
        self.assertEqual(turn_event.turn_id, "turn-1")
        self.assertEqual(turn_event.record_uris, [record["uri"]])

    async def test_merged_record_does_not_truncate_session_content(self) -> None:
        """Session merge preserves full content for worker derivation."""
        vector_store = MagicMock()
        vector_store.upsert = AsyncMock(
            side_effect=lambda _collection, record: record["id"]
        )
        memory_events = _Events()
        buffer = SessionBuffer(
            collection_resolver=lambda: "context",
            merge_token_budget=1,
        )
        content = "assistant: " + ("OpenCortex session detail. " * 80)
        namespace = SimpleNamespace(
            session_immediate_uri=lambda **_kwargs: (
                "opencortex://default/default/memories/events/immediate-1"
            ),
            session_events_parent=lambda session_id, **_kwargs: (
                f"opencortex://default/default/memories/events/{session_id}"
            ),
            session_merged_uri=lambda session_id, msg_range, **_kwargs: (
                f"opencortex://default/default/memories/events/{session_id}/"
                f"merged/{msg_range[0]}-{msg_range[1]}"
            ),
        )
        config = SimpleNamespace(
            immediate_event_ttl_hours=24,
            merged_event_ttl_hours=72,
        )
        writer = PrimaryRecordWriter(
            vector_store=vector_store,
            collection_resolver=lambda: "context",
            namespace=_Namespace(),
        )

        store = SessionStore(
            buffer=buffer,
            namespace=namespace,
            embedder=_Embedder(),
            writer=writer,
            events=StoreEvents(memory_events),
            config=config,
            ttl_from_hours=lambda hours: f"ttl:{hours}",
        )
        result = await store.message(
            SessionMessageInput(
                session_id="session-1",
                turn_id="turn-1",
                messages=[
                    SessionMessage(
                        role="assistant",
                        content=content,
                        meta={},
                    )
                ],
            )
        )

        self.assertTrue(result.merge_requested)
        key = buffer.session_key(
            collection="context",
            tenant_id="default",
            user_id="default",
            session_id="session-1",
        )
        merger = SessionMerger(
            buffer=buffer,
            namespace=namespace,
            writer=writer,
            events=StoreEvents(memory_events),
            config=config,
            ttl_from_hours=lambda hours: f"ttl:{hours}",
        )
        await merger.merge_unmerged(
            key,
            profile=IdentityProfile(session_id="session-1"),
        )

        merged_record = vector_store.upsert.await_args.args[1]
        self.assertEqual(merged_record["meta"]["layer"], "merged")
        self.assertEqual(merged_record["content"], content)
        self.assertEqual(merged_record["abstract"], "")
        self.assertEqual(merged_record["overview"], "")
        self.assertEqual(merged_record["ttl_expires_at"], "ttl:72")

    async def test_merge_uses_frozen_chunk_when_worker_lags(self) -> None:
        """Later immediate writes do not enlarge an already requested merge."""
        vector_store = MagicMock()
        vector_store.upsert = AsyncMock(
            side_effect=lambda _collection, record: record["id"]
        )
        memory_events = _Events()
        buffer = SessionBuffer(
            collection_resolver=lambda: "context",
            merge_token_budget=3,
        )
        namespace = SimpleNamespace(
            session_immediate_uri=lambda **_kwargs: (
                "opencortex://default/default/memories/events/immediate-1"
            ),
            session_events_parent=lambda session_id, **_kwargs: (
                f"opencortex://default/default/memories/events/{session_id}"
            ),
            session_merged_uri=lambda session_id, msg_range, **_kwargs: (
                f"opencortex://default/default/memories/events/{session_id}/"
                f"merged/{msg_range[0]}-{msg_range[1]}"
            ),
        )
        config = SimpleNamespace(
            immediate_event_ttl_hours=24,
            merged_event_ttl_hours=72,
        )
        writer = PrimaryRecordWriter(
            vector_store=vector_store,
            collection_resolver=lambda: "context",
            namespace=_Namespace(),
        )
        store = SessionStore(
            buffer=buffer,
            namespace=namespace,
            embedder=_Embedder(),
            writer=writer,
            events=StoreEvents(memory_events),
            config=config,
            ttl_from_hours=lambda hours: f"ttl:{hours}",
        )

        first = "a" * 12
        second = "b" * 12
        third = "c" * 12
        await store.message(
            SessionMessageInput(
                session_id="session-1",
                turn_id="turn-1",
                messages=[SessionMessage(role="user", content=first)],
            )
        )
        await store.message(
            SessionMessageInput(
                session_id="session-1",
                turn_id="turn-2",
                messages=[
                    SessionMessage(role="user", content=second),
                    SessionMessage(role="user", content=third),
                ],
            )
        )
        key = buffer.session_key(
            collection="context",
            tenant_id="default",
            user_id="default",
            session_id="session-1",
        )
        self.assertTrue(buffer.has_pending_merge(key))

        merger = SessionMerger(
            buffer=buffer,
            namespace=namespace,
            writer=writer,
            events=StoreEvents(memory_events),
            config=config,
            ttl_from_hours=lambda hours: f"ttl:{hours}",
        )
        await merger.merge_unmerged(
            key,
            profile=IdentityProfile(session_id="session-1"),
        )

        merged_record = vector_store.upsert.await_args.args[1]
        self.assertEqual(merged_record["meta"]["msg_range"], [0, 0])
        self.assertEqual(merged_record["content"], first)
        self.assertNotIn(second, merged_record["content"])
        self.assertNotIn(third, merged_record["content"])

        tail = buffer.snapshot(key)
        self.assertIsNotNone(tail)
        assert tail is not None
        self.assertEqual(tail.messages, [second])
