# SPDX-License-Identifier: Apache-2.0
"""Tests for ``SubsystemBootstrapper`` (Phase 5 of plan 015).

Boundary tests: construction, lazy property contract, and docstring
presence. Behavioral coverage for the moved methods continues to
live in the existing suites (``test_e2e_phase1.py``,
``test_perf_fixes.py``, etc.) which exercise them through the
orchestrator's init() delegate surface.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from opencortex.lifecycle.bootstrapper import SubsystemBootstrapper
from opencortex.retrieve.events import RecallCompletedEvent
from opencortex_app.store.event.events import MemoryEventManager, MemoryStoredEvent


class TestSubsystemBootstrapperConstruction(unittest.TestCase):
    """Smoke tests — class can be constructed safely."""

    def test_construct_with_mock_orchestrator(self) -> None:
        """Constructing with a mock stores the back-reference."""
        mock_orch = MagicMock()
        bs = SubsystemBootstrapper(mock_orch)
        self.assertIs(bs._orch, mock_orch)

    def test_construct_with_none_orchestrator_does_not_validate(self) -> None:
        """Construction does not validate the orchestrator eagerly."""
        bs = SubsystemBootstrapper(None)  # type: ignore[arg-type]
        self.assertIsNone(bs._orch)


class TestOrchestratorBootstrapperProperty(unittest.TestCase):
    """Lock the lazy-property contract for _bootstrapper."""

    def test_lazy_property_works_on_new_bypassed_orchestrator(self) -> None:
        """The lazy property works even on __new__ bypass objects."""
        from opencortex.orchestrator import MemoryOrchestrator

        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        bs = orch._bootstrapper
        self.assertIsNotNone(bs)
        self.assertIsInstance(bs, SubsystemBootstrapper)

    def test_lazy_property_caches_instance(self) -> None:
        """The lazy property returns the same bootstrapper instance."""
        from opencortex.orchestrator import MemoryOrchestrator

        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        a = orch._bootstrapper
        b = orch._bootstrapper
        self.assertIs(a, b)

    def test_bootstrapper_back_reference_points_to_orchestrator(self) -> None:
        """The bootstrapper keeps its parent memory facade reference."""
        from opencortex.orchestrator import MemoryOrchestrator

        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        bs = orch._bootstrapper
        self.assertIs(bs._orch, orch)


class TestAutophagyEventRegistration(unittest.IsolatedAsyncioTestCase):
    """Autophagy plugin handlers are event-bus subscribers."""

    async def test_registered_handlers_invoke_autophagy_kernel(self) -> None:
        """Events call autophagy without direct store/recall coupling."""
        mock_orch = MagicMock()
        mock_orch._memory_events = MemoryEventManager()
        mock_orch._autophagy_kernel = MagicMock()
        mock_orch._autophagy_kernel.initialize_owner = AsyncMock()
        mock_orch._autophagy_kernel.apply_recall_outcome = AsyncMock()
        mock_orch._initialize_autophagy_owner_state = AsyncMock()
        mock_orch._resolve_memory_owner_ids = AsyncMock(return_value=["record-1"])
        bs = SubsystemBootstrapper(mock_orch)

        bs._register_autophagy_event_handlers()

        mock_memory = MagicMock()
        memory_event = MemoryStoredEvent(
            uri="opencortex://tenant/user/memories/test",
            record_id="record-1",
            tenant_id="tenant",
            user_id="user",
            project_id="public",
            context_type="memory",
            category="general",
        )
        recall_event = RecallCompletedEvent(
            query="test",
            tenant_id="tenant",
            user_id="user",
            memories=[mock_memory],
        )

        mock_orch._memory_events.publish_nowait(memory_event)
        mock_orch._memory_events.publish_nowait(recall_event)
        await asyncio.sleep(0)
        await mock_orch._memory_events.close()

        mock_orch._initialize_autophagy_owner_state.assert_awaited_once()
        mock_orch._resolve_memory_owner_ids.assert_awaited_once_with([mock_memory])
        mock_orch._autophagy_kernel.apply_recall_outcome.assert_awaited_once()


class TestPrimaryRecordSideEffects(unittest.IsolatedAsyncioTestCase):
    """Primary write side effects run from event handlers."""

    async def test_memory_stored_updates_indexes_and_fs_async(self) -> None:
        """memory_stored drives projection, entity index, and CortexFS."""
        mock_orch = MagicMock()
        mock_orch._memory_events = MemoryEventManager()
        mock_orch._sync_anchor_projection_records = AsyncMock()
        mock_orch._entity_index = MagicMock()
        mock_orch._get_collection.return_value = "context"
        mock_orch._fs = MagicMock()
        mock_orch._fs.write_context = AsyncMock()
        bs = SubsystemBootstrapper(mock_orch)

        bs._register_primary_record_side_effects()

        record = {
            "id": "record-1",
            "uri": "opencortex://tenant/user/memories/test",
            "abstract": "summary",
            "overview": "overview",
            "is_leaf": True,
            "entities": ["Alice"],
            "abstract_json": {"memory_kind": "event"},
        }
        event = MemoryStoredEvent(
            uri=record["uri"],
            record_id="record-1",
            tenant_id="tenant",
            user_id="user",
            project_id="public",
            context_type="memory",
            category="events",
            content="full content",
            record=record,
        )

        mock_orch._memory_events.publish_nowait(event)
        await asyncio.sleep(0)
        await mock_orch._memory_events.close()

        mock_orch._sync_anchor_projection_records.assert_awaited_once_with(
            source_record=record,
            abstract_json={"memory_kind": "event"},
        )
        mock_orch._entity_index.add.assert_called_once_with(
            "context",
            "record-1",
            ["Alice"],
        )
        mock_orch._fs.write_context.assert_awaited_once_with(
            uri=record["uri"],
            content="full content",
            abstract="summary",
            abstract_json={"memory_kind": "event"},
            overview="overview",
            is_leaf=True,
        )

    async def test_memory_stored_updates_session_record_side_effects(self) -> None:
        """memory_stored drives primary side effects for a stored record."""
        mock_orch = MagicMock()
        mock_orch._memory_events = MemoryEventManager()
        mock_orch._sync_anchor_projection_records = AsyncMock()
        mock_orch._entity_index = None
        mock_orch._get_collection.return_value = "context"
        mock_orch._fs = MagicMock()
        mock_orch._fs.write_context = AsyncMock()
        bs = SubsystemBootstrapper(mock_orch)

        bs._register_primary_record_side_effects()

        record = {
            "id": "turn-1",
            "uri": "opencortex://tenant/user/memories/events/turn",
            "abstract": "hello",
            "overview": "",
            "is_leaf": True,
            "abstract_json": {},
        }
        event = MemoryStoredEvent(
            uri=record["uri"],
            record_id="turn-1",
            tenant_id="tenant",
            user_id="user",
            project_id="public",
            context_type="memory",
            category="events",
            content="hello",
            record=record,
        )

        mock_orch._memory_events.publish_nowait(event)
        await asyncio.sleep(0)
        await mock_orch._memory_events.close()

        mock_orch._sync_anchor_projection_records.assert_awaited_once()
        mock_orch._fs.write_context.assert_awaited_once_with(
            uri=record["uri"],
            content="hello",
            abstract="hello",
            abstract_json={},
            overview="",
            is_leaf=True,
        )


class TestDocstringPresence(unittest.TestCase):
    """Smoke test — every public method has a non-empty docstring."""

    _DOCUMENTED_METHODS = [
        "init",
        "_init_cognition",
        "_register_autophagy_event_handlers",
        "_init_alpha",
        "_init_skill_engine",
        "_create_default_embedder",
        "_create_local_embedder",
        "_startup_maintenance",
        "_check_and_reembed",
    ]

    def test_public_methods_have_docstrings(self) -> None:
        """Public bootstrapper methods keep docstrings."""
        for name in self._DOCUMENTED_METHODS:
            method = getattr(SubsystemBootstrapper, name)
            self.assertTrue(
                method.__doc__ and method.__doc__.strip(),
                f"SubsystemBootstrapper.{name} is missing a docstring",
            )


if __name__ == "__main__":
    unittest.main()
