# SPDX-License-Identifier: Apache-2.0
"""Support helpers for probe/planner/runtime retrieval orchestration."""

import re
from typing import Any, Dict, List, Optional

from opencortex.intent.types import (
    ProbeScopeInput,
    ProbeScopeSource,
    QueryAnchorKind,
    RetrievalPlan,
    ScopeLevel,
    SearchResult,
)
from opencortex.retrieve.types import ContextType


def merge_filter_clauses(
    *filters: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Merge non-empty filter clauses into one AND filter."""
    clauses = [clause for clause in filters if clause]
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"op": "and", "conds": clauses}


def build_scope_filter(
    *,
    context_type: Optional[ContextType],
    target_uri: str,
    target_doc_id: Optional[str],
    session_context: Optional[Dict[str, Any]],
    metadata_filter: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Build shared scope clauses for probe and execution."""
    type_filter = None
    if context_type and context_type != ContextType.ANY:
        type_filter = {
            "op": "must",
            "field": "context_type",
            "conds": [context_type.value],
        }

    target_filter = None
    if target_uri:
        target_filter = {
            "op": "prefix",
            "field": "uri",
            "prefix": target_uri,
        }

    doc_filter = None
    if target_doc_id:
        doc_filter = {
            "op": "must",
            "field": "source_doc_id",
            "conds": [target_doc_id],
        }

    session_filter = None
    scoped_session_id = str((session_context or {}).get("session_id", "") or "").strip()
    if scoped_session_id:
        session_filter = {
            "op": "must",
            "field": "session_id",
            "conds": [scoped_session_id],
        }

    return merge_filter_clauses(
        metadata_filter,
        type_filter,
        target_filter,
        doc_filter,
        session_filter,
    )


def build_probe_scope_input(
    *,
    context_type: Optional[ContextType],
    target_uri: str,
    target_doc_id: Optional[str],
    session_context: Optional[Dict[str, Any]],
) -> ProbeScopeInput:
    """Build structured probe scope inputs without flattening precedence."""
    scoped_session_id = str((session_context or {}).get("session_id", "") or "").strip()
    normalized_target_uri = str(target_uri or "").strip()
    normalized_doc_id = str(target_doc_id or "").strip()
    normalized_context_type = (
        context_type.value if context_type and context_type != ContextType.ANY else None
    )

    if normalized_target_uri:
        return ProbeScopeInput(
            source=ProbeScopeSource.TARGET_URI,
            authoritative=True,
            target_uri=normalized_target_uri,
            context_type=normalized_context_type,
        )
    if scoped_session_id:
        return ProbeScopeInput(
            source=ProbeScopeSource.SESSION_ID,
            authoritative=True,
            session_id=scoped_session_id,
            context_type=normalized_context_type,
        )
    if normalized_doc_id:
        return ProbeScopeInput(
            source=ProbeScopeSource.SOURCE_DOC_ID,
            authoritative=True,
            source_doc_id=normalized_doc_id,
            context_type=normalized_context_type,
        )
    if normalized_context_type:
        return ProbeScopeInput(
            source=ProbeScopeSource.CONTEXT_TYPE,
            authoritative=False,
            context_type=normalized_context_type,
        )
    return ProbeScopeInput(
        source=ProbeScopeSource.GLOBAL_ROOT,
        authoritative=False,
    )


def _normalize_anchor_value(value: Any) -> str:
    """Normalize anchor text for cheap overlap scoring."""
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _normalize_anchor_kind(value: Any) -> str:
    """Normalize anchor kind strings into the active planner taxonomy."""
    normalized = str(value or "").strip().lower()
    if normalized in {"preference", "constraint", "relation"}:
        return QueryAnchorKind.TOPIC.value
    if normalized:
        return normalized
    return QueryAnchorKind.TOPIC.value


def query_anchor_groups(
    retrieve_plan: Optional[RetrievalPlan],
    probe_result: Optional[SearchResult],
) -> Dict[str, set[str]]:
    """Collect query anchors grouped by semantic kind."""
    groups: Dict[str, set[str]] = {}

    if retrieve_plan is not None:
        for anchor in retrieve_plan.query_plan.anchors:
            normalized = _normalize_anchor_value(anchor.value)
            if not normalized:
                continue
            kind = _normalize_anchor_kind(anchor.kind.value)
            groups.setdefault(kind, set()).add(normalized)

    if probe_result is not None:
        topic_group = groups.setdefault(QueryAnchorKind.TOPIC.value, set())
        for anchor in probe_result.anchor_hits:
            normalized = _normalize_anchor_value(anchor)
            if normalized:
                topic_group.add(normalized)
        time_group = groups.setdefault(QueryAnchorKind.TIME.value, set())
        entity_group = groups.setdefault(QueryAnchorKind.ENTITY.value, set())
        for value in probe_result.query_entities:
            normalized = _normalize_anchor_value(value)
            if normalized:
                entity_group.add(normalized)
        for starting_point in probe_result.starting_points:
            for value in starting_point.entities:
                normalized = _normalize_anchor_value(value)
                if normalized:
                    entity_group.add(normalized)
            for value in starting_point.time_refs:
                normalized = _normalize_anchor_value(value)
                if normalized:
                    time_group.add(normalized)
        for value in probe_result.starting_point_anchors:
            normalized = _normalize_anchor_value(value)
            if normalized:
                topic_group.add(normalized)

    return groups


def record_anchor_groups(record: Dict[str, Any]) -> Dict[str, set[str]]:
    """Project record-side anchors into grouped normalized sets."""
    groups: Dict[str, set[str]] = {}

    abstract_json = record.get("abstract_json")
    if isinstance(abstract_json, dict):
        for anchor in abstract_json.get("anchors") or []:
            if not isinstance(anchor, dict):
                continue
            normalized = _normalize_anchor_value(
                anchor.get("text") or anchor.get("value")
            )
            if not normalized:
                continue
            kind = _normalize_anchor_kind(anchor.get("anchor_type"))
            groups.setdefault(kind, set()).add(normalized)

    topic_group = groups.setdefault(QueryAnchorKind.TOPIC.value, set())
    for value in record.get("anchor_hits") or []:
        normalized = _normalize_anchor_value(value)
        if normalized:
            topic_group.add(normalized)

    return groups


def probe_candidate_ranks(
    probe_result: Optional[SearchResult],
) -> Dict[str, int]:
    """Return a stable URI-to-rank map from Phase 1 probe results."""
    if probe_result is None:
        return {}
    return {
        candidate.uri: rank
        for rank, candidate in enumerate(probe_result.candidate_entries)
        if candidate.uri
    }


def build_start_point_filter(
    *,
    retrieve_plan: Optional[RetrievalPlan],
    probe_result: Optional[SearchResult],
    bound_plan: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Build a bounded start-point filter from Phase 1 evidence."""
    if probe_result is None or retrieve_plan is None:
        return None

    plan = bound_plan or {}
    bind_start_points = bool(plan.get("bind_start_points"))
    if not bind_start_points:
        return None
    if retrieve_plan.scope_level != ScopeLevel.GLOBAL:
        return None

    uri_cap = int(plan.get("seed_uri_cap", 0) or 0)
    if uri_cap <= 0:
        uri_cap = 6
    anchor_cap = int(plan.get("anchor_cap", 0) or 0)
    if anchor_cap <= 0:
        anchor_cap = 4

    uri_values = [sp.uri for sp in (probe_result.starting_points or []) if sp.uri] + [
        candidate.uri
        for candidate in probe_result.candidate_entries[:uri_cap]
        if candidate.uri
    ]
    anchor_values: List[str] = []
    for anchor in retrieve_plan.query_plan.anchors:
        normalized = str(anchor.value or "").strip()
        if normalized and normalized not in anchor_values:
            anchor_values.append(normalized)
        if len(anchor_values) >= anchor_cap:
            break
    if not anchor_values:
        for anchor in probe_result.anchor_hits:
            normalized = str(anchor or "").strip()
            if normalized and normalized not in anchor_values:
                anchor_values.append(normalized)
            if len(anchor_values) >= anchor_cap:
                break

    clauses: List[Dict[str, Any]] = []
    if uri_values:
        clauses.append({"op": "must", "field": "uri", "conds": uri_values})
    if anchor_values:
        clauses.append({"op": "must", "field": "anchor_hits", "conds": anchor_values})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"op": "or", "conds": clauses}


def anchor_rerank_bonus(
    *,
    query_anchor_groups: Dict[str, set[str]],
    record_anchor_groups: Dict[str, set[str]],
) -> tuple[float, List[str]]:
    """Compute a cheap structured-anchor rerank bonus."""
    weights = {
        QueryAnchorKind.TIME.value: 0.18,
        QueryAnchorKind.ENTITY.value: 0.14,
        QueryAnchorKind.TOPIC.value: 0.08,
        QueryAnchorKind.PROFILE.value: 0.06,
    }
    caps = {
        QueryAnchorKind.TIME.value: 0.24,
        QueryAnchorKind.ENTITY.value: 0.22,
        QueryAnchorKind.TOPIC.value: 0.16,
        QueryAnchorKind.PROFILE.value: 0.06,
    }
    all_record_values = (
        set().union(*record_anchor_groups.values()) if record_anchor_groups else set()
    )
    bonus = 0.0
    reasons: List[str] = []
    overlap_counts: Dict[str, int] = {}

    for kind, query_values in query_anchor_groups.items():
        if not query_values:
            continue
        record_values = record_anchor_groups.get(kind, set())
        if kind == QueryAnchorKind.TOPIC.value:
            overlap = query_values & all_record_values
        else:
            overlap = query_values & record_values
            if not overlap:
                overlap = query_values & all_record_values
        if not overlap:
            continue
        overlap_count = len(overlap)
        overlap_counts[kind] = overlap_count
        bonus += min(caps.get(kind, 0.08), weights.get(kind, 0.08) * overlap_count)
        reasons.append(f"anchor_{kind}")

    if (
        overlap_counts.get(QueryAnchorKind.TIME.value, 0) > 0
        and overlap_counts.get(QueryAnchorKind.ENTITY.value, 0) > 0
    ):
        bonus += 0.12
        reasons.append("anchor_combo")
    elif (
        overlap_counts.get(QueryAnchorKind.ENTITY.value, 0) > 0
        and overlap_counts.get(QueryAnchorKind.TOPIC.value, 0) > 0
    ):
        bonus += 0.06
        reasons.append("anchor_combo")

    return min(0.55, bonus), reasons
