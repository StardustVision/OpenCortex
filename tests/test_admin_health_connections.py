# SPDX-License-Identifier: Apache-2.0
"""Tests for /api/v1/admin/health/connections (U4, plan 009).

Locks the pool-visibility endpoint contract:
- Auth: admin-only (non-admin -> 403).
- Shape: returns ``status``, ``clients``, ``sweeper`` keys.
- Edge case: when a client is uninitialized (orchestrator hasn't built
  it yet, or it's a bare callable with no ``.client``), the endpoint
  returns ``stats_source: uninitialized`` rather than crash.
- Edge case: when stat extraction fails internally, the endpoint still
  returns 200 with ``stats_source: unavailable`` and a ``reason``.
- Status thresholds: a pool past 80% utilization tips the top-level
  ``status`` to ``degraded``.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from opencortex.http.admin_routes import (
    _classify_pool_status,
    _extract_pool_stats,
    admin_health_connections,
)


def _run(coro):
    return asyncio.run(coro)


class _FakePool:
    """Stand-in for ``client._transport._pool`` with N connections."""

    def __init__(self, open_count: int, idle_count: int):
        # Build N connection objects; first ``idle_count`` are idle.
        conns = []
        for i in range(open_count):
            conn = MagicMock()
            conn.is_idle = MagicMock(return_value=(i < idle_count))
            conns.append(conn)
        self.connections = conns


class _FakeClient:
    """Mimics httpx.AsyncClient enough for ``_extract_pool_stats``."""

    def __init__(self, *, max_connections: int, max_keepalive: int,
                 open_count: int, idle_count: int):
        self._limits = MagicMock()
        self._limits.max_connections = max_connections
        self._limits.max_keepalive_connections = max_keepalive
        self._transport = MagicMock()
        self._transport._pool = _FakePool(open_count, idle_count)


class TestExtractPoolStats(unittest.TestCase):
    """Unit tests for the helper. Cheap and isolated."""

    def test_handles_none_client(self):
        out = _extract_pool_stats(None)
        self.assertEqual(out["stats_source"], "unavailable")
        self.assertIn("reason", out)
        self.assertIsNone(out["open_connections"])

    def test_returns_pool_counts_and_limits(self):
        client = _FakeClient(
            max_connections=20, max_keepalive=5, open_count=3, idle_count=2,
        )
        out = _extract_pool_stats(client)
        self.assertEqual(out["stats_source"], "transport_pool")
        self.assertEqual(out["open_connections"], 3)
        self.assertEqual(out["keepalive_connections"], 2)
        self.assertEqual(out["limits"]["max_connections"], 20)
        self.assertEqual(out["limits"]["max_keepalive_connections"], 5)

    def test_handles_missing_transport(self):
        """A client without _transport returns unavailable, not crash."""
        client = MagicMock(spec=[])  # no attributes
        out = _extract_pool_stats(client)
        self.assertEqual(out["stats_source"], "unavailable")

    def test_handles_pool_walk_failure(self):
        """An exception inside the pool walk reports unavailable."""
        client = MagicMock()
        # Make accessing transport raise.
        type(client)._transport = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        out = _extract_pool_stats(client)
        self.assertEqual(out["stats_source"], "unavailable")
        self.assertIn("reason", out)


class TestClassifyPoolStatus(unittest.TestCase):
    """Threshold logic — degrade at 80% utilization."""

    def test_below_threshold_is_healthy(self):
        stats = {
            "stats_source": "transport_pool",
            "open_connections": 10,
            "limits": {"max_connections": 20},
        }
        self.assertEqual(_classify_pool_status(stats), "healthy")

    def test_at_eighty_percent_is_healthy(self):
        """80% exactly is still healthy — only ABOVE 80% degrades."""
        stats = {
            "stats_source": "transport_pool",
            "open_connections": 16,  # 16/20 = 80% exactly
            "limits": {"max_connections": 20},
        }
        self.assertEqual(_classify_pool_status(stats), "healthy")

    def test_above_threshold_is_degraded(self):
        stats = {
            "stats_source": "transport_pool",
            "open_connections": 17,  # 17/20 = 85% > 80%
            "limits": {"max_connections": 20},
        }
        self.assertEqual(_classify_pool_status(stats), "degraded")

    def test_unavailable_when_no_stats(self):
        stats = {"stats_source": "unavailable"}
        self.assertEqual(_classify_pool_status(stats), "unavailable")


class TestAdminHealthConnectionsEndpoint(unittest.TestCase):
    """End-to-end shape tests on the route handler.

    ``_require_admin()`` is patched to no-op so we don't need to
    construct a real JWT context. Auth itself is locked by the
    existing admin-route test patterns and is_admin() unit tests.
    """

    def _bare_orch(self, *, llm_client=None, rerank_client_inner=None,
                   sweeper_attrs=None):
        """Build a minimal stand-in orchestrator for the endpoint."""
        orch = MagicMock(spec=[])  # only attributes we set explicitly
        if llm_client is not None:
            llm_completion = MagicMock()
            llm_completion.client = llm_client
            llm_completion.backend = "openai"
            orch._llm_completion = llm_completion
        else:
            orch._llm_completion = None
        if rerank_client_inner is not None:
            rerank = MagicMock()
            rerank._http_client = rerank_client_inner
            orch._rerank_client = rerank
        else:
            orch._rerank_client = None
        # Sweeper fields default to None / not_started; tests can override.
        for k, v in (sweeper_attrs or {}).items():
            setattr(orch, k, v)
        cfg = MagicMock()
        cfg.connection_sweep_interval_seconds = 600
        orch._config = cfg
        return orch

    def _call_endpoint(self, orch):
        with patch("opencortex.http.admin_routes._require_admin", lambda: None), \
             patch("opencortex.http.admin_routes._orchestrator", orch):
            return _run(admin_health_connections())

    def test_endpoint_returns_expected_shape(self):
        client = _FakeClient(
            max_connections=20, max_keepalive=5, open_count=3, idle_count=2,
        )
        orch = self._bare_orch(llm_client=client, rerank_client_inner=client)
        body = self._call_endpoint(orch)
        self.assertIn("status", body)
        self.assertIn("clients", body)
        self.assertIn("sweeper", body)
        self.assertIn("llm_completion", body["clients"])
        self.assertIn("rerank", body["clients"])
        self.assertEqual(body["clients"]["llm_completion"]["backend"], "openai")
        self.assertEqual(body["clients"]["llm_completion"]["open_connections"], 3)
        self.assertEqual(body["status"], "healthy")

    def test_uninitialized_clients_report_uninitialized(self):
        """No LLM wrapper, no rerank client -> uninitialized, not crash."""
        orch = self._bare_orch()  # both None
        body = self._call_endpoint(orch)
        self.assertEqual(
            body["clients"]["llm_completion"]["stats_source"], "uninitialized",
        )
        self.assertEqual(
            body["clients"]["rerank"]["stats_source"], "uninitialized",
        )
        # Top-level status is "unavailable" when nothing is healthy.
        self.assertEqual(body["status"], "unavailable")

    def test_pool_above_threshold_marks_status_degraded(self):
        """A single client past 80% tips the top-level status."""
        # 18/20 = 90% utilization -> degraded.
        hot_client = _FakeClient(
            max_connections=20, max_keepalive=5, open_count=18, idle_count=0,
        )
        cool_client = _FakeClient(
            max_connections=20, max_keepalive=5, open_count=2, idle_count=1,
        )
        orch = self._bare_orch(
            llm_client=hot_client, rerank_client_inner=cool_client,
        )
        body = self._call_endpoint(orch)
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(
            _classify_pool_status(body["clients"]["llm_completion"]),
            "degraded",
        )

    def test_stat_extraction_failure_does_not_500(self):
        """Internal failure inside _extract_pool_stats yields unavailable, not 500."""
        bad_client = MagicMock()
        # Force a failure path inside the helper.
        type(bad_client)._transport = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("transport gone"))
        )
        orch = self._bare_orch(llm_client=bad_client)
        body = self._call_endpoint(orch)
        self.assertEqual(
            body["clients"]["llm_completion"]["stats_source"], "unavailable",
        )
        self.assertIn("reason", body["clients"]["llm_completion"])

    def test_sweeper_fields_present_with_iso_timestamp(self):
        """When U5 lands and sets _last_connection_sweep_at, it ISO-formats."""
        from datetime import datetime, timezone

        ts = datetime(2026, 4, 25, 21, 0, 0, tzinfo=timezone.utc)
        orch = self._bare_orch(sweeper_attrs={
            "_last_connection_sweep_at": ts,
            "_last_connection_sweep_status": "ok",
        })
        body = self._call_endpoint(orch)
        self.assertEqual(body["sweeper"]["last_sweep_at"], ts.isoformat())
        self.assertEqual(body["sweeper"]["last_sweep_status"], "ok")
        self.assertEqual(body["sweeper"]["interval_seconds"], 600)


if __name__ == "__main__":
    unittest.main()
