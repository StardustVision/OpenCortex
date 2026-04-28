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
from opencortex.services.memory_signals import (
    MemorySignalBus,
    MemoryStoredSignal,
    RecallCompletedSignal,
)


class TestSubsystemBootstrapperConstruction(unittest.TestCase):
    """Smoke tests — class can be constructed safely."""

    def test_construct_with_mock_orchestrator(self) -> None:
        mock_orch = MagicMock()
        bs = SubsystemBootstrapper(mock_orch)
        self.assertIs(bs._orch, mock_orch)

    def test_construct_with_none_orchestrator_does_not_validate(self) -> None:
        bs = SubsystemBootstrapper(None)  # type: ignore[arg-type]
        self.assertIsNone(bs._orch)


class TestOrchestratorBootstrapperProperty(unittest.TestCase):
    """Lock the lazy-property contract for _bootstrapper."""

    def test_lazy_property_works_on_new_bypassed_orchestrator(self) -> None:
        from opencortex.orchestrator import MemoryOrchestrator

        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        bs = orch._bootstrapper
        self.assertIsNotNone(bs)
        self.assertIsInstance(bs, SubsystemBootstrapper)

    def test_lazy_property_caches_instance(self) -> None:
        from opencortex.orchestrator import MemoryOrchestrator

        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        a = orch._bootstrapper
        b = orch._bootstrapper
        self.assertIs(a, b)

    def test_bootstrapper_back_reference_points_to_orchestrator(self) -> None:
        from opencortex.orchestrator import MemoryOrchestrator

        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        bs = orch._bootstrapper
        self.assertIs(bs._orch, orch)


class TestAutophagySignalRegistration(unittest.IsolatedAsyncioTestCase):
    """Autophagy plugin handlers are signal-bus subscribers."""

    async def test_registered_handlers_invoke_autophagy_kernel(self) -> None:
        """Signals call autophagy without direct store/recall coupling."""
        mock_orch = MagicMock()
        mock_orch._memory_signal_bus = MemorySignalBus()
        mock_orch._autophagy_kernel = MagicMock()
        mock_orch._autophagy_kernel.initialize_owner = AsyncMock()
        mock_orch._autophagy_kernel.apply_recall_outcome = AsyncMock()
        mock_orch._initialize_autophagy_owner_state = AsyncMock()
        mock_orch._resolve_memory_owner_ids = AsyncMock(return_value=["record-1"])
        bs = SubsystemBootstrapper(mock_orch)

        bs._register_autophagy_signal_handlers()

        mock_memory = MagicMock()
        memory_signal = MemoryStoredSignal(
            uri="opencortex://tenant/user/memories/test",
            record_id="record-1",
            tenant_id="tenant",
            user_id="user",
            project_id="public",
            context_type="memory",
            category="general",
        )
        recall_signal = RecallCompletedSignal(
            query="test",
            tenant_id="tenant",
            user_id="user",
            memories=[mock_memory],
        )

        mock_orch._memory_signal_bus.publish_nowait(memory_signal)
        mock_orch._memory_signal_bus.publish_nowait(recall_signal)
        await asyncio.sleep(0)
        await mock_orch._memory_signal_bus.close()

        mock_orch._initialize_autophagy_owner_state.assert_awaited_once()
        mock_orch._resolve_memory_owner_ids.assert_awaited_once_with([mock_memory])
        mock_orch._autophagy_kernel.apply_recall_outcome.assert_awaited_once()


class TestDocstringPresence(unittest.TestCase):
    """Smoke test — every public method has a non-empty docstring."""

    _DOCUMENTED_METHODS = [
        "init",
        "_init_cognition",
        "_register_autophagy_signal_handlers",
        "_init_alpha",
        "_init_skill_engine",
        "_create_default_embedder",
        "_create_local_embedder",
        "_startup_maintenance",
        "_check_and_reembed",
    ]

    def test_public_methods_have_docstrings(self) -> None:
        for name in self._DOCUMENTED_METHODS:
            method = getattr(SubsystemBootstrapper, name)
            self.assertTrue(
                method.__doc__ and method.__doc__.strip(),
                f"SubsystemBootstrapper.{name} is missing a docstring",
            )


if __name__ == "__main__":
    unittest.main()
