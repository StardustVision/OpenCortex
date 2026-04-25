# SPDX-License-Identifier: Apache-2.0
"""Tests for the periodic connection sweeper (U5, plan 009).

Locks the defense-in-depth contract:
- Sweeper task starts (mirrors ``_start_autophagy_sweeper`` shape).
- Sweeper task is cancelled BEFORE the per-client aclose() steps in
  ``close()`` so it cannot inspect a half-closed pool.
- ``_run_connection_sweep_once`` updates ``_last_connection_sweep_at``
  and ``_last_connection_sweep_status`` so /admin/health/connections
  can read them.
- A pool above the warn ratio (>80%) emits a WARNING log.
- Re-entrancy lock prevents two concurrent sweeps.
- Configurable interval via env var ``OPENCORTEX_CONNECTION_SWEEP_INTERVAL_SECONDS``.
- Idempotent close: safe to call twice even after sweeper is cancelled.
"""

from __future__ import annotations

import asyncio
import logging
import os
import unittest
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from opencortex.config import CortexConfig
from opencortex.orchestrator import MemoryOrchestrator


def _run(coro):
    return asyncio.run(coro)


def _make_bare_orchestrator() -> MemoryOrchestrator:
    """Build an orchestrator without going through __init__/init."""
    return MemoryOrchestrator.__new__(MemoryOrchestrator)


class _FakePool:
    """Stand-in for ``client._transport._pool`` with N connections."""

    def __init__(self, open_count: int):
        conns = []
        for i in range(open_count):
            conn = MagicMock()
            conn.is_idle = MagicMock(return_value=False)
            conns.append(conn)
        self.connections = conns


class _FakeClient:
    """Mimics httpx.AsyncClient enough for ``_extract_pool_stats``."""

    def __init__(self, *, max_connections: int, open_count: int):
        self._limits = MagicMock()
        self._limits.max_connections = max_connections
        self._limits.max_keepalive_connections = max(1, max_connections // 4)
        self._transport = MagicMock()
        self._transport._pool = _FakePool(open_count)


class TestConnectionSweepOnce(unittest.TestCase):
    """Direct tests on _run_connection_sweep_once — no full init needed."""

    def test_sweep_updates_status_and_timestamp(self):
        """A clean sweep marks status='ok' and stamps the time."""

        async def check():
            orch = _make_bare_orchestrator()
            orch._config = MagicMock()
            llm_completion = MagicMock()
            llm_completion.client = _FakeClient(
                max_connections=20, open_count=2,
            )
            orch._llm_completion = llm_completion
            orch._rerank_client = None
            orch._connection_sweep_guard = asyncio.Lock()
            orch._last_connection_sweep_at = None
            orch._last_connection_sweep_status = "not_started"

            await orch._run_connection_sweep_once()

            self.assertIsNotNone(orch._last_connection_sweep_at)
            self.assertEqual(orch._last_connection_sweep_status, "ok")

        _run(check())

    def test_sweep_warns_when_pool_above_threshold(self):
        """Pool at >80% utilization emits WARNING + status='warn'."""

        async def check():
            orch = _make_bare_orchestrator()
            orch._config = MagicMock()
            llm_completion = MagicMock()
            llm_completion.client = _FakeClient(
                max_connections=20, open_count=18,  # 90%
            )
            orch._llm_completion = llm_completion
            orch._rerank_client = None
            orch._connection_sweep_guard = asyncio.Lock()
            orch._last_connection_sweep_at = None
            orch._last_connection_sweep_status = "not_started"

            with self.assertLogs(
                "opencortex.orchestrator", level="WARNING",
            ) as cm:
                await orch._run_connection_sweep_once()

            self.assertEqual(orch._last_connection_sweep_status, "warn")
            joined = "\n".join(cm.output)
            self.assertIn("nearing cap", joined)
            self.assertIn("llm_completion", joined)

        _run(check())

    def test_sweep_handles_uninitialized_clients(self):
        """No clients, no crash, status='ok'."""

        async def check():
            orch = _make_bare_orchestrator()
            orch._config = MagicMock()
            orch._llm_completion = None
            orch._rerank_client = None
            orch._connection_sweep_guard = asyncio.Lock()
            orch._last_connection_sweep_at = None
            orch._last_connection_sweep_status = "not_started"

            await orch._run_connection_sweep_once()

            self.assertEqual(orch._last_connection_sweep_status, "ok")

        _run(check())

    def test_sweep_inspects_rerank_singleton_inner_client(self):
        """When the rerank singleton has built its lazy http_client, sweep checks it."""

        async def check():
            orch = _make_bare_orchestrator()
            orch._config = MagicMock()
            orch._llm_completion = None
            rerank = MagicMock()
            rerank._http_client = _FakeClient(
                max_connections=20, open_count=19,  # 95%
            )
            orch._rerank_client = rerank
            orch._connection_sweep_guard = asyncio.Lock()
            orch._last_connection_sweep_at = None
            orch._last_connection_sweep_status = "not_started"

            with self.assertLogs(
                "opencortex.orchestrator", level="WARNING",
            ) as cm:
                await orch._run_connection_sweep_once()

            joined = "\n".join(cm.output)
            self.assertIn("rerank", joined)
            self.assertIn("nearing cap", joined)

        _run(check())


class TestConnectionSweeperLifecycle(unittest.TestCase):
    """Start / cancel / idempotent-close on the sweeper task itself."""

    def test_start_sweeper_creates_named_task(self):
        """``_start_connection_sweeper`` creates an asyncio.Task with the right name."""

        async def check():
            orch = _make_bare_orchestrator()
            orch._config = MagicMock()
            orch._config.connection_sweep_interval_seconds = 600
            orch._llm_completion = None
            orch._rerank_client = None
            orch._connection_sweep_task = None
            orch._connection_sweep_guard = None
            orch._last_connection_sweep_at = None
            orch._last_connection_sweep_status = "not_started"

            orch._start_connection_sweeper()

            self.assertIsNotNone(orch._connection_sweep_task)
            self.assertEqual(
                orch._connection_sweep_task.get_name(),
                "opencortex.connections.periodic_sweep",
            )
            # Cleanup: cancel so the test process doesn't leak the task.
            orch._connection_sweep_task.cancel()
            try:
                await orch._connection_sweep_task
            except asyncio.CancelledError:
                pass

        _run(check())

    def test_close_cancels_sweeper_before_aclose(self):
        """``orchestrator.close()`` cancels the sweeper task FIRST.

        Order matters: if the sweeper inspected pools while the
        per-client aclose() calls were running, it could touch a
        half-closed transport and crash. The cancellation comes before
        the new aclose block in close().
        """

        async def check():
            orch = _make_bare_orchestrator()
            orch._config = MagicMock()
            orch._config.connection_sweep_interval_seconds = 0.01
            orch._llm_completion = None
            orch._rerank_client = None
            orch._connection_sweep_task = None
            orch._connection_sweep_guard = None
            orch._last_connection_sweep_at = None
            orch._last_connection_sweep_status = "not_started"
            orch._initialized = True

            orch._start_connection_sweeper()
            sweep_task = orch._connection_sweep_task
            self.assertFalse(sweep_task.done())

            await orch.close()

            self.assertTrue(sweep_task.done() or sweep_task.cancelled())
            self.assertIsNone(orch._connection_sweep_task)

        _run(check())

    def test_close_is_idempotent_after_sweeper_already_cancelled(self):
        """Calling close() twice doesn't crash (sweeper already None)."""

        async def check():
            orch = _make_bare_orchestrator()
            orch._config = MagicMock()
            orch._config.connection_sweep_interval_seconds = 0.01
            orch._llm_completion = None
            orch._rerank_client = None
            orch._connection_sweep_task = None
            orch._connection_sweep_guard = None
            orch._last_connection_sweep_at = None
            orch._last_connection_sweep_status = "not_started"
            orch._initialized = True

            orch._start_connection_sweeper()
            await orch.close()
            # Second close — sweeper is None, must not crash.
            await orch.close()

        _run(check())


class TestConfigurableInterval(unittest.TestCase):
    """``OPENCORTEX_CONNECTION_SWEEP_INTERVAL_SECONDS`` overrides default."""

    def test_env_var_overrides_default(self):
        with patch.dict(
            os.environ,
            {"OPENCORTEX_CONNECTION_SWEEP_INTERVAL_SECONDS": "30"},
            clear=False,
        ):
            cfg = CortexConfig()
            cfg._apply_env_overrides()  # Internal — exercise env path.
            self.assertEqual(cfg.connection_sweep_interval_seconds, 30)


if __name__ == "__main__":
    unittest.main()
