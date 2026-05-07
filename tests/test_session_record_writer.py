# SPDX-License-Identifier: Apache-2.0
"""Tests for the session record write boundary."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from opencortex.context.session_record_writer import SessionRecordWriter
from opencortex.core.context import Context
from opencortex.http.request_context import (
    reset_request_project_id,
    set_request_project_id,
)


class _EmbedResult:
    """Small embed result used by the writer tests."""

    dense_vector = [0.1, 0.2, 0.3, 0.4]
    sparse_vector = {"indices": [1], "values": [0.5]}


class _Embedder:
    """Minimal synchronous embedder stub."""

    def embed(self, text: str) -> _EmbedResult:
        self.last_text = text
        return _EmbedResult()


class TestSessionRecordWriter(unittest.IsolatedAsyncioTestCase):
    """Verify session writes follow primary/index/side-effect boundaries."""

    def _build_writer(self) -> tuple[SessionRecordWriter, Any]:
        storage = MagicMock()
        storage.upsert = AsyncMock(return_value="record-id")
        fs = MagicMock()
        fs.write_context = AsyncMock()
        entity_index = MagicMock()
        embedder = _Embedder()
        memory = SimpleNamespace(
            _config=SimpleNamespace(
                immediate_event_ttl_hours=24,
                merged_event_ttl_hours=72,
                context_flattening_enabled=True,
                embedding_provider="",
            ),
            _storage=storage,
            _fs=fs,
            _embedder=embedder,
            _entity_index=entity_index,
            _get_collection=MagicMock(return_value="context"),
            _ttl_from_hours=MagicMock(side_effect=lambda hours: f"ttl:{hours}"),
            _build_abstract_json=MagicMock(
                side_effect=lambda **kwargs: {
                    "memory_kind": "event",
                    "anchors": [{"text": "Alice", "value": "Alice"}],
                    "fact_points": [],
                    **kwargs,
                }
            ),
            _memory_object_payload=MagicMock(
                return_value={
                    "memory_kind": "event",
                    "anchor_hits": "Alice",
                    "merge_signature": "event:alice",
                    "mergeable": True,
                    "retrieval_surface": "l0_object",
                    "anchor_surface": True,
                }
            ),
            _sync_anchor_projection_records=AsyncMock(),
            _is_retryable_immediate_embed_exception=MagicMock(return_value=False),
            add=AsyncMock(),
            _memory_events=SimpleNamespace(events=[]),
        )
        memory._memory_events.publish_nowait = lambda event: (
            memory._memory_events.events.append(event)
        )
        manager = SimpleNamespace(_orchestrator=memory)
        return SessionRecordWriter(manager), memory

    async def test_immediate_message_writes_primary_then_indexes_and_fs(self) -> None:
        """Immediate writes use the same boundary shape as store writes."""
        writer, memory = self._build_writer()
        token = set_request_project_id("project-1")
        try:
            uri = await writer.write_immediate_message(
                session_id="session-1",
                msg_index=3,
                text="assistant: Alice moved to Hangzhou.",
                tenant_id="tenant",
                user_id="user",
                tool_calls=[{"name": "Read"}],
                meta={
                    "speaker": "assistant",
                    "entities": ["Alice"],
                    "topics": ["travel"],
                },
            )
        finally:
            reset_request_project_id(token)

        record = memory._storage.upsert.await_args.args[1]
        self.assertEqual(record["id"], "record-id")
        self.assertEqual(record["uri"], uri)
        self.assertEqual(record["scope"], "private")
        self.assertEqual(record["source_tenant_id"], "tenant")
        self.assertEqual(record["source_user_id"], "user")
        self.assertEqual(record["session_id"], "session-1")
        self.assertEqual(record["project_id"], "project-1")
        self.assertEqual(record["ttl_expires_at"], "ttl:24")
        self.assertEqual(record["meta"]["layer"], "immediate")
        self.assertEqual(record["meta"]["msg_index"], 3)
        self.assertEqual(record["meta"]["tool_calls"], [{"name": "Read"}])
        self.assertEqual(record["keywords"], "travel")
        self.assertEqual(record["entities"], ["Alice"])
        self.assertEqual(record["memory_kind"], "event")
        self.assertEqual(record["sparse_vector"], {"indices": [1], "values": [0.5]})
        self.assertEqual(
            memory._embedder.last_text,
            "[assistant] assistant: Alice moved to Hangzhou.",
        )

        memory._storage.upsert.assert_awaited_once_with("context", record)
        memory._sync_anchor_projection_records.assert_not_awaited()
        memory._entity_index.add.assert_not_called()
        memory._fs.write_context.assert_not_awaited()
        self.assertEqual(len(memory._memory_events.events), 1)
        event = memory._memory_events.events[0]
        self.assertEqual(event.name, "session_turn_stored")
        self.assertEqual(event.record_uris, [record["uri"]])

    async def test_add_session_record_writes_directly_without_facade_add(self) -> None:
        """Merged/end records do not call back into CortexMemory.add."""
        writer, memory = self._build_writer()
        target = SimpleNamespace(
            uri="opencortex://tenant/user/memories/events/merged",
            parent_uri="opencortex://tenant/user/memories/events",
            existing_record=None,
            meta={
                "layer": "merged",
                "session_id": "session-1",
                "topics": ["travel"],
                "entities": ["Alice"],
            },
            explicit_entities=["Alice"],
            explicit_topics=["travel"],
        )
        ctx = Context(
            uri=target.uri,
            parent_uri=target.parent_uri,
            is_leaf=True,
            abstract="merged abstract",
            overview="merged overview",
            context_type="memory",
            category="events",
            meta=target.meta,
            session_id="session-1",
        )
        assembled = SimpleNamespace(
            ctx=ctx,
            abstract="merged abstract",
            overview="merged overview",
            keywords="travel",
            keywords_list=["travel"],
            entities=["Alice"],
            meta=target.meta,
            effective_category="events",
            abstract_json={"memory_kind": "event", "anchors": []},
            object_payload={
                "memory_kind": "event",
                "anchor_hits": "",
                "merge_signature": "event:merged",
                "mergeable": True,
                "retrieval_surface": "l0_object",
                "anchor_surface": False,
            },
        )
        write_engine = SimpleNamespace(
            _context_builder=SimpleNamespace(
                resolve_target=AsyncMock(return_value=target),
                assemble_context=MagicMock(return_value=assembled),
            ),
            _write_derive_service=SimpleNamespace(
                derive_for_write=AsyncMock(
                    return_value=SimpleNamespace(
                        abstract="merged abstract",
                        overview="merged overview",
                        layers={},
                    )
                )
            ),
            _write_embed_service=SimpleNamespace(
                embed_for_write=AsyncMock(
                    return_value=SimpleNamespace(sparse_vector=None)
                )
            ),
        )
        memory._memory_service = SimpleNamespace(_memory_writer=write_engine)

        result = await writer.add_session_record(
            uri=target.uri,
            abstract="merged abstract",
            content="merged content",
            category="events",
            context_type="memory",
            session_id="session-1",
            tenant_id="tenant",
            user_id="user",
            is_leaf=True,
            meta=target.meta,
            overview="merged overview",
            defer_derive=True,
        )

        memory.add.assert_not_awaited()
        write_engine._context_builder.resolve_target.assert_awaited_once()
        write_engine._write_derive_service.derive_for_write.assert_awaited_once()
        write_engine._context_builder.assemble_context.assert_called_once()
        write_engine._write_embed_service.embed_for_write.assert_awaited_once_with(ctx)

        record = memory._storage.upsert.await_args.args[1]
        self.assertIs(result, ctx)
        self.assertEqual(record["uri"], target.uri)
        self.assertEqual(record["meta"]["layer"], "merged")
        self.assertEqual(record["ttl_expires_at"], "ttl:72")
        self.assertEqual(record["keywords"], "travel")
        self.assertEqual(record["entities"], ["Alice"])
        self.assertEqual(ctx.meta["dedup_action"], "created")
        self.assertEqual(len(memory._memory_events.events), 1)
        event = memory._memory_events.events[0]
        self.assertEqual(event.name, "memory_stored")
        self.assertEqual(event.content, "merged content")


if __name__ == "__main__":
    unittest.main()
