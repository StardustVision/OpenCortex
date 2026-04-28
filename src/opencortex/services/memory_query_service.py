# SPDX-License-Identifier: Apache-2.0
"""Memory query/list/index service for OpenCortex."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opencortex.http.request_context import (
    get_effective_identity,
    get_effective_project_id,
)
from opencortex.intent import RetrievalDepth, RetrievalPlan, SearchResult
from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
    FindResult,
    QueryResult,
    TypedQuery,
)

if TYPE_CHECKING:
    from opencortex.services.memory_recall_pipeline_service import (
        MemoryRecallPipelineService,
    )
    from opencortex.services.memory_service import MemoryService


class MemoryQueryService:
    """Own memory search, listing, and index query behavior."""

    def __init__(self, memory_service: "MemoryService") -> None:
        self._service = memory_service

    @property
    def _orch(self) -> Any:
        return self._service._orch

    # =========================================================================
    # Queries (U3 of plan 011)
    # =========================================================================

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
        """Search for relevant contexts using probe-planner-runtime pipeline.

        Args:
            query: Natural language query string.
            context_type: Restrict to a specific type
                (memory/resource/skill).
            target_uri: Restrict search to a directory subtree.
            limit: Maximum results per type.
            score_threshold: Minimum relevance score.
            metadata_filter: Additional filter conditions.
            detail_level: Fallback detail level if planner does not
                override (``"l0"``, ``"l1"``, ``"l2"``).
            probe_result: Pre-computed probe result; computed when
                ``None``.
            retrieve_plan: Pre-computed retrieval plan; computed when
                ``None``.
            meta: Optional metadata dict (supports ``target_doc_id``).
            session_context: Optional session context for runtime scope.

        Returns:
            ``FindResult`` with ``memories``, ``resources``, and
            ``skills`` lists.
        """
        return await self._recall_pipeline_service.search(
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

    @property
    def _recall_pipeline_service(self) -> "MemoryRecallPipelineService":
        """Lazy-built service for recall search orchestration."""
        from opencortex.services.memory_recall_pipeline_service import (
            MemoryRecallPipelineService,
        )

        cached = getattr(self, "_recall_pipeline_service_instance", None)
        if cached is None:
            cached = MemoryRecallPipelineService(self)
            self._recall_pipeline_service_instance = cached
        return cached

    def _build_typed_queries(
        self,
        *,
        query: str,
        context_type: Optional[ContextType],
        target_uri: str,
        retrieve_plan: RetrievalPlan,
        runtime_bound_plan: Dict[str, Any],
    ) -> List[TypedQuery]:
        """Project planner posture into concrete ``TypedQuery`` list for retrieval."""
        if context_type:
            types_to_search = [context_type]
        elif target_uri:
            types_to_search = [MemoryQueryService._infer_context_type(target_uri)]
        else:
            raw_context_types = runtime_bound_plan.get("context_types") or ["memory"]
            if len(raw_context_types) > 1:
                types_to_search = [ContextType.ANY]
            else:
                types_to_search = [
                    MemoryQueryService._context_type_from_value(raw_value)
                    for raw_value in raw_context_types
                ]

        return [
            TypedQuery(
                query=query,
                context_type=ct,
                intent="memory",
                priority=1,
                target_directories=[target_uri] if target_uri else [],
                detail_level=MemoryQueryService._detail_level_from_retrieval_depth(
                    retrieve_plan.retrieval_depth
                ),
            )
            for ct in types_to_search
        ]

    @staticmethod
    def _context_type_from_value(raw_value: str) -> ContextType:
        """Convert a raw string to a ContextType, defaulting to ANY on mismatch."""
        try:
            return ContextType(raw_value)
        except ValueError:
            return ContextType.ANY

    @staticmethod
    def _detail_level_from_retrieval_depth(
        retrieval_depth: RetrievalDepth,
    ) -> DetailLevel:
        """Map a RetrievalDepth enum value to its corresponding DetailLevel."""
        return DetailLevel(retrieval_depth.value)

    @staticmethod
    def _summarize_retrieve_breakdown(
        query_results: List[QueryResult],
    ) -> Dict[str, float]:
        """Aggregate per-query retrieval timings into a request-level breakdown."""
        keys = ("embed", "search", "rerank", "assemble", "total")
        if not query_results:
            return {key: 0.0 for key in keys}

        summary: Dict[str, float] = {}
        for key in keys:
            values = [
                float((query_result.timing_ms or {}).get(key, 0.0))
                for query_result in query_results
            ]
            summary[key] = round(max(values, default=0.0), 4)
        return summary

    @staticmethod
    def _infer_context_type(uri: str) -> ContextType:
        """Infer ContextType from URI path segments."""
        if "/staging/" in uri:
            return ContextType.STAGING
        if "/memories/" in uri:
            return ContextType.MEMORY
        if "/shared/cases/" in uri:
            return ContextType.CASE
        if "/shared/patterns/" in uri:
            return ContextType.PATTERN
        if "/skills/" in uri:
            return ContextType.SKILL
        return ContextType.RESOURCE

    async def list_memories(
        self,
        category: Optional[str] = None,
        context_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        include_payload: bool = False,
    ) -> List[Dict[str, Any]]:
        """List user-accessible memories ordered by ``updated_at`` desc.

        Returns private (own) and shared memories, excluding staging
        records. Results are tenant-scoped and project-isolated.

        Args:
            category: Filter by category.
            context_type: Filter by context type.
            limit: Maximum records to return.
            offset: Pagination offset.
            include_payload: Include ``meta``, ``abstract_json``,
                ``overview``, and other enrichment fields.

        Returns:
            List of dicts with ``uri``, ``abstract``, ``category``,
            ``context_type``, ``scope``, ``project_id``, and timestamps.
        """
        orch = self._orch
        orch._ensure_init()
        tid, uid = get_effective_identity()

        # Same scope filter as search(): private own + shared
        scope_filter = {
            "op": "or",
            "conds": [
                {"op": "must", "field": "scope", "conds": ["shared", ""]},
                {
                    "op": "and",
                    "conds": [
                        {"op": "must", "field": "scope", "conds": ["private"]},
                        {"op": "must", "field": "source_user_id", "conds": [uid]},
                    ],
                },
            ],
        }

        conds: List[Dict[str, Any]] = [
            {"op": "must_not", "field": "context_type", "conds": ["staging"]},
            scope_filter,
        ]
        if tid:
            conds.append(
                {"op": "must", "field": "source_tenant_id", "conds": [tid, ""]}
            )
        if category:
            conds.append({"op": "must", "field": "category", "conds": [category]})
        if context_type:
            conds.append(
                {"op": "must", "field": "context_type", "conds": [context_type]}
            )

        # Project filter: strict isolation
        project_id = get_effective_project_id()
        if project_id and project_id != "public":
            conds.append(
                {
                    "op": "or",
                    "conds": [
                        {
                            "op": "must",
                            "field": "project_id",
                            "conds": [project_id, "public"],
                        },
                    ],
                }
            )

        combined: Dict[str, Any] = {"op": "and", "conds": conds}

        records = await orch._storage.filter(
            orch._get_collection(),
            combined,
            limit=limit,
            offset=offset,
            order_by="updated_at",
            order_desc=True,
        )

        items: List[Dict[str, Any]] = []
        for record in records:
            if not record.get("abstract"):
                continue
            item = {
                "uri": record.get("uri", ""),
                "abstract": record.get("abstract", ""),
                "category": record.get("category", ""),
                "context_type": record.get("context_type", ""),
                "scope": record.get("scope", ""),
                "project_id": record.get("project_id", ""),
                "updated_at": record.get("updated_at", ""),
                "created_at": record.get("created_at", ""),
            }
            if include_payload:
                meta = dict(record.get("meta") or {})
                item.update(
                    {
                        "meta": record.get("meta", {}),
                        "abstract_json": record.get("abstract_json", {}),
                        "session_id": record.get("session_id", ""),
                        "speaker": record.get("speaker", ""),
                        "event_date": record.get("event_date", ""),
                        "overview": record.get("overview", ""),
                        "msg_range": meta.get("msg_range"),
                        "recomposition_stage": meta.get("recomposition_stage"),
                        "source_uri": meta.get("source_uri"),
                    }
                )
            items.append(item)
        return items

    async def memory_index(
        self,
        context_type: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Return a lightweight index of all memories grouped by context type.

        Only leaf records with non-empty abstracts are included.

        Args:
            context_type: Comma-separated list of context types to
                include. All types when ``None``.
            limit: Maximum records to scan.

        Returns:
            Dict with ``"index"`` mapping context type to a list of
            ``{uri, abstract, context_type, category, created_at}`` and
            ``"total"`` count.
        """
        orch = self._orch
        orch._ensure_init()
        tid, uid = get_effective_identity()

        scope_filter = {
            "op": "or",
            "conds": [
                {"op": "must", "field": "scope", "conds": ["shared", ""]},
                {
                    "op": "and",
                    "conds": [
                        {"op": "must", "field": "scope", "conds": ["private"]},
                        {"op": "must", "field": "source_user_id", "conds": [uid]},
                    ],
                },
            ],
        }

        conds: List[Dict[str, Any]] = [
            {"op": "must_not", "field": "context_type", "conds": ["staging"]},
            {"op": "must", "field": "is_leaf", "conds": [True]},
            scope_filter,
        ]
        if tid:
            conds.append(
                {"op": "must", "field": "source_tenant_id", "conds": [tid, ""]}
            )

        if context_type:
            types = [t.strip() for t in context_type.split(",") if t.strip()]
            conds.append({"op": "must", "field": "context_type", "conds": types})

        project_id = get_effective_project_id()
        if project_id and project_id != "public":
            conds.append(
                {
                    "op": "or",
                    "conds": [
                        {
                            "op": "must",
                            "field": "project_id",
                            "conds": [project_id, "public"],
                        },
                    ],
                }
            )

        records = await orch._storage.filter(
            orch._get_collection(),
            {"op": "and", "conds": conds},
            limit=limit,
            offset=0,
            order_by="created_at",
            order_desc=True,
        )

        index: Dict[str, list] = {}
        for r in records:
            abstract = r.get("abstract", "")
            if not abstract:
                continue
            ct = r.get("context_type", "memory")
            if ct not in index:
                index[ct] = []
            index[ct].append(
                {
                    "uri": r.get("uri", ""),
                    "abstract": abstract[:150],
                    "context_type": ct,
                    "category": r.get("category", ""),
                    "created_at": r.get("created_at", ""),
                }
            )

        total = sum(len(v) for v in index.values())
        return {"index": index, "total": total}

    async def list_memories_admin(
        self,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        category: Optional[str] = None,
        context_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List memories across all users (admin-only, no scope isolation).

        Args:
            tenant_id: Filter by tenant.
            user_id: Filter by user.
            category: Filter by category.
            context_type: Filter by context type.
            limit: Maximum records to return.
            offset: Pagination offset.

        Returns:
            List of dicts with ``uri``, ``abstract``, ``category``,
            ``context_type``, ``scope``, ``project_id``, identity
            fields, and timestamps.
        """
        orch = self._orch
        orch._ensure_init()

        conds: List[Dict[str, Any]] = [
            {"op": "must_not", "field": "context_type", "conds": ["staging"]},
        ]
        if tenant_id:
            conds.append(
                {"op": "must", "field": "source_tenant_id", "conds": [tenant_id]}
            )
        if user_id:
            conds.append({"op": "must", "field": "source_user_id", "conds": [user_id]})
        if category:
            conds.append({"op": "must", "field": "category", "conds": [category]})
        if context_type:
            conds.append(
                {"op": "must", "field": "context_type", "conds": [context_type]}
            )

        combined: Dict[str, Any] = {"op": "and", "conds": conds}

        records = await orch._storage.filter(
            orch._get_collection(),
            combined,
            limit=limit,
            offset=offset,
            order_by="updated_at",
            order_desc=True,
        )

        return [
            {
                "uri": r.get("uri", ""),
                "abstract": r.get("abstract", ""),
                "category": r.get("category", ""),
                "context_type": r.get("context_type", ""),
                "scope": r.get("scope", ""),
                "project_id": r.get("project_id", ""),
                "source_tenant_id": r.get("source_tenant_id", ""),
                "source_user_id": r.get("source_user_id", ""),
                "updated_at": r.get("updated_at", ""),
                "created_at": r.get("created_at", ""),
            }
            for r in records
            if r.get("abstract")  # skip directory nodes (empty abstract)
        ]
