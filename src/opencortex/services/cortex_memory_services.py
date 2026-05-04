# SPDX-License-Identifier: Apache-2.0
"""Lazy service registry for the CortexMemory facade."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, TypeVar

if TYPE_CHECKING:
    from opencortex.cortex_memory import CortexMemory
    from opencortex.lifecycle.background_tasks import BackgroundTaskManager
    from opencortex.lifecycle.bootstrapper import SubsystemBootstrapper
    from opencortex.services.derivation_service import DerivationService
    from opencortex.services.knowledge_service import KnowledgeService
    from opencortex.services.memory_admin_stats_service import (
        MemoryAdminStatsService,
    )
    from opencortex.services.memory_record_service import MemoryRecordService
    from opencortex.services.memory_service import MemoryService
    from opencortex.services.memory_sharing_service import MemorySharingService
    from opencortex.services.model_runtime_service import ModelRuntimeService
    from opencortex.services.retrieval_service import RetrievalService
    from opencortex.services.session_lifecycle_service import (
        SessionLifecycleService,
    )
    from opencortex.services.system_status_service import SystemStatusService

T = TypeVar("T")


class CortexMemoryServices:
    """Own lazy construction of memory-facade-scoped service collaborators."""

    def __init__(self, memory: CortexMemory) -> None:
        self._orch = memory

    def _cached(self, attr_name: str, factory: Callable[[], T]) -> T:
        """Return a memory-facade-owned cached service instance."""
        cached = getattr(self._orch, attr_name, None)
        if cached is None:
            cached = factory()
            setattr(self._orch, attr_name, cached)
        return cached

    @property
    def memory_service(self) -> "MemoryService":
        """Lazy-built MemoryService for delegated CRUD/query/scoring methods."""
        from opencortex.services.memory_service import MemoryService
        from opencortex.services.memory_write_service import MemoryWriteDependencies

        return self._cached(
            "_memory_service_instance",
            lambda: self._build_memory_service(
                MemoryService,
                MemoryWriteDependencies,
            ),
        )

    def _build_memory_service(
        self,
        memory_service_type: type["MemoryService"],
        dependencies_type: type,
    ) -> "MemoryService":
        """Construct MemoryService and bind explicit write-path dependencies."""
        service = memory_service_type(self._orch)
        if not hasattr(self._orch, "_config"):
            return service
        service.configure_write_dependencies(
            dependencies_type(
                config=self._orch._config,
                storage=self._orch._storage,
                fs=self._orch._fs,
                embedder=self._orch._embedder,
                memory_signal_bus=getattr(self._orch, "_memory_signal_bus", None),
                entity_index=getattr(self._orch, "_entity_index", None),
                memory_record_service=self._orch._memory_record_service,
                derivation_service=self._orch._derivation_service,
                session_lifecycle_service=self._orch._session_lifecycle_service,
                ensure_init=self._orch._ensure_init,
                get_collection=self._orch._get_collection,
                feedback=service.feedback,
            )
        )
        return service

    @property
    def derivation_service(self) -> "DerivationService":
        """Lazy-built DerivationService for derive-domain methods."""
        from opencortex.services.derivation_service import DerivationService

        return self._cached(
            "_derivation_service_instance",
            lambda: DerivationService(self._orch),
        )

    @property
    def retrieval_service(self) -> "RetrievalService":
        """Lazy-built RetrievalService for search/retrieve-domain methods."""
        from opencortex.services.retrieval_service import RetrievalService

        return self._cached(
            "_retrieval_service_instance",
            lambda: RetrievalService(self._orch),
        )

    @property
    def session_lifecycle_service(self) -> "SessionLifecycleService":
        """Lazy-built SessionLifecycleService for session/trace lifecycle methods."""
        from opencortex.services.session_lifecycle_service import (
            SessionLifecycleService,
        )

        return self._cached(
            "_session_lifecycle_service_instance",
            lambda: SessionLifecycleService(self._orch),
        )

    @property
    def memory_record_service(self) -> "MemoryRecordService":
        """Lazy-built MemoryRecordService for record/projection/URI helpers."""
        from opencortex.services.memory_record_service import MemoryRecordService

        return self._cached(
            "_memory_record_service_instance",
            lambda: MemoryRecordService(self._orch),
        )

    @property
    def model_runtime_service(self) -> "ModelRuntimeService":
        """Lazy-built ModelRuntimeService for embedder/rerank runtime helpers."""
        from opencortex.services.model_runtime_service import ModelRuntimeService

        return self._cached(
            "_model_runtime_service_instance",
            lambda: ModelRuntimeService(self._orch),
        )

    @property
    def memory_sharing_service(self) -> "MemorySharingService":
        """Lazy-built MemorySharingService for sharing/admin mutations."""
        from opencortex.services.memory_sharing_service import MemorySharingService

        return self._cached(
            "_memory_sharing_service_instance",
            lambda: MemorySharingService(self._orch),
        )

    @property
    def memory_admin_stats_service(self) -> "MemoryAdminStatsService":
        """Lazy-built MemoryAdminStatsService for admin memory statistics."""
        from opencortex.services.memory_admin_stats_service import (
            MemoryAdminStatsService,
        )

        return self._cached(
            "_memory_admin_stats_service_instance",
            lambda: MemoryAdminStatsService(self._orch),
        )

    @property
    def knowledge_service(self) -> "KnowledgeService":
        """Lazy-built KnowledgeService for delegated knowledge methods."""
        from opencortex.services.knowledge_service import KnowledgeService

        return self._cached(
            "_knowledge_service_instance",
            lambda: KnowledgeService(self._orch),
        )

    @property
    def system_status_service(self) -> "SystemStatusService":
        """Lazy-built SystemStatusService for delegated status methods."""
        from opencortex.services.system_status_service import SystemStatusService

        return self._cached(
            "_system_status_service_instance",
            lambda: SystemStatusService(self._orch),
        )

    @property
    def background_task_manager(self) -> "BackgroundTaskManager":
        """Lazy-built BackgroundTaskManager for delegated lifecycle methods."""
        from opencortex.lifecycle.background_tasks import BackgroundTaskManager

        return self._cached(
            "_background_task_manager_instance",
            lambda: BackgroundTaskManager(self._orch),
        )

    @property
    def bootstrapper(self) -> "SubsystemBootstrapper":
        """Lazy-built SubsystemBootstrapper for subsystem creation and wiring."""
        from opencortex.lifecycle.bootstrapper import SubsystemBootstrapper

        return self._cached(
            "_bootstrapper_instance",
            lambda: SubsystemBootstrapper(self._orch),
        )


MemoryOrchestratorServices = CortexMemoryServices
