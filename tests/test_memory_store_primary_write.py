# SPDX-License-Identifier: Apache-2.0
"""Tests for the memory store primary-write steps."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

from opencortex.core.context import Context
from opencortex.http.request_context import (
    reset_request_project_id,
    set_request_project_id,
)
from opencortex.services.memory_writer import MemoryWriter
from opencortex.store.event.events import MemoryStoredEvent


class _Events:
    """Capture published events for assertions."""

    def __init__(self) -> None:
        self.events: List[Any] = []

    def publish_nowait(self, event: Any) -> None:
        self.events.append(event)


class TestMemoryStorePrimaryWrite(unittest.IsolatedAsyncioTestCase):
    """Verify the explicit primary-write process calls."""

    def _build_writer(self) -> tuple[MemoryWriter, Any]:
        storage = MagicMock()
        storage.upsert = AsyncMock()
        deps = SimpleNamespace(
            config=SimpleNamespace(
                immediate_event_ttl_hours=2,
                merged_event_ttl_hours=48,
                context_flattening_enabled=False,
            ),
            storage=storage,
            fs=MagicMock(),
            embedder=None,
            memory_events=_Events(),
            entity_index=MagicMock(),
            memory_record_service=SimpleNamespace(
                _ttl_from_hours=MagicMock(side_effect=lambda hours: f"ttl:{hours}")
            ),
            derivation_service=MagicMock(),
            session_lifecycle_service=MagicMock(),
            ensure_init=MagicMock(),
            get_collection=MagicMock(return_value="context"),
            feedback=AsyncMock(),
            llm_completion=None,
            parser_registry=None,
            set_parser_registry=None,
            derive_queue=None,
            inflight_derive_uris=set(),
        )
        return MemoryWriter(deps), deps

    async def test_primary_write_process_builds_upserts_and_publishes(self) -> None:
        """Primary memory write does not run projection/entity/fs directly."""
        writer, deps = self._build_writer()
        token = set_request_project_id("project-9")
        try:
            ctx = Context(
                uri="opencortex://tenant/user/memories/preferences/test",
                parent_uri="opencortex://tenant/user/memories/preferences",
                is_leaf=True,
                abstract="short preference",
                overview="preference overview",
                context_type="memory",
                category="preferences",
                meta={
                    "source_doc_id": "doc-1",
                    "source_doc_title": "Doc One",
                    "source_section_path": "Root > Intro",
                    "chunk_role": "body",
                    "speaker": "Alice",
                    "event_date": "2026-04-28",
                },
                session_id="session-1",
                id="record-1",
            )
            ctx.vector = [0.1, 0.2, 0.3]
            abstract_json = {"summary": "short preference", "fact_points": ["fp1"]}
            record = writer.build_primary_record(
                ctx=ctx,
                abstract_json=abstract_json,
                object_payload={
                    "memory_kind": "profile",
                    "merge_signature": "sig",
                    "mergeable": True,
                },
                effective_category="preferences",
                keywords="alpha, beta",
                entities=["Alice"],
                meta=ctx.meta,
                context_type="memory",
                session_id="session-1",
                tenant_id="tenant",
                user_id="user",
                sparse_vector={"indices": [1], "values": [0.4]},
            )
            upsert_ms = await writer.upsert_primary_record(record)
            writer.publish_memory_stored(
                record=record,
                ctx=ctx,
                content="full content",
                tenant_id="tenant",
                user_id="user",
                context_type="memory",
                effective_category="preferences",
            )
        finally:
            reset_request_project_id(token)

        deps.storage.upsert.assert_awaited_once_with("context", record)
        deps.entity_index.add.assert_not_called()
        deps.fs.write_context.assert_not_called()
        self.assertIsInstance(upsert_ms, int)
        self.assertEqual(record["scope"], "private")
        self.assertEqual(record["source_tenant_id"], "tenant")
        self.assertEqual(record["source_user_id"], "user")
        self.assertEqual(record["project_id"], "project-9")
        self.assertEqual(record["session_id"], "session-1")
        self.assertEqual(record["keywords"], "alpha, beta")
        self.assertEqual(record["entities"], ["Alice"])
        self.assertEqual(record["abstract_json"], abstract_json)
        self.assertEqual(record["memory_kind"], "profile")
        self.assertEqual(record["source_doc_id"], "doc-1")
        self.assertEqual(record["source_doc_title"], "Doc One")
        self.assertEqual(record["source_section_path"], "Root > Intro")
        self.assertEqual(record["chunk_role"], "body")
        self.assertEqual(record["speaker"], "Alice")
        self.assertEqual(record["event_date"], "2026-04-28")
        self.assertEqual(record["ttl_expires_at"], "")

        event = deps.memory_events.events[0]
        self.assertIsInstance(event, MemoryStoredEvent)
        self.assertEqual(event.uri, ctx.uri)
        self.assertEqual(event.record_id, "record-1")
        self.assertEqual(event.project_id, "project-9")
        self.assertEqual(event.context_type, "memory")
        self.assertEqual(event.category, "preferences")
        self.assertEqual(event.content, "full content")
        self.assertEqual(event.record, record)

    async def test_staging_record_gets_immediate_ttl(self) -> None:
        """Staging records receive the immediate-event TTL."""
        writer, deps = self._build_writer()
        ctx = Context(
            uri="opencortex://tenant/user/memories/events/staging",
            abstract="temporary note",
            context_type="staging",
            category="events",
            id="staging-1",
        )

        record = writer.build_primary_record(
            ctx=ctx,
            abstract_json={},
            object_payload={},
            effective_category="events",
            keywords="",
            entities=[],
            meta={},
            context_type="staging",
            session_id=None,
            tenant_id="tenant",
            user_id="user",
            sparse_vector=None,
        )

        self.assertEqual(record["ttl_expires_at"], "ttl:2")
        deps.memory_record_service._ttl_from_hours.assert_called_with(2)
