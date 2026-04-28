# SPDX-License-Identifier: Apache-2.0
"""Write-time semantic deduplication and merge behavior."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from opencortex.core.context import Context
from opencortex.http.request_context import get_effective_project_id
from opencortex.services.memory_signals import MemoryStoredSignal

if TYPE_CHECKING:
    from opencortex.services.memory_write_service import MemoryWriteService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DedupMergeResult:
    """Result of a write-time deduplication attempt."""

    merged: bool
    ctx: Optional[Context] = None
    target_uri: str = ""
    score: float = 0.0
    existing_record: Dict[str, Any] = field(default_factory=dict)
    dedup_ms: int = 0
    total_ms_at_match: int = 0


@dataclass(frozen=True)
class FilterExpr:
    """Small typed builder for the storage filter DSL."""

    op: str
    field: str = ""
    values: Tuple[Any, ...] = ()
    children: Tuple["FilterExpr", ...] = ()

    @classmethod
    def eq(cls, field: str, *values: Any) -> "FilterExpr":
        """Build an equality/membership filter."""
        return cls(op="must", field=field, values=tuple(values))

    @classmethod
    def all(cls, *children: "FilterExpr") -> "FilterExpr":
        """Build an AND expression."""
        return cls(op="and", children=tuple(child for child in children if child))

    @classmethod
    def any(cls, *children: "FilterExpr") -> "FilterExpr":
        """Build an OR expression."""
        return cls(op="or", children=tuple(child for child in children if child))

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the storage filter DSL."""
        if self.op == "must":
            return {"op": "must", "field": self.field, "conds": list(self.values)}
        return {"op": self.op, "conds": [child.to_dict() for child in self.children]}


class MemoryWriteDedupService:
    """Owns duplicate search, merge, and merge-signal behavior."""

    def __init__(self, write_service: "MemoryWriteService") -> None:
        """Bind the dedup service to a write service facade."""
        self._write_service = write_service

    @property
    def _orch(self) -> Any:
        return self._write_service._orch

    @property
    def _service(self) -> Any:
        return self._write_service._service

    async def try_merge_duplicate(
        self,
        *,
        ctx: Context,
        vector: List[float],
        memory_kind: str,
        merge_signature: str,
        threshold: float,
        tenant_id: str,
        user_id: str,
        abstract: str,
        content: str,
        add_started: float,
    ) -> DedupMergeResult:
        """Try to merge into an existing duplicate and return the outcome."""
        dedup_started = asyncio.get_running_loop().time()
        duplicate = await self.check_duplicate(
            vector=vector,
            memory_kind=memory_kind,
            merge_signature=merge_signature,
            threshold=threshold,
            tid=tenant_id,
            uid=user_id,
        )
        dedup_ms = int((asyncio.get_running_loop().time() - dedup_started) * 1000)
        if duplicate is None:
            return DedupMergeResult(merged=False, dedup_ms=dedup_ms)

        existing_uri, existing_score = duplicate
        total_ms_at_match = int(
            (asyncio.get_running_loop().time() - add_started) * 1000
        )
        existing_record = await self._orch._get_record_by_uri(existing_uri)
        persisted_owner_id = ""
        persisted_project_id = get_effective_project_id()
        if existing_record:
            persisted_owner_id = str(existing_record.get("id", ""))
            persisted_project_id = str(
                existing_record.get("project_id", persisted_project_id)
            )

        await self.merge_into(existing_uri, abstract, content)
        self._publish_merge_signal(
            uri=existing_uri,
            record_id=persisted_owner_id,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=persisted_project_id,
            existing_record=existing_record or {},
        )

        ctx.uri = existing_uri
        ctx.meta["dedup_action"] = "merged"
        ctx.meta["dedup_score"] = round(existing_score, 4)
        return DedupMergeResult(
            merged=True,
            ctx=ctx,
            target_uri=existing_uri,
            score=existing_score,
            existing_record=dict(existing_record or {}),
            dedup_ms=dedup_ms,
            total_ms_at_match=total_ms_at_match,
        )

    async def check_duplicate(
        self,
        vector: List[float],
        memory_kind: str,
        merge_signature: str,
        threshold: float,
        tid: str,
        uid: str,
    ) -> Optional[Tuple[str, float]]:
        """Return duplicate ``(existing_uri, score)`` when one exists."""
        orch = self._orch
        try:
            dedup_filter = self._build_duplicate_filter(
                memory_kind=memory_kind,
                merge_signature=merge_signature,
                tid=tid,
                uid=uid,
            )
            results = await orch._storage.search(
                orch._get_collection(),
                query_vector=vector,
                filter=dedup_filter,
                limit=1,
                output_fields=["uri", "abstract"],
            )
            if results:
                score = results[0].get("_score", results[0].get("score", 0.0))
                if score >= threshold:
                    return (results[0]["uri"], score)
        except Exception as exc:
            logger.debug("[MemoryService] Dedup check failed: %s", exc)
        return None

    async def merge_into(
        self, existing_uri: str, new_abstract: str, new_content: str
    ) -> None:
        """Merge new content into an existing record and reinforce it."""
        orch = self._orch
        records = await orch._storage.filter(
            orch._get_collection(),
            FilterExpr.eq("uri", existing_uri).to_dict(),
            limit=1,
            output_fields=["abstract", "overview"],
        )
        existing_content = ""
        if records:
            try:
                existing_content = await orch._fs.read_file(existing_uri)
            except Exception:
                existing_content = ""

        merged_content = (
            f"{existing_content}\n---\n{new_content}".strip()
            if new_content
            else existing_content
        )
        await self._service.update(
            existing_uri,
            abstract=new_abstract,
            content=merged_content,
        )
        await self._service.feedback(existing_uri, 0.5)

    @staticmethod
    def _build_duplicate_filter(
        *,
        memory_kind: str,
        merge_signature: str,
        tid: str,
        uid: str,
    ) -> Dict[str, Any]:
        """Build the tenant/scope/project filter for duplicate search."""
        clauses = [
            FilterExpr.eq("source_tenant_id", tid),
            FilterExpr.eq("is_leaf", True),
        ]
        if memory_kind:
            clauses.append(FilterExpr.eq("memory_kind", memory_kind))
        if merge_signature:
            clauses.append(FilterExpr.eq("merge_signature", merge_signature))
        clauses.append(
            FilterExpr.any(
                FilterExpr.eq("scope", "shared"),
                FilterExpr.all(
                    FilterExpr.eq("scope", "private"),
                    FilterExpr.eq("source_user_id", uid),
                ),
            )
        )
        project_id = get_effective_project_id()
        if project_id:
            clauses.append(FilterExpr.eq("project_id", project_id))
        return FilterExpr.all(*clauses).to_dict()

    def _publish_merge_signal(
        self,
        *,
        uri: str,
        record_id: str,
        tenant_id: str,
        user_id: str,
        project_id: str,
        existing_record: Dict[str, Any],
    ) -> None:
        """Publish the dedup merge lifecycle signal when a bus exists."""
        signal_bus = getattr(self._orch, "_memory_signal_bus", None)
        if signal_bus is None:
            return
        signal_bus.publish_nowait(
            MemoryStoredSignal(
                uri=uri,
                record_id=record_id,
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=project_id,
                context_type=str(existing_record.get("context_type", "")),
                category=str(existing_record.get("category", "")),
                dedup_action="merged",
                record=dict(existing_record),
            )
        )
