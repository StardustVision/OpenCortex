# SPDX-License-Identifier: Apache-2.0
"""Object-aware planner for memory retrieval."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List, Optional

from opencortex.intent.types import (
    MemoryCoarseClass,
    MemoryQueryPlan,
    MemorySearchProfile,
    ProbeScopeSource,
    QueryAnchor,
    QueryAnchorKind,
    QueryRewriteMode,
    RetrievalDepth,
    RetrievalPlan,
    ScopeLevel,
    SearchResult,
    probe_confidence,
)
from opencortex.memory import MemoryKind

_TIME_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{4}|\d{1,2}:\d{2}|yesterday|today|tomorrow|last|next)\b",
    re.IGNORECASE,
)
_CAPITALIZED_ENTITY_RE = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*\b")
_CJK_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_STOP_WORDS = {
    "what", "which", "who", "when", "where", "why", "how", "did", "does", "about",
    "from", "with", "that", "this", "have", "been", "were", "your", "their", "there",
    "then", "into", "just", "will", "would", "could", "should", "please", "tell",
    "give", "show", "list", "summarize", "summary", "recap", "overview", "compare",
    "based",
    "过去", "之前", "一下", "一下子", "关于", "我们", "什么", "哪个", "哪些", "之前",
    "现在", "需要", "还有",
}
_PROFILE_HINT_RE = re.compile(
    r"\b(?:prefer|usually|habit|style|taste|favorite|favourite|recommend|constraint)\b|"
    r"(偏好|习惯|通常|口味|风格|推荐|约束|不喜欢|喜欢)",
    re.IGNORECASE,
)
_EXPLORE_HINT_RE = re.compile(
    r"\b(?:summarize|summary|recap|overview|compare|list|pattern|aggregate|count|all)\b|"
    r"(总结|回顾|概述|比较|列出|统计|归纳|全部|所有)",
    re.IGNORECASE,
)
_RELATIONAL_HINT_RE = re.compile(
    r"\b(?:before|after|between|with|related|relationship|latest|first|last)\b|"
    r"(之前|之后|之间|关联|关系|一起|最近|第一次|最后一次)",
    re.IGNORECASE,
)
_FULL_EVIDENCE_RE = re.compile(
    r"\b(?:full|entire|verbatim|original|complete content|raw content)\b|"
    r"(原文|全文|完整内容|完整记录|逐字)",
    re.IGNORECASE,
)

_BASE_MEMORY_KINDS = {
    MemoryCoarseClass.LOOKUP: [MemoryKind.EVENT, MemoryKind.SUMMARY],
    MemoryCoarseClass.PROFILE: [
        MemoryKind.PROFILE,
        MemoryKind.PREFERENCE,
        MemoryKind.CONSTRAINT,
    ],
    MemoryCoarseClass.EXPLORE: [
        MemoryKind.SUMMARY,
        MemoryKind.EVENT,
        MemoryKind.DOCUMENT_CHUNK,
    ],
    MemoryCoarseClass.RELATIONAL: [
        MemoryKind.RELATION,
        MemoryKind.EVENT,
        MemoryKind.DOCUMENT_CHUNK,
    ],
}

_BASE_SEARCH_PROFILE = {
    MemoryCoarseClass.LOOKUP: {"recall": 0.24, "association": 0.0, "rerank": False},
    MemoryCoarseClass.PROFILE: {"recall": 0.30, "association": 0.0, "rerank": True},
    MemoryCoarseClass.EXPLORE: {"recall": 0.48, "association": 0.35, "rerank": True},
    MemoryCoarseClass.RELATIONAL: {"recall": 0.40, "association": 0.60, "rerank": True},
}


class RecallPlanner:
    """Build a bounded object-aware retrieval plan."""

    def __init__(self, *, cone_enabled: bool) -> None:
        self._cone_enabled = cone_enabled

    def semantic_plan(
        self,
        *,
        query: str,
        probe_result: SearchResult,
        max_items: int,
        recall_mode: str,
        detail_level_override: Optional[str],
        scope_input: Optional[Any] = None,
    ) -> Optional[RetrievalPlan]:
        """Build the Phase 2 planner output from bounded probe evidence."""
        if recall_mode == "never":
            return None

        scope_level, scope_source, scope_filter, root_uris = self._select_scope(
            probe_result=probe_result,
            scope_input=scope_input,
        )

        if (
            probe_result.scope_authoritative
            and not probe_result.candidate_entries
            and not probe_result.anchor_hits
        ):
            return None

        confidence = probe_confidence(probe_result)
        coarse_class = self._infer_class_prior(
            query=query,
            probe_result=probe_result,
            max_items=max_items,
        )
        anchors = self._extract_anchors(query, probe_result)
        retrieval_depth = self._retrieval_depth(
            query=query,
            coarse_class=coarse_class,
            confidence=confidence,
            probe_result=probe_result,
            detail_level_override=detail_level_override,
        )

        session_scope = None
        if (
            scope_level == ScopeLevel.SESSION_ONLY
            or scope_source == ProbeScopeSource.SESSION_ID
        ):
            for sp in probe_result.starting_points:
                if sp.session_id:
                    session_scope = sp.session_id
                    break

        return RetrievalPlan(
            target_memory_kinds=self._target_memory_kinds(
                query=query,
                coarse_class=coarse_class,
                anchors=anchors,
                probe_result=probe_result,
            ),
            query_plan=MemoryQueryPlan(
                anchors=anchors,
                rewrite_mode=QueryRewriteMode.NONE,
            ),
            search_profile=self._search_profile(
                coarse_class=coarse_class,
                confidence=confidence,
                max_items=max_items,
                probe_result=probe_result,
                anchors=anchors,
            ),
            retrieval_depth=retrieval_depth,
            scope_level=scope_level,
            session_scope=session_scope,
            confidence=confidence,
            decision=self._decision_for_depth(retrieval_depth),
            drill_uris=[],
            expand_anchors=[],
            scope_filter=scope_filter,
        )

    def _select_scope(
        self,
        *,
        probe_result: SearchResult,
        scope_input: Optional[Any],
    ) -> tuple[ScopeLevel, ProbeScopeSource, Optional[Dict[str, Any]], List[str]]:
        """Infer scope from probe signals and caller scope context."""
        if scope_input is None:
            return ScopeLevel.GLOBAL, ProbeScopeSource.GLOBAL_ROOT, None, []

        source = scope_input.source
        starting_points = probe_result.starting_points

        if source == ProbeScopeSource.TARGET_URI:
            root_uris = [scope_input.target_uri] if scope_input.target_uri else []
            parent_uris = [sp.uri for sp in starting_points if sp.uri]
            scope_filter = (
                {"op": "must", "field": "parent_uri", "conds": parent_uris}
                if parent_uris
                else None
            )
            return ScopeLevel.CONTAINER_SCOPED, source, scope_filter, root_uris

        if source == ProbeScopeSource.SESSION_ID:
            session_ids = list({
                sp.session_id
                for sp in starting_points
                if sp.session_id
            })
            scope_filter = (
                {"op": "must", "field": "session_id", "conds": session_ids}
                if session_ids
                else None
            )
            root_uris = [sp.uri for sp in starting_points if sp.session_id][:3]
            return ScopeLevel.SESSION_ONLY, source, scope_filter, root_uris

        if source == ProbeScopeSource.SOURCE_DOC_ID:
            doc_ids = list({
                sp.source_doc_id
                for sp in starting_points
                if sp.source_doc_id
            })
            scope_filter = (
                {"op": "must", "field": "source_doc_id", "conds": doc_ids}
                if doc_ids
                else None
            )
            root_uris = [sp.uri for sp in starting_points if sp.source_doc_id][:3]
            return ScopeLevel.DOCUMENT_ONLY, source, scope_filter, root_uris

        if starting_points:
            top = starting_points[0]
            if top.session_id:
                session_id = top.session_id
                filtered = [
                    sp for sp in starting_points if sp.session_id == session_id
                ][:3]
                scope_filter = {
                    "op": "must",
                    "field": "session_id",
                    "conds": [session_id],
                }
                root_uris = [sp.uri for sp in filtered]
                return ScopeLevel.SESSION_ONLY, ProbeScopeSource.SESSION_ID, scope_filter, root_uris
            if top.source_doc_id:
                doc_id = top.source_doc_id
                filtered = [
                    sp for sp in starting_points if sp.source_doc_id == doc_id
                ][:3]
                scope_filter = {
                    "op": "must",
                    "field": "source_doc_id",
                    "conds": [doc_id],
                }
                root_uris = [sp.uri for sp in filtered]
                return ScopeLevel.DOCUMENT_ONLY, ProbeScopeSource.SOURCE_DOC_ID, scope_filter, root_uris

        return ScopeLevel.GLOBAL, source, None, []

    def _infer_class_prior(
        self,
        *,
        query: str,
        probe_result: SearchResult,
        max_items: int,
    ) -> MemoryCoarseClass:
        evidence = probe_result.evidence
        kind_counts = Counter(
            candidate.memory_kind for candidate in probe_result.candidate_entries
        )
        if kind_counts[MemoryKind.RELATION] > 0:
            return MemoryCoarseClass.RELATIONAL
        if kind_counts[MemoryKind.PROFILE] > 0 or kind_counts[MemoryKind.PREFERENCE] > 0:
            return MemoryCoarseClass.PROFILE
        if kind_counts[MemoryKind.CONSTRAINT] > 0 and evidence.anchor_hit_count > 0:
            return MemoryCoarseClass.PROFILE
        if (
            kind_counts[MemoryKind.DOCUMENT_CHUNK] > 0
            and (evidence.candidate_count >= 2 or max_items >= 6)
        ):
            return MemoryCoarseClass.EXPLORE
        if evidence.anchor_hit_count >= 3 and evidence.candidate_count >= 3:
            return MemoryCoarseClass.EXPLORE
        if _PROFILE_HINT_RE.search(query):
            return MemoryCoarseClass.PROFILE
        if _RELATIONAL_HINT_RE.search(query):
            return MemoryCoarseClass.RELATIONAL
        if _EXPLORE_HINT_RE.search(query) or max_items >= 8:
            return MemoryCoarseClass.EXPLORE
        return MemoryCoarseClass.LOOKUP

    def _extract_anchors(
        self,
        query: str,
        probe_result: SearchResult,
    ) -> list[QueryAnchor]:
        """Extract query-derived anchors without feeding probe evidence back in."""
        anchors: list[QueryAnchor] = []

        def _append_anchor(kind: QueryAnchorKind, value: str) -> None:
            normalized = str(value or "").strip()
            if not normalized:
                return
            lowered = normalized.lower()
            if lowered in _STOP_WORDS:
                return
            if any(anchor.value.lower() == lowered for anchor in anchors):
                return
            anchors.append(QueryAnchor(kind=kind, value=normalized))

        for match in _TIME_RE.findall(query):
            _append_anchor(QueryAnchorKind.TIME, match)

        for value in probe_result.query_entities:
            kind = QueryAnchorKind.TIME if _TIME_RE.search(value) else QueryAnchorKind.ENTITY
            _append_anchor(kind, value)
            if len(anchors) >= 6:
                return anchors[:6]

        for match in _CAPITALIZED_ENTITY_RE.findall(query):
            _append_anchor(QueryAnchorKind.ENTITY, match)
            if len(anchors) >= 6:
                return anchors[:6]

        if _PROFILE_HINT_RE.search(query):
            _append_anchor(QueryAnchorKind.PROFILE, "profile")

        for token in _CJK_TOKEN_RE.findall(query):
            _append_anchor(QueryAnchorKind.TOPIC, token)
            if len(anchors) >= 6:
                return anchors[:6]

        for token in _TOKEN_RE.findall(query):
            _append_anchor(QueryAnchorKind.TOPIC, token)
            if len(anchors) >= 6:
                return anchors[:6]

        return anchors[:6]

    def _target_memory_kinds(
        self,
        *,
        query: str,
        coarse_class: MemoryCoarseClass,
        anchors: list[QueryAnchor],
        probe_result: SearchResult,
    ) -> list[MemoryKind]:
        ordered = list(_BASE_MEMORY_KINDS[coarse_class])

        for hit in probe_result.candidate_entries:
            self._promote(ordered, hit.memory_kind)

        if _PROFILE_HINT_RE.search(query):
            self._promote(ordered, MemoryKind.PROFILE)
            self._promote(ordered, MemoryKind.PREFERENCE)

        if _FULL_EVIDENCE_RE.search(query):
            self._promote(ordered, MemoryKind.DOCUMENT_CHUNK)

        if any(anchor.kind == QueryAnchorKind.TIME for anchor in anchors):
            self._promote(ordered, MemoryKind.EVENT)

        if coarse_class == MemoryCoarseClass.RELATIONAL and (
            probe_result.evidence.anchor_hit_count > 1
            or any(anchor.kind == QueryAnchorKind.ENTITY for anchor in anchors)
        ):
            self._promote(ordered, MemoryKind.RELATION)

        return ordered

    def _search_profile(
        self,
        *,
        coarse_class: MemoryCoarseClass,
        confidence: float,
        max_items: int,
        probe_result: SearchResult,
        anchors: list[QueryAnchor],
    ) -> MemorySearchProfile:
        evidence = probe_result.evidence
        policy = _BASE_SEARCH_PROFILE[coarse_class]
        recall_budget = policy["recall"]

        if evidence.object_candidate_count == 0 and evidence.anchor_candidate_count > 0:
            recall_budget += 0.12
        elif evidence.candidate_count == 0:
            recall_budget += 0.14
        elif confidence < 0.45:
            recall_budget += 0.08
        elif confidence > 0.8 and evidence.candidate_count <= 2:
            recall_budget -= 0.05

        recall_budget += min(max_items, 10) / 150.0
        recall_budget = round(max(0.15, min(0.75, recall_budget)), 4)

        has_starting_points = bool(probe_result.starting_points)
        has_starting_point_anchors = bool(probe_result.starting_point_anchors)
        strong_entity_anchor = bool(
            any(anchor.kind == QueryAnchorKind.ENTITY for anchor in anchors)
            or any(starting_point.entities for starting_point in probe_result.starting_points)
        )

        association_budget = 0.0
        if (
            self._cone_enabled
            and strong_entity_anchor
            and coarse_class in {MemoryCoarseClass.RELATIONAL, MemoryCoarseClass.EXPLORE}
        ):
            if has_starting_points and not has_starting_point_anchors:
                association_budget = 0.0
            else:
                association_budget = 0.24
                if coarse_class == MemoryCoarseClass.RELATIONAL:
                    association_budget += 0.12
                if evidence.anchor_hit_count > 0:
                    association_budget += min(0.16, evidence.anchor_hit_count * 0.04)
                if confidence > 0.8 and coarse_class == MemoryCoarseClass.EXPLORE:
                    association_budget -= 0.04
                association_budget = round(max(0.0, min(0.65, association_budget)), 4)

        rerank = bool(
            policy["rerank"]
            or confidence < 0.65
            or evidence.anchor_hit_count > 0
        )
        return MemorySearchProfile(
            recall_budget=recall_budget,
            association_budget=association_budget,
            rerank=rerank,
        )

    def _retrieval_depth(
        self,
        *,
        query: str,
        coarse_class: MemoryCoarseClass,
        confidence: float,
        probe_result: SearchResult,
        detail_level_override: Optional[str],
    ) -> RetrievalDepth:
        if detail_level_override:
            try:
                return RetrievalDepth(detail_level_override)
            except ValueError:
                pass

        if _FULL_EVIDENCE_RE.search(query):
            return RetrievalDepth.L2
        if self._l0_is_sufficient(probe_result, confidence):
            return RetrievalDepth.L0

        return RetrievalDepth.L2

    @staticmethod
    def _l0_is_sufficient(
        probe_result: SearchResult,
        confidence: float,
    ) -> bool:
        evidence = probe_result.evidence
        return bool(
            probe_result.candidate_entries
            and evidence.object_candidate_count > 0
            and evidence.candidate_count <= 2
            and confidence >= 0.76
            and (evidence.score_gap or 0.0) >= 0.08
        )

    @staticmethod
    def _decision_for_depth(depth: RetrievalDepth) -> str:
        if depth == RetrievalDepth.L0:
            return "stop_l0"
        if depth == RetrievalDepth.L1:
            return "stop_l1"
        return "hydrate_l2"

    @staticmethod
    def _promote(values: list[MemoryKind], target: MemoryKind) -> None:
        if target not in values:
            values.insert(0, target)
            return
        values.insert(0, values.pop(values.index(target)))
