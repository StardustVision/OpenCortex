# SPDX-License-Identifier: Apache-2.0
"""Tests for ``KnowledgeService`` (Phase 2 of plan 012).

Boundary tests for the new service module: construction, lazy property
contract, and docstring presence. Integration coverage of the moved
methods continues to live in the existing test suites
(``tests/test_phase2_shrinkage.py``, ``tests/test_alpha_http.py``)
— those exercise the methods through the orchestrator's public surface
(delegate pattern).
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from opencortex.services.knowledge_service import KnowledgeService


class TestKnowledgeServiceConstruction(unittest.TestCase):
    """Smoke tests — class can be constructed safely."""

    def test_construct_with_mock_orchestrator(self) -> None:
        """KnowledgeService(orch) succeeds and stores the back-reference."""
        mock_orch = MagicMock()
        service = KnowledgeService(mock_orch)
        self.assertIs(service._orch, mock_orch)

    def test_construct_with_none_orchestrator_does_not_validate(self) -> None:
        """Constructor stores the back-reference verbatim — no None guard."""
        service = KnowledgeService(None)  # type: ignore[arg-type]
        self.assertIsNone(service._orch)


class TestOrchestratorKnowledgeServiceProperty(unittest.TestCase):
    """Lock the lazy-property contract for _knowledge_service."""

    def test_lazy_property_works_on_new_bypassed_orchestrator(self) -> None:
        """``__new__`` bypass + first ``_knowledge_service`` access succeeds."""
        from opencortex.orchestrator import MemoryOrchestrator

        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        service = orch._knowledge_service
        self.assertIsNotNone(service)
        self.assertIs(orch._knowledge_service, service)

    def test_lazy_property_caches_service_instance(self) -> None:
        """Two reads return the same service instance (cached, not rebuilt)."""
        from opencortex.orchestrator import MemoryOrchestrator

        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        a = orch._knowledge_service
        b = orch._knowledge_service
        self.assertIs(a, b)


class TestDocstringPresence(unittest.TestCase):
    """Smoke test — every public method has a non-empty docstring."""

    _PUBLIC_METHODS = [
        "knowledge_search",
        "knowledge_approve",
        "knowledge_reject",
        "knowledge_list_candidates",
        "archivist_trigger",
        "archivist_status",
        "run_archivist",
    ]

    def test_public_methods_have_docstrings(self) -> None:
        """All public KnowledgeService methods have non-empty docstrings."""
        for name in self._PUBLIC_METHODS:
            method = getattr(KnowledgeService, name)
            self.assertTrue(
                method.__doc__ and method.__doc__.strip(),
                f"KnowledgeService.{name} is missing a docstring",
            )


if __name__ == "__main__":
    unittest.main()
