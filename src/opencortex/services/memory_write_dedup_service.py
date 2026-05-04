# SPDX-License-Identifier: Apache-2.0
"""Write-time semantic deduplication and merge behavior."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from opencortex.core.context import Context
from opencortex.http.request_context import get_effective_project_id
from opencortex.services.derivation_service import (
    _merge_unique_strings,
    _split_keyword_string,
)
from opencortex.services.memory_filters import (
    FilterExpr,
    and_filter,
    memory_visibility_filter,
)
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


class MemoryWriteDedupService:
    """Owns duplicate search, merge, and merge-signal behavior."""

    def __init__(self, write_service: "MemoryWriteService") -> None:
        """Bind the dedup service to a write service facade."""
        self._write_service = write_service

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
        existing_record = await self._write_service._get_record_by_uri(existing_uri)
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
        try:
            dedup_filter = self._build_duplicate_filter(
                memory_kind=memory_kind,
                merge_signature=merge_signature,
                tid=tid,
                uid=uid,
            )
            results = await self._write_service._storage.search(
                self._write_service._get_collection(),
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
        records = await self._write_service._storage.filter(
            self._write_service._get_collection(),
            FilterExpr.eq("uri", existing_uri).to_dict(),
            limit=1,
            output_fields=["abstract", "overview"],
        )
        existing_content = ""
        if records:
            try:
                existing_content = await self._write_service._fs.read_file(existing_uri)
            except Exception:
                existing_content = ""

        merged_content = (
            f"{existing_content}\n---\n{new_content}".strip()
            if new_content
            else existing_content
        )
        if records:
            await self._update_merged_record(
                existing_uri=existing_uri,
                record=records[0],
                abstract=new_abstract,
                content=merged_content,
            )
        await self._write_service.feedback(existing_uri, 0.5)

    async def _update_merged_record(
        self,
        *,
        existing_uri: str,
        record: Dict[str, Any],
        abstract: str,
        content: str,
    ) -> None:
        """Apply the minimal record update needed by dedup merge."""
        record_id = record.get("id", "")
        if not record_id:
            return

        next_meta = self._coerce_meta(record.get("meta", {}))
        next_overview = str(record.get("overview", "") or "")
        next_entities = _merge_unique_strings(
            record.get("entities") or [],
            next_meta.get("entities"),
        )
        next_keywords_list = _merge_unique_strings(
            next_meta.get("topics"),
            _split_keyword_string(record.get("keywords", "")),
        )
        derived_fact_points: Optional[List[str]] = None
        if content:
            derive_result = await self._write_service._derive_layers(
                user_abstract=abstract,
                content=content,
                user_overview="",
            )
            next_entities = _merge_unique_strings(
                derive_result.get("entities", []),
                next_entities,
            )
            next_keywords_list = _merge_unique_strings(
                next_keywords_list,
                _split_keyword_string(derive_result.get("keywords", "")),
            )
            next_anchor_handles = _merge_unique_strings(
                next_meta.get("anchor_handles"),
                derive_result.get("anchor_handles", []),
            )
            if next_anchor_handles:
                next_meta["anchor_handles"] = next_anchor_handles
            raw_fps = derive_result.get("fact_points", [])
            derived_fact_points = (
                [str(fp) for fp in raw_fps] if isinstance(raw_fps, list) else []
            )

        update_data: Dict[str, Any] = {"abstract": abstract}
        if next_keywords_list:
            next_meta["topics"] = _merge_unique_strings(
                next_meta.get("topics"),
                next_keywords_list,
            )
            update_data["keywords"] = ", ".join(next_keywords_list)
        if next_entities:
            update_data["entities"] = next_entities
        if next_meta:
            update_data["meta"] = next_meta

        embedder = self._write_service._embedder
        if embedder:
            loop = asyncio.get_running_loop()
            embed_input = abstract
            if next_keywords_list:
                embed_input = f"{embed_input} {', '.join(next_keywords_list)}".strip()
            result = await loop.run_in_executor(None, embedder.embed, embed_input)
            update_data["vector"] = result.dense_vector
            if result.sparse_vector:
                update_data["sparse_vector"] = result.sparse_vector

        abstract_json = self._write_service._build_abstract_json(
            uri=existing_uri,
            context_type=str(record.get("context_type", "") or ""),
            category=str(record.get("category", "") or ""),
            abstract=abstract,
            overview=next_overview,
            content=content,
            entities=next_entities,
            meta=next_meta,
            keywords=next_keywords_list,
            parent_uri=str(record.get("parent_uri", "") or ""),
            session_id=str(record.get("session_id", "") or ""),
        )
        if derived_fact_points is not None:
            abstract_json["fact_points"] = derived_fact_points
        else:
            prior_abstract_json = record.get("abstract_json")
            if isinstance(prior_abstract_json, dict):
                prior_fps = prior_abstract_json.get("fact_points") or []
                if isinstance(prior_fps, list):
                    abstract_json["fact_points"] = [str(fp) for fp in prior_fps]
        update_data.update(
            self._write_service._memory_object_payload(
                abstract_json,
                is_leaf=bool(record.get("is_leaf", False)),
            )
        )
        update_data["abstract_json"] = abstract_json

        await self._write_service._storage.update(
            self._write_service._get_collection(),
            record_id,
            update_data,
        )
        updated_record = dict(record)
        updated_record.update(update_data)
        await self._write_service._sync_anchor_projection_records(
            source_record=updated_record,
            abstract_json=abstract_json,
        )
        await self._write_service._fs.write_context(
            uri=existing_uri,
            content=content,
            abstract=abstract,
            overview=next_overview,
            abstract_json=abstract_json,
        )

    @staticmethod
    def _coerce_meta(raw_meta: Any) -> Dict[str, Any]:
        """Return metadata as a dict, tolerating legacy encoded payloads."""
        if isinstance(raw_meta, str):
            try:
                decoded = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                return {}
            return decoded if isinstance(decoded, dict) else {}
        if isinstance(raw_meta, dict):
            return dict(raw_meta)
        return {}

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
            FilterExpr.eq("is_leaf", True),
            memory_visibility_filter(
                tenant_id=tid,
                user_id=uid,
                project_id=get_effective_project_id(),
                exclude_staging=True,
                exclude_superseded=True,
            ),
        ]
        if memory_kind:
            clauses.append(FilterExpr.eq("memory_kind", memory_kind))
        if merge_signature:
            clauses.append(FilterExpr.eq("merge_signature", merge_signature))
        return and_filter(*clauses)

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
        signal_bus = self._write_service._memory_signal_bus
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
