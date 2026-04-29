# SPDX-License-Identifier: Apache-2.0
"""Memory record facade plus search/listing service.

``MemoryService`` preserves the public method surface that
``MemoryOrchestrator`` delegates to, while write/mutation and scoring
domain logic live in narrower services.

Boundary
--------
``MemoryService`` is responsible for:
- Compatibility wrappers for memory write/mutation methods.
- Compatibility wrappers for memory query/list/index methods.
- Compatibility wrappers for scoring + lifecycle adjuncts.

It is explicitly NOT responsible for:
- Knowledge management (``knowledge_*``, archivist) — Phase 2
- System status reporting — Phase 4 (``SystemStatusService``)
- Subsystem boot sequencing — Phase 5
- Periodic background tasks (autophagy / connection sweepers / derive
  worker) — Phase 6
- Conversation lifecycle (``session_*``, benchmark ingest) — already
  delegated to ``ContextManager``
- Storage adapters, embedders, recall planning, intent routing — owned
  by their respective modules

Design
------
The service holds a back-reference to the orchestrator
(``self._orch``) and reaches into orchestrator-owned subsystems
(``_storage``, ``_embedder``, ``_fs``, ``_recall_planner``, etc.) at
call time. This mirrors the precedent set by
``BenchmarkConversationIngestService``.

Construction is sync and cheap — no I/O, no model loading. The
orchestrator service registry lazily builds one ``MemoryService`` instance
so delegate methods can call ``self._memory_service.X`` without
``if None`` guards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from opencortex.core.context import Context
from opencortex.intent import RetrievalDepth, RetrievalPlan, SearchResult
from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
    FindResult,
    QueryResult,
    TypedQuery,
)

if TYPE_CHECKING:
    from opencortex.orchestrator import MemoryOrchestrator
    from opencortex.services.memory_query_service import MemoryQueryService
    from opencortex.services.memory_scoring_service import MemoryScoringService
    from opencortex.services.memory_write_service import MemoryWriteService

_BATCH_ADD_CONCURRENCY = 8
_BATCH_ADD_TASK_CHUNK_SIZE = _BATCH_ADD_CONCURRENCY * 4


class MemoryService:
    """Compatibility facade plus memory search/listing surface.

    The service is lazily constructed by the orchestrator service registry and
    delegates to narrower services or orchestrator-owned subsystems via
    ``self._orch``.
    """

    def __init__(self, orchestrator: "MemoryOrchestrator") -> None:
        """Bind the service to its parent orchestrator.

        Args:
            orchestrator: The ``MemoryOrchestrator`` instance whose
                subsystems (``_storage``, ``_embedder``, ``_fs``,
                ``_recall_planner``, etc.) this service reaches into
                at call time. Stored as ``self._orch``; not validated.
        """
        self._orch = orchestrator

    @property
    def _memory_write_service(self) -> "MemoryWriteService":
        """Lazy-built MemoryWriteService for write/mutation methods."""
        from opencortex.services.memory_write_service import MemoryWriteService

        cached = getattr(self, "_memory_write_service_instance", None)
        if cached is None:
            cached = MemoryWriteService(self)
            self._memory_write_service_instance = cached
        return cached

    # =========================================================================
    # Write / mutation facade
    # =========================================================================

    async def update(
        self,
        uri: str,
        abstract: Optional[str] = None,
        content: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        overview: Optional[str] = None,
    ) -> bool:
        """Delegate to MemoryWriteService.update."""
        return await self._memory_write_service.update(
            uri=uri,
            abstract=abstract,
            content=content,
            meta=meta,
            overview=overview,
        )

    async def remove(self, uri: str, recursive: bool = True) -> int:
        """Delegate to MemoryWriteService.remove."""
        return await self._memory_write_service.remove(uri, recursive=recursive)

    async def add(
        self,
        abstract: str,
        content: str = "",
        overview: str = "",
        category: str = "",
        parent_uri: Optional[str] = None,
        uri: Optional[str] = None,
        context_type: Optional[str] = None,
        is_leaf: bool = True,
        meta: Optional[Dict[str, Any]] = None,
        related_uri: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        dedup: bool = False,
        dedup_threshold: float = 0.82,
        embed_text: str = "",
        defer_derive: bool = False,
    ) -> Context:
        """Delegate to MemoryWriteService.add."""
        return await self._memory_write_service.add(
            abstract=abstract,
            content=content,
            overview=overview,
            category=category,
            parent_uri=parent_uri,
            uri=uri,
            context_type=context_type,
            is_leaf=is_leaf,
            meta=meta,
            related_uri=related_uri,
            session_id=session_id,
            dedup=dedup,
            dedup_threshold=dedup_threshold,
            embed_text=embed_text,
            defer_derive=defer_derive,
        )

    async def _check_duplicate(
        self,
        vector: List[float],
        memory_kind: str,
        merge_signature: str,
        threshold: float,
        tid: str,
        uid: str,
    ) -> Optional[Tuple[str, float]]:
        """Delegate to MemoryWriteService._check_duplicate."""
        return await self._memory_write_service._check_duplicate(
            vector=vector,
            memory_kind=memory_kind,
            merge_signature=merge_signature,
            threshold=threshold,
            tid=tid,
            uid=uid,
        )

    async def _merge_into(
        self, existing_uri: str, new_abstract: str, new_content: str
    ) -> None:
        """Delegate to MemoryWriteService._merge_into."""
        await self._memory_write_service._merge_into(
            existing_uri=existing_uri,
            new_abstract=new_abstract,
            new_content=new_content,
        )

    async def _ensure_parent_records(self, parent_uri: str) -> None:
        """Delegate to MemoryWriteService._ensure_parent_records."""
        await self._memory_write_service._ensure_parent_records(parent_uri)

    async def _generate_abstract_overview(
        self,
        content: str,
        file_path: str,
    ) -> tuple[str, str]:
        """Delegate to MemoryWriteService._generate_abstract_overview."""
        return await self._memory_write_service._generate_abstract_overview(
            content,
            file_path,
        )

    async def _add_document(
        self,
        content: str,
        abstract: str,
        overview: str,
        category: str,
        parent_uri: Optional[str],
        context_type: str,
        meta: Optional[Dict[str, Any]],
        session_id: Optional[str],
        source_path: str,
    ) -> Context:
        """Delegate to MemoryWriteService._add_document."""
        return await self._memory_write_service._add_document(
            content=content,
            abstract=abstract,
            overview=overview,
            category=category,
            parent_uri=parent_uri,
            context_type=context_type,
            meta=meta,
            session_id=session_id,
            source_path=source_path,
        )

    async def batch_add(
        self,
        items: List[Dict[str, Any]],
        source_path: str = "",
        scan_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Delegate to MemoryWriteService.batch_add."""
        return await self._memory_write_service.batch_add(
            items=items,
            source_path=source_path,
            scan_meta=scan_meta,
        )

    # =========================================================================
    # Scoring + lifecycle facade
    # =========================================================================

    @property
    def _memory_scoring_service(self) -> "MemoryScoringService":
        """Lazy-built service for feedback, decay, and protection methods."""
        from opencortex.services.memory_scoring_service import MemoryScoringService

        cached = getattr(self, "_memory_scoring_service_instance", None)
        if cached is None:
            cached = MemoryScoringService(self)
            self._memory_scoring_service_instance = cached
        return cached

    async def feedback(self, uri: str, reward: float) -> None:
        """Delegate to MemoryScoringService.feedback."""
        await self._memory_scoring_service.feedback(uri, reward)

    async def feedback_batch(self, rewards: List[Dict[str, Any]]) -> None:
        """Delegate to MemoryScoringService.feedback_batch."""
        await self._memory_scoring_service.feedback_batch(rewards)

    async def decay(self) -> Optional[Dict[str, Any]]:
        """Delegate to MemoryScoringService.decay."""
        return await self._memory_scoring_service.decay()

    async def cleanup_expired_staging(self) -> int:
        """Delegate to MemoryScoringService.cleanup_expired_staging."""
        return await self._memory_scoring_service.cleanup_expired_staging()

    async def protect(self, uri: str, protected: bool = True) -> None:
        """Delegate to MemoryScoringService.protect."""
        await self._memory_scoring_service.protect(uri, protected=protected)

    async def get_profile(self, uri: str) -> Optional[Dict[str, Any]]:
        """Delegate to MemoryScoringService.get_profile."""
        return await self._memory_scoring_service.get_profile(uri)

    # =========================================================================
    # Query / listing facade
    # =========================================================================

    @property
    def _memory_query_service(self) -> "MemoryQueryService":
        """Lazy-built service for search, listing, and index methods."""
        from opencortex.services.memory_query_service import MemoryQueryService

        cached = getattr(self, "_memory_query_service_instance", None)
        if cached is None:
            cached = MemoryQueryService(self)
            self._memory_query_service_instance = cached
        return cached

    async def search(
        self,
        query: str,
        context_type: Optional[ContextType] = None,
        target_uri: str = "",
        limit: int = 5,
        score_threshold: Optional[float] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
        detail_level: str = "l1",
        probe_result: Optional[SearchResult] = None,
        retrieve_plan: Optional[RetrievalPlan] = None,
        meta: Optional[Dict[str, Any]] = None,
        session_context: Optional[Dict[str, Any]] = None,
    ) -> FindResult:
        """Delegate to MemoryQueryService.search."""
        return await self._memory_query_service.search(
            query=query,
            context_type=context_type,
            target_uri=target_uri,
            limit=limit,
            score_threshold=score_threshold,
            metadata_filter=metadata_filter,
            detail_level=detail_level,
            probe_result=probe_result,
            retrieve_plan=retrieve_plan,
            meta=meta,
            session_context=session_context,
        )

    def _build_typed_queries(
        self,
        *,
        query: str,
        context_type: Optional[ContextType],
        target_uri: str,
        retrieve_plan: RetrievalPlan,
        runtime_bound_plan: Dict[str, Any],
    ) -> List[TypedQuery]:
        """Delegate to MemoryQueryService._build_typed_queries."""
        return self._memory_query_service._build_typed_queries(
            query=query,
            context_type=context_type,
            target_uri=target_uri,
            retrieve_plan=retrieve_plan,
            runtime_bound_plan=runtime_bound_plan,
        )

    @staticmethod
    def _context_type_from_value(raw_value: str) -> ContextType:
        """Delegate to MemoryQueryService._context_type_from_value."""
        from opencortex.services.memory_query_service import MemoryQueryService

        return MemoryQueryService._context_type_from_value(raw_value)

    @staticmethod
    def _detail_level_from_retrieval_depth(
        retrieval_depth: RetrievalDepth,
    ) -> DetailLevel:
        """Delegate to MemoryQueryService._detail_level_from_retrieval_depth."""
        from opencortex.services.memory_query_service import MemoryQueryService

        return MemoryQueryService._detail_level_from_retrieval_depth(retrieval_depth)

    @staticmethod
    def _summarize_retrieve_breakdown(
        query_results: List[QueryResult],
    ) -> Dict[str, float]:
        """Delegate to MemoryQueryService._summarize_retrieve_breakdown."""
        from opencortex.services.memory_query_service import MemoryQueryService

        return MemoryQueryService._summarize_retrieve_breakdown(query_results)

    @staticmethod
    def _infer_context_type(uri: str) -> ContextType:
        """Delegate to MemoryQueryService._infer_context_type."""
        from opencortex.services.memory_query_service import MemoryQueryService

        return MemoryQueryService._infer_context_type(uri)

    async def list_memories(
        self,
        category: Optional[str] = None,
        context_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        include_payload: bool = False,
    ) -> List[Dict[str, Any]]:
        """Delegate to MemoryQueryService.list_memories."""
        return await self._memory_query_service.list_memories(
            category=category,
            context_type=context_type,
            limit=limit,
            offset=offset,
            include_payload=include_payload,
        )

    async def memory_index(
        self,
        context_type: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Delegate to MemoryQueryService.memory_index."""
        return await self._memory_query_service.memory_index(
            context_type=context_type,
            limit=limit,
        )

    async def list_memories_admin(
        self,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        category: Optional[str] = None,
        context_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Delegate to MemoryQueryService.list_memories_admin."""
        return await self._memory_query_service.list_memories_admin(
            tenant_id=tenant_id,
            user_id=user_id,
            category=category,
            context_type=context_type,
            limit=limit,
            offset=offset,
        )
