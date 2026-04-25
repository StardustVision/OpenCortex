# SPDX-License-Identifier: Apache-2.0
"""Lifecycle tests for RerankClient (U2, plan 009).

Locks the contract added by the TCP CLOSE_WAIT leak fix:
- ``aclose()`` releases the lazy ``_http_client`` exactly once.
- Calling ``aclose()`` before any request (when ``_http_client is None``)
  is a safe no-op.
- Two consecutive ``aclose()`` calls don't raise (idempotent).
- ``_get_http_client`` applies ``httpx.Limits(max_connections=20,
  max_keepalive_connections=5)``.
- ``MemoryOrchestrator.init()`` builds exactly one ``RerankClient`` and
  the same instance survives across multiple admin requests
  (regression lock for the per-request leak that triggered the
  original incident).
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import httpx

from opencortex.retrieve.rerank_client import RerankClient
from opencortex.retrieve.rerank_config import RerankConfig


def _run(coro):
    return asyncio.run(coro)


class TestRerankClientAclose(unittest.TestCase):
    """Direct lifecycle tests on RerankClient.aclose()."""

    def test_aclose_when_http_client_uninitialized_is_safe(self):
        """aclose() before any API call (lazy client never built) is no-op."""

        async def check():
            client = RerankClient(RerankConfig())
            # _http_client is None at construction (lazy).
            self.assertIsNone(client._http_client)
            # Should NOT raise.
            await client.aclose()
            self.assertIsNone(client._http_client)

        _run(check())

    def test_aclose_releases_lazy_http_client(self):
        """aclose() calls inner client.aclose() exactly once when one exists."""

        async def check():
            client = RerankClient(RerankConfig())
            # Force the lazy client into being.
            spy = AsyncMock()
            spy.aclose = AsyncMock()
            client._http_client = spy
            await client.aclose()
            spy.aclose.assert_awaited_once()
            # Attribute is nulled so a second close sees "already closed".
            self.assertIsNone(client._http_client)

        _run(check())

    def test_aclose_is_idempotent(self):
        """Two consecutive aclose() calls don't raise."""

        async def check():
            client = RerankClient(RerankConfig())
            spy = AsyncMock()
            spy.aclose = AsyncMock()
            client._http_client = spy
            await client.aclose()
            await client.aclose()
            # Still only invoked once — second call hit the None guard.
            spy.aclose.assert_awaited_once()

        _run(check())

    def test_aclose_swallows_inner_failure(self):
        """A failing inner aclose is logged but doesn't propagate."""

        async def check():
            client = RerankClient(RerankConfig())
            failing = AsyncMock()
            failing.aclose = AsyncMock(side_effect=RuntimeError("net down"))
            client._http_client = failing
            # Should NOT raise.
            await client.aclose()
            failing.aclose.assert_awaited_once()

        _run(check())


class TestRerankClientLimits(unittest.TestCase):
    """_get_http_client applies the project-standard pool caps."""

    def test_get_http_client_passes_limits_kwarg(self):
        """The httpx.AsyncClient constructor receives limits=Limits(20, 5)."""

        async def check():
            captured: List[Dict[str, Any]] = []
            real_cls = httpx.AsyncClient

            class _CapturingClient(real_cls):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    captured.append(dict(kwargs))
                    super().__init__(*args, **kwargs)

            httpx.AsyncClient = _CapturingClient
            try:
                client = RerankClient(RerankConfig())
                inner = client._get_http_client()
                self.assertIsNotNone(inner)
                self.assertEqual(len(captured), 1)
                limits = captured[0].get("limits")
                self.assertIsNotNone(limits)
                self.assertEqual(limits.max_connections, 20)
                self.assertEqual(limits.max_keepalive_connections, 5)
            finally:
                httpx.AsyncClient = real_cls
                await client.aclose()

        _run(check())


class TestRerankClientSingletonContract(unittest.TestCase):
    """Regression lock for the original CLOSE_WAIT incident.

    Pre-fix, ``admin_search_debug`` constructed a fresh RerankClient
    per request → each admin call leaked one TCP socket. The fix lifts
    instantiation to ``MemoryOrchestrator.init()`` so the same instance
    serves every admin request for the process lifetime.

    The full orchestrator-init path is exercised in
    ``tests/test_orchestrator_close.py`` (U3) where the close-ordering
    contract is also locked. Here we just assert the singleton-shape
    invariant: many rerank calls reuse one client.
    """

    def test_repeated_rerank_calls_reuse_one_http_client(self):
        """100 rerank fanouts on the same RerankClient construct one client."""

        async def check():
            captured: List[Dict[str, Any]] = []
            real_cls = httpx.AsyncClient

            class _CapturingClient(real_cls):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    captured.append(dict(kwargs))
                    super().__init__(*args, **kwargs)

            httpx.AsyncClient = _CapturingClient
            try:
                client = RerankClient(RerankConfig())
                # Trigger the lazy http_client multiple times.
                for _ in range(100):
                    inner = client._get_http_client()
                # Exactly ONE httpx.AsyncClient was constructed despite
                # 100 calls — this is the regression lock against
                # re-introducing per-request instantiation.
                self.assertEqual(
                    len(captured), 1,
                    "RerankClient must reuse its lazy http_client across "
                    "calls; constructing a new one per call is the original "
                    "leak shape (project_connection_pool_leak.md)",
                )
                # Same instance returned each call.
                self.assertIs(inner, client._http_client)
            finally:
                httpx.AsyncClient = real_cls
                await client.aclose()

        _run(check())


if __name__ == "__main__":
    unittest.main()
