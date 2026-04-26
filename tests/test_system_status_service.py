# SPDX-License-Identifier: Apache-2.0
"""Tests for ``SystemStatusService`` (Phase 4 of plan 013).

Boundary tests for the new service module: construction, lazy property
contract, method routing, and docstring presence. Integration coverage
of the moved methods continues to live in the existing test suites
(``tests/test_e2e_phase1.py``, ``tests/test_http_server.py``,
``tests/test_multi_tenant.py``) — those exercise the methods through
the orchestrator's public surface (delegate pattern).
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from opencortex.services.system_status_service import SystemStatusService


class TestSystemStatusServiceConstruction(unittest.TestCase):
    """Smoke tests — class can be constructed safely."""

    def test_construct_with_mock_orchestrator(self) -> None:
        """SystemStatusService(orch) succeeds and stores the back-reference."""
        mock_orch = MagicMock()
        service = SystemStatusService(mock_orch)
        self.assertIs(service._orch, mock_orch)

    def test_construct_with_none_orchestrator_does_not_validate(self) -> None:
        """Constructor stores the back-reference verbatim — no None guard."""
        service = SystemStatusService(None)  # type: ignore[arg-type]
        self.assertIsNone(service._orch)


class TestOrchestratorSystemStatusServiceProperty(unittest.TestCase):
    """Lock the lazy-property contract for _system_status_service."""

    def test_lazy_property_works_on_new_bypassed_orchestrator(self) -> None:
        """``__new__`` bypass + first ``_system_status_service`` access succeeds."""
        from opencortex.orchestrator import MemoryOrchestrator

        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        service = orch._system_status_service
        self.assertIsNotNone(service)
        self.assertIs(orch._system_status_service, service)

    def test_lazy_property_caches_service_instance(self) -> None:
        """Two reads return the same service instance (cached, not rebuilt)."""
        from opencortex.orchestrator import MemoryOrchestrator

        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        a = orch._system_status_service
        b = orch._system_status_service
        self.assertIs(a, b)


class TestHealthCheck(unittest.TestCase):
    """health_check delegates to storage and returns expected shape."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _make_service(self, *, initialized=True, storage_healthy=True):
        mock_orch = MagicMock()
        mock_orch._initialized = initialized
        mock_orch._embedder = MagicMock()
        mock_orch._llm_completion = MagicMock()
        mock_orch._storage = MagicMock()
        mock_orch._storage.health_check = AsyncMock(return_value=storage_healthy)
        return SystemStatusService(mock_orch)

    def test_health_check_returns_expected_keys(self) -> None:
        service = self._make_service()
        result = self._run(service.health_check())
        self.assertIn("initialized", result)
        self.assertIn("storage", result)
        self.assertIn("embedder", result)
        self.assertIn("llm", result)

    def test_health_check_initialized_true(self) -> None:
        service = self._make_service(initialized=True, storage_healthy=True)
        result = self._run(service.health_check())
        self.assertTrue(result["initialized"])
        self.assertTrue(result["storage"])

    def test_health_check_not_initialized_skips_storage(self) -> None:
        service = self._make_service(initialized=False)
        result = self._run(service.health_check())
        self.assertFalse(result["initialized"])
        self.assertFalse(result["storage"])


class TestSystemStatus(unittest.TestCase):
    """system_status routes to the correct sub-method."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _make_service(self):
        mock_orch = MagicMock()
        mock_orch._initialized = True
        mock_orch._embedder = MagicMock()
        mock_orch._llm_completion = MagicMock()
        mock_orch._storage = MagicMock()
        mock_orch._storage.health_check = AsyncMock(return_value=True)
        mock_orch._storage.get_stats = AsyncMock(return_value={"total_records": 0})
        mock_orch._build_rerank_config.return_value = MagicMock(
            is_available=lambda: False
        )
        mock_orch._config.rerank_model = None
        from opencortex.http.request_context import get_effective_identity
        return SystemStatusService(mock_orch)

    def test_system_status_health_routes_to_health_check(self) -> None:
        service = self._make_service()
        result = self._run(service.system_status("health"))
        self.assertIn("initialized", result)
        self.assertNotIn("issues", result)

    def test_system_status_doctor_includes_issues(self) -> None:
        service = self._make_service()
        result = self._run(service.system_status("doctor"))
        self.assertIn("issues", result)
        self.assertIn("initialized", result)


class TestDeriveStatus(unittest.TestCase):
    """derive_status returns correct status strings."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_derive_status_pending_when_inflight(self) -> None:
        mock_orch = MagicMock()
        mock_orch._inflight_derive_uris = {"opencortex://t/u/mem/cat/node1"}
        service = SystemStatusService(mock_orch)
        result = self._run(service.derive_status("opencortex://t/u/mem/cat/node1"))
        self.assertEqual(result["status"], "pending")

    def test_derive_status_not_found_when_no_records(self) -> None:
        mock_orch = MagicMock()
        mock_orch._inflight_derive_uris = set()
        mock_orch._fs._uri_to_path.return_value = "/fake/path"
        mock_orch._fs.agfs.read.side_effect = FileNotFoundError
        mock_orch._storage.filter = AsyncMock(return_value=[])
        mock_orch._get_collection.return_value = "memories"
        service = SystemStatusService(mock_orch)
        result = self._run(service.derive_status("opencortex://t/u/mem/cat/missing"))
        self.assertEqual(result["status"], "not_found")


class TestWaitDeferredDerives(unittest.TestCase):
    """wait_deferred_derives returns immediately when count is zero."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_returns_immediately_when_no_pending_derives(self) -> None:
        mock_orch = MagicMock()
        mock_orch._deferred_derive_count = 0
        service = SystemStatusService(mock_orch)
        # Should complete without sleeping
        self._run(service.wait_deferred_derives(poll_interval=0.001))


class TestDocstringPresence(unittest.TestCase):
    """Smoke test — every public method has a non-empty docstring."""

    _PUBLIC_METHODS = [
        "health_check",
        "stats",
        "system_status",
        "derive_status",
        "wait_deferred_derives",
        "reembed_all",
    ]

    def test_public_methods_have_docstrings(self) -> None:
        """All public SystemStatusService methods have non-empty docstrings."""
        for name in self._PUBLIC_METHODS:
            method = getattr(SystemStatusService, name)
            self.assertTrue(
                method.__doc__ and method.__doc__.strip(),
                f"SystemStatusService.{name} is missing a docstring",
            )


if __name__ == "__main__":
    unittest.main()
