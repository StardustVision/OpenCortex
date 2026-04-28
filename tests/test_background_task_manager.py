# SPDX-License-Identifier: Apache-2.0
"""Tests for ``BackgroundTaskManager`` (Phase 3 of plan 014).

Boundary tests: construction, lazy property contract, start/close lifecycle,
and docstring presence. Behavioral coverage for the moved methods continues to
live in the existing suites (``test_perf_fixes.py``,
``test_connection_sweeper.py``, ``test_document_async_derive.py``) which
exercise them through the orchestrator's delegate surface.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from opencortex.lifecycle.background_tasks import BackgroundTaskManager


class TestBackgroundTaskManagerConstruction(unittest.TestCase):
    """Smoke tests — class can be constructed safely."""

    def test_construct_with_mock_orchestrator(self) -> None:
        mock_orch = MagicMock()
        mgr = BackgroundTaskManager(mock_orch)
        self.assertIs(mgr._orch, mock_orch)

    def test_construct_with_none_orchestrator_does_not_validate(self) -> None:
        mgr = BackgroundTaskManager(None)  # type: ignore[arg-type]
        self.assertIsNone(mgr._orch)


class TestOrchestratorBackgroundTaskManagerProperty(unittest.TestCase):
    """Lock the lazy-property contract for _background_task_manager."""

    def test_lazy_property_works_on_new_bypassed_orchestrator(self) -> None:
        from opencortex.orchestrator import MemoryOrchestrator

        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        mgr = orch._background_task_manager
        self.assertIsNotNone(mgr)
        self.assertIsInstance(mgr, BackgroundTaskManager)

    def test_lazy_property_caches_manager_instance(self) -> None:
        from opencortex.orchestrator import MemoryOrchestrator

        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        a = orch._background_task_manager
        b = orch._background_task_manager
        self.assertIs(a, b)

    def test_manager_back_reference_points_to_orchestrator(self) -> None:
        from opencortex.orchestrator import MemoryOrchestrator

        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        mgr = orch._background_task_manager
        self.assertIs(mgr._orch, orch)


class TestStartDeriveWorker(unittest.IsolatedAsyncioTestCase):
    """_start_derive_worker creates a named asyncio.Task."""

    async def test_start_derive_worker_creates_task(self) -> None:
        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.config import CortexConfig

        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        orch._config = CortexConfig()
        orch._derive_queue = asyncio.Queue()
        orch._derive_worker_task = None

        orch._background_task_manager._start_derive_worker()

        task = orch._derive_worker_task
        self.assertIsNotNone(task)
        self.assertFalse(task.done())

        # Clean up
        await orch._derive_queue.put(None)
        await asyncio.wait_for(task, timeout=1.0)


class TestCloseLifecycle(unittest.IsolatedAsyncioTestCase):
    """close() cancels all background tasks cleanly."""

    async def test_close_with_no_tasks_running(self) -> None:
        mock_orch = MagicMock()
        mock_orch._connection_sweep_task = None
        mock_orch._autophagy_startup_sweep_task = None
        mock_orch._autophagy_sweep_task = None
        mock_orch._memory_signal_bus = None
        mock_orch._derive_worker_task = None
        mgr = BackgroundTaskManager(mock_orch)
        # Should complete without error
        await mgr.close()

    async def test_close_cancels_connection_sweep_task(self) -> None:
        mock_orch = MagicMock()

        async def long_loop():
            await asyncio.sleep(60)

        task = asyncio.create_task(long_loop())
        mock_orch._connection_sweep_task = task
        mock_orch._autophagy_startup_sweep_task = None
        mock_orch._autophagy_sweep_task = None
        mock_orch._memory_signal_bus = None
        mock_orch._derive_worker_task = None

        mgr = BackgroundTaskManager(mock_orch)
        await mgr.close()

        self.assertTrue(task.cancelled() or task.done())

    async def test_close_sets_connection_sweep_task_none(self) -> None:
        mock_orch = MagicMock()
        mock_orch._connection_sweep_task = None
        mock_orch._autophagy_startup_sweep_task = None
        mock_orch._autophagy_sweep_task = None
        mock_orch._memory_signal_bus = None
        mock_orch._derive_worker_task = None

        mgr = BackgroundTaskManager(mock_orch)
        await mgr.close()

        self.assertIsNone(mock_orch._connection_sweep_task)


class TestDocstringPresence(unittest.TestCase):
    """Smoke test — every public method has a non-empty docstring."""

    _PUBLIC_METHODS = [
        "_start_autophagy_sweeper",
        "_run_autophagy_sweep_once",
        "_autophagy_sweep_loop",
        "_start_connection_sweeper",
        "_run_connection_sweep_once",
        "_start_derive_worker",
        "_recover_pending_derives",
        "_drain_derive_queue",
        "close",
    ]

    def test_public_methods_have_docstrings(self) -> None:
        for name in self._PUBLIC_METHODS:
            method = getattr(BackgroundTaskManager, name)
            self.assertTrue(
                method.__doc__ and method.__doc__.strip(),
                f"BackgroundTaskManager.{name} is missing a docstring",
            )


if __name__ == "__main__":
    unittest.main()
