# SPDX-License-Identifier: Apache-2.0
"""Candidate scoring and projection helpers for retrieval."""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opencortex.intent import RetrievalPlan
from opencortex.intent.retrieval_support import (
    anchor_rerank_bonus,
    record_anchor_groups,
)
from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
    MatchedContext,
    TypedQuery,
)

if TYPE_CHECKING:
    from opencortex.services.retrieval_service import RetrievalService


class RetrievalCandidateService:
    """Own candidate scoring, ACL, anchor matching, and context projection."""

    def __init__(self, retrieval_service: "RetrievalService") -> None:
        self._service = retrieval_service

    def _score_object_record(
        self,
        *,
        record: Dict[str, Any],
        typed_query: TypedQuery,
        retrieve_plan: Optional[RetrievalPlan],
        query_anchor_groups: Dict[str, set[str]],
        probe_candidate_ranks: Dict[str, int],
        cone_weight: float,
        uri_path_costs: Optional[Dict[str, float]] = None,
    ) -> tuple[float, str]:
        """Fuse URI path score (primary) with object-aware boosts."""
        leaf_uri = str(record.get("uri", "") or "")
        if uri_path_costs is not None and leaf_uri in uri_path_costs:
            score = 1.0 - uri_path_costs[leaf_uri]
        else:
            score = float(record.get("_score", record.get("score", 0.0)) or 0.0)
        reasons: List[str] = []
        target_kinds = (
            [kind.value for kind in retrieve_plan.target_memory_kinds]
            if retrieve_plan is not None
            else []
        )
        record_kind = str(record.get("memory_kind", ""))
        if record_kind in target_kinds:
            kind_rank = target_kinds.index(record_kind)
            score += 0.14 * (len(target_kinds) - kind_rank) / max(len(target_kinds), 1)
            reasons.append("kind")

        anchor_bonus, anchor_reasons = anchor_rerank_bonus(
            query_anchor_groups=query_anchor_groups,
            record_anchor_groups=record_anchor_groups(record),
        )
        if anchor_bonus > 0:
            score += anchor_bonus
            reasons.extend(anchor_reasons)

        probe_rank = probe_candidate_ranks.get(str(record.get("uri", "") or ""))
        if probe_rank is not None:
            score += max(0.04, 0.14 - min(probe_rank, 5) * 0.02)
            reasons.append("probe")

        if typed_query.target_directories and any(
            str(record.get("uri", "")).startswith(prefix)
            for prefix in typed_query.target_directories
        ):
            score += 0.06
            reasons.append("scope")

        if typed_query.target_doc_id and (
            str(record.get("source_doc_id", "")) == typed_query.target_doc_id
        ):
            score += 0.08
            reasons.append("doc")

        reward = float(record.get("reward_score", 0.0) or 0.0)
        if reward:
            score += max(min(0.06, reward * 0.03), -0.03)
            reasons.append("reward")

        active_count = int(record.get("active_count", 0) or 0)
        if active_count > 0:
            score += min(0.05, math.log1p(active_count) * 0.01)
            reasons.append("hot")

        cone_bonus = float(record.get("_cone_bonus", 0.0) or 0.0)
        if cone_weight > 0.0 and cone_bonus > 0.0:
            score += min(0.30, cone_weight * min(1.0, cone_bonus))
            reasons.append("cone")

        return score, ",".join(reasons) or "semantic"

    @staticmethod
    def _record_passes_acl(
        record: Dict[str, Any],
        tenant_id: str,
        user_id: str,
        project_id: str,
    ) -> bool:
        """Return True if record passes tenant/scope/project access control."""
        r_tenant = str(record.get("source_tenant_id", "") or "")
        if tenant_id and r_tenant and r_tenant != tenant_id:
            return False
        if record.get("scope") == "private" and record.get("source_user_id") != user_id:
            return False
        r_project = str(record.get("project_id", "") or "")
        return not (
            project_id
            and project_id != "public"
            and r_project not in (project_id, "public", "")
        )

    @staticmethod
    def _matched_record_anchors(
        *,
        record: Dict[str, Any],
        query_anchor_groups: Dict[str, set[str]],
    ) -> List[str]:
        """Return normalized query anchors that concretely matched this record."""
        if not query_anchor_groups:
            return []
        matched: List[str] = []
        record_groups = record_anchor_groups(record)
        for kind, query_values in query_anchor_groups.items():
            record_values = record_groups.get(kind, set())
            for value in sorted(query_values.intersection(record_values)):
                if value not in matched:
                    matched.append(value)
        return matched[:8]

    async def _records_to_matched_contexts(
        self,
        *,
        candidates: List[Dict[str, Any]],
        context_type: ContextType,
        detail_level: DetailLevel,
    ) -> List[MatchedContext]:
        """Convert raw store records into MatchedContext objects."""

        async def _build_one(record: Dict[str, Any]) -> MatchedContext:
            uri = str(record.get("uri", ""))
            overview = None
            if detail_level in (DetailLevel.L1, DetailLevel.L2):
                overview = str(record.get("overview", "") or "") or None

            content = None
            if detail_level == DetailLevel.L2:
                content = str(record.get("content", "") or "") or None
                if content is None and self._service._fs:
                    try:
                        content = await self._service._fs.read_file(f"{uri}/content.md")
                    except Exception:
                        content = None

            effective_type = context_type
            if context_type == ContextType.ANY:
                try:
                    effective_type = ContextType(
                        str(record.get("context_type", "memory"))
                    )
                except ValueError:
                    effective_type = ContextType.MEMORY

            return MatchedContext(
                uri=uri,
                context_type=effective_type,
                is_leaf=bool(record.get("is_leaf", False)),
                abstract=str(record.get("abstract", "") or ""),
                overview=overview,
                content=content,
                keywords=str(record.get("keywords", "") or ""),
                category=str(record.get("category", "") or ""),
                score=float(
                    record.get("_final_score", record.get("_score", 0.0)) or 0.0
                ),
                match_reason=str(record.get("_match_reason", "") or ""),
                session_id=str(record.get("session_id", "") or ""),
                source_doc_id=record.get("source_doc_id"),
                source_doc_title=record.get("source_doc_title"),
                source_section_path=record.get("source_section_path"),
                source_uri=(
                    dict(record.get("meta") or {}).get("source_uri")
                    if isinstance(record.get("meta"), dict)
                    else None
                ),
                msg_range=(
                    dict(record.get("meta") or {}).get("msg_range")
                    if isinstance(record.get("meta"), dict)
                    else None
                ),
                recomposition_stage=(
                    dict(record.get("meta") or {}).get("recomposition_stage")
                    if isinstance(record.get("meta"), dict)
                    else None
                ),
                layer=(
                    dict(record.get("meta") or {}).get("layer")
                    if isinstance(record.get("meta"), dict)
                    else None
                ),
                matched_anchors=list(record.get("_matched_anchors", []) or []),
                cone_used=bool(record.get("_cone_used", False)),
                path_source=record.get("_path_source") or None,
                path_cost=(
                    float(record["_path_cost"])
                    if record.get("_path_cost") is not None
                    else None
                ),
                path_breakdown=record.get("_path_breakdown") or None,
                relations=[],
            )

        matches = await asyncio.gather(*[_build_one(record) for record in candidates])
        return list(matches)
