# SPDX-License-Identifier: Apache-2.0
"""Tests for CortexMemory service registry behavior."""

import unittest

from opencortex.cortex_memory import CortexMemory
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.services.memory_service import MemoryService
from opencortex.services.cortex_memory_services import CortexMemoryServices
from opencortex.services.orchestrator_services import MemoryOrchestratorServices


class TestCortexMemoryServices(unittest.TestCase):
    """Lock the memory facade service-registry contract."""

    def test_services_registry_lazy_property_survives_new_bypass(self) -> None:
        """``__new__`` bypass still gets a lazy registry on first access."""
        memory = CortexMemory.__new__(CortexMemory)

        registry = memory._services

        self.assertIsInstance(registry, CortexMemoryServices)
        self.assertIs(memory._services, registry)

    def test_memory_service_property_uses_registry_cache(self) -> None:
        """Repeated facade property access returns the same service instance."""
        memory = CortexMemory.__new__(CortexMemory)

        service = memory._memory_service

        self.assertIsInstance(service, MemoryService)
        self.assertIs(memory._memory_service, service)
        self.assertIs(memory._memory_service_instance, service)

    def test_existing_service_instance_cache_is_honored(self) -> None:
        """Directly seeded legacy cache attributes remain compatible."""
        memory = CortexMemory.__new__(CortexMemory)
        sentinel = object()
        memory._memory_service_instance = sentinel

        self.assertIs(memory._memory_service, sentinel)

    def test_normal_constructor_defers_registry_until_first_access(self) -> None:
        """Normal construction keeps the registry lazy and cheap."""
        memory = CortexMemory()

        self.assertIsNone(memory._services_instance)
        registry = memory._services
        self.assertIsInstance(registry, CortexMemoryServices)
        self.assertIs(memory._services_instance, registry)

    def test_memory_orchestrator_alias_keeps_new_bypass_compatibility(self) -> None:
        """Legacy alias still supports direct ``__new__`` bypass fixtures."""
        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)

        registry = orch._services

        self.assertIs(MemoryOrchestrator, CortexMemory)
        self.assertIs(MemoryOrchestratorServices, CortexMemoryServices)
        self.assertIsInstance(registry, CortexMemoryServices)
