# SPDX-License-Identifier: Apache-2.0
"""Tests for the direct session message store flow."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from opencortex.store.embedder import StoreEmbedder
from opencortex.store.event.events import StoreEvents
from opencortex.store.schemas import SessionMessage, SessionMessageInput
from opencortex.store.session.buffer import SessionBuffer
from opencortex.store.session.store import SessionStore
from opencortex.writer.primary_record_writer import PrimaryRecordWriter


class _EmbedResult:
    """Small embed result used by store tests."""

    dense_vector = [0.1, 0.2, 0.3, 0.4]
    sparse_vector = None


class _Embedder:
    """Minimal synchronous embedder stub."""

    def embed(self, text: str) -> _EmbedResult:
        self.last_text = text
        return _EmbedResult()


class _Events:
    """Capture published events."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish_nowait(self, event: Any) -> None:
        self.events.append(event)


class TestSessionMessageStore(unittest.IsolatedAsyncioTestCase):
    """Verify the session message chain stays direct and small."""

    async def test_message_writes_primary_record_appends_buffer(
        self,
    ) -> None:
        """Message follows build/embed/write/buffer/event/merge-request order."""
        storage = MagicMock()
        storage.upsert = AsyncMock(side_effect=lambda _collection, record: record["id"])
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
            embedder=StoreEmbedder(_Embedder()),
            writer=PrimaryRecordWriter(
                config=SimpleNamespace(
                    immediate_event_ttl_hours=24,
                    merged_event_ttl_hours=72,
                ),
                storage=storage,
                collection_resolver=lambda: "context",
                ttl_from_hours=lambda hours: f"ttl:{hours}",
            ),
            events=StoreEvents(memory_events),
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

        record = storage.upsert.await_args.args[1]
        self.assertEqual(record["context_type"], "memory")
        self.assertEqual(record["category"], "events")
        self.assertEqual(record["session_id"], "session-1")
        self.assertEqual(record["meta"]["layer"], "immediate")
        self.assertEqual(record["meta"]["turn_id"], "turn-1")
        self.assertEqual(record["meta"]["msg_index"], 0)
        self.assertEqual(record["meta"]["role"], "assistant")
        self.assertEqual(record["meta"]["tool_calls"], [{"name": "Read"}])
        self.assertEqual(record["ttl_expires_at"], "ttl:24")
        self.assertEqual(record["keywords"], "travel")
        self.assertEqual(record["entities"], ["Alice"])

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
                "session_turn_stored",
            ],
        )
        turn_event = memory_events.events[0]
        self.assertEqual(turn_event.session_id, "session-1")
        self.assertEqual(turn_event.turn_id, "turn-1")
        self.assertEqual(turn_event.record_uris, [record["uri"]])
