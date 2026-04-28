# SPDX-License-Identifier: Apache-2.0
"""Tests for memory store record assembly and persistence."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

from opencortex.core.context import Context
from opencortex.http.request_context import (
    reset_request_project_id,
    set_request_project_id,
)
from opencortex.services.memory_signals import MemoryStoredSignal
from opencortex.services.memory_store_record_service import (
    MemoryStoreRecordService,
)


class _SignalBus:
    """Capture published signals for assertions."""

    def __init__(self) -> None:
        self.signals: List[Any] = []

    def publish_nowait(self, signal: Any) -> None:
        self.signals.append(signal)


class TestMemoryStoreRecordService(unittest.IsolatedAsyncioTestCase):
    """Verify the extracted store persistence boundary."""

    def _build_service(self) -> tuple[MemoryStoreRecordService, Any]:
        storage = MagicMock()
        storage.upsert = AsyncMock()
        fs = MagicMock()
        fs.write_context = AsyncMock()
        signal_bus = _SignalBus()
        entity_index = MagicMock()
        orch = SimpleNamespace(
            _storage=storage,
            _fs=fs,
            _memory_signal_bus=signal_bus,
            _entity_index=entity_index,
            _config=SimpleNamespace(
                immediate_event_ttl_hours=2,
                merged_event_ttl_hours=48,
            ),
            _get_collection=MagicMock(return_value="context"),
            _sync_anchor_projection_records=AsyncMock(),
            _ttl_from_hours=MagicMock(side_effect=lambda hours: f"ttl:{hours}"),
        )
        write_service = SimpleNamespace(_orch=orch)
        return MemoryStoreRecordService(write_service), orch

    async def test_persist_context_record_assembles_and_persists_record(self) -> None:
        """The service owns normal store record payload construction."""
        service, orch = self._build_service()
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
            object_payload = {
                "memory_kind": "profile",
                "merge_signature": "sig",
                "mergeable": True,
            }

            result = await service.persist_context_record(
                ctx=ctx,
                content="full content",
                abstract_json=abstract_json,
                object_payload=object_payload,
                effective_category="preferences",
                keywords="alpha, beta",
                entities=["Alice"],
                meta=ctx.meta,
                context_type="memory",
                session_id="session-1",
                tenant_id="tenant",
                user_id="user",
                sparse_vector={"indices": [1], "values": [0.4]},
                is_leaf=True,
            )
            await asyncio.sleep(0)
        finally:
            reset_request_project_id(token)

        record = result.record
        orch._storage.upsert.assert_awaited_once_with("context", record)
        orch._sync_anchor_projection_records.assert_awaited_once_with(
            source_record=record,
            abstract_json=abstract_json,
        )
        orch._entity_index.add.assert_called_once_with("context", "record-1", ["Alice"])
        orch._fs.write_context.assert_awaited_once_with(
            uri=ctx.uri,
            content="full content",
            abstract=ctx.abstract,
            abstract_json=abstract_json,
            overview=ctx.overview,
            is_leaf=True,
        )

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
        self.assertIsInstance(result.upsert_ms, int)

        self.assertEqual(len(orch._memory_signal_bus.signals), 1)
        signal = orch._memory_signal_bus.signals[0]
        self.assertIsInstance(signal, MemoryStoredSignal)
        self.assertEqual(signal.uri, ctx.uri)
        self.assertEqual(signal.record_id, "record-1")
        self.assertEqual(signal.project_id, "project-9")
        self.assertEqual(signal.context_type, "memory")
        self.assertEqual(signal.category, "preferences")
        self.assertEqual(signal.record, record)

    async def test_staging_record_gets_immediate_ttl(self) -> None:
        """Staging records receive the immediate-event TTL."""
        service, orch = self._build_service()
        ctx = Context(
            uri="opencortex://tenant/user/memories/events/staging",
            abstract="temporary note",
            context_type="staging",
            category="events",
            id="staging-1",
        )

        result = await service.persist_context_record(
            ctx=ctx,
            content="",
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
            is_leaf=True,
        )
        await asyncio.sleep(0)

        self.assertEqual(result.record["ttl_expires_at"], "ttl:2")
        orch._ttl_from_hours.assert_called_with(2)

    async def test_merged_event_memory_gets_merged_event_ttl(self) -> None:
        """Merged event memories receive the merged-event TTL."""
        service, orch = self._build_service()
        ctx = Context(
            uri="opencortex://tenant/user/memories/events/merged",
            abstract="merged event",
            context_type="memory",
            category="events",
            meta={"layer": "merged"},
            id="merged-1",
        )

        result = await service.persist_context_record(
            ctx=ctx,
            content="",
            abstract_json={},
            object_payload={},
            effective_category="events",
            keywords="",
            entities=[],
            meta=ctx.meta,
            context_type="memory",
            session_id=None,
            tenant_id="tenant",
            user_id="user",
            sparse_vector=None,
            is_leaf=True,
        )
        await asyncio.sleep(0)

        self.assertEqual(result.record["ttl_expires_at"], "ttl:48")
        orch._ttl_from_hours.assert_called_with(48)
