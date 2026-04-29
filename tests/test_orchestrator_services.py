# SPDX-License-Identifier: Apache-2.0
"""Tests for MemoryOrchestratorServices registry behavior."""

import unittest

from opencortex.orchestrator import MemoryOrchestrator
from opencortex.services.memory_service import MemoryService
from opencortex.services.orchestrator_services import MemoryOrchestratorServices


class TestMemoryOrchestratorServices(unittest.TestCase):
    """Lock the orchestrator facade service-registry contract."""

    def test_services_registry_lazy_property_survives_new_bypass(self) -> None:
        """``__new__`` bypass still gets a lazy registry on first access."""
        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)

        registry = orch._services

        self.assertIsInstance(registry, MemoryOrchestratorServices)
        self.assertIs(orch._services, registry)

    def test_memory_service_property_uses_registry_cache(self) -> None:
        """Repeated facade property access returns the same service instance."""
        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)

        service = orch._memory_service

        self.assertIsInstance(service, MemoryService)
        self.assertIs(orch._memory_service, service)
        self.assertIs(orch._memory_service_instance, service)

    def test_existing_service_instance_cache_is_honored(self) -> None:
        """Directly seeded legacy cache attributes remain compatible."""
        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        sentinel = object()
        orch._memory_service_instance = sentinel

        self.assertIs(orch._memory_service, sentinel)

    def test_normal_constructor_defers_registry_until_first_access(self) -> None:
        """Normal construction keeps the registry lazy and cheap."""
        orch = MemoryOrchestrator()

        self.assertIsNone(orch._services_instance)
        registry = orch._services
        self.assertIsInstance(registry, MemoryOrchestratorServices)
        self.assertIs(orch._services_instance, registry)
