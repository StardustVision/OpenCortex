# SPDX-License-Identifier: Apache-2.0
"""Object-aware planner for memory retrieval."""

from __future__ import annotations

import re
from typing import Optional

from opencortex.intent.types import (
    MemoryCoarseClass,
    MemoryQueryPlan,
    MemorySearchProfile,
    QueryAnchor,
    QueryAnchorKind,
    QueryRewriteMode,
    RetrievalDepth,
    RetrievalPlan,
    SearchResult,
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
    MemoryCoarseClass.LOOKUP: {"recall": 0.25, "association": 0.05, "rerank": False},
    MemoryCoarseClass.PROFILE: {"recall": 0.35, "association": 0.10, "rerank": True},
    MemoryCoarseClass.EXPLORE: {"recall": 0.70, "association": 0.25, "rerank": True},
    MemoryCoarseClass.RELATIONAL: {"recall": 0.45, "association": 0.75, "rerank": True},
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
        fallback_depth: Optional[RetrievalDepth] = None,
    ) -> Optional[RetrievalPlan]:
        """Build the Phase 2 planner output from probe evidence and raw query."""
        if recall_mode == "never":
            return None
        if recall_mode == "auto" and not probe_result.should_recall:
            return None

        coarse_class = self._infer_class_prior(query, probe_result)
        confidence = self._probe_confidence(probe_result)
        anchors = self._extract_anchors(query, probe_result)
        rewrite_mode = self._rewrite_mode(query, coarse_class, anchors)
        target_memory_kinds = self._target_memory_kinds(
            query,
            coarse_class,
            anchors,
            probe_result,
        )
        search_profile = self._search_profile(
            coarse_class=coarse_class,
            confidence=confidence,
            max_items=max_items,
            probe_result=probe_result,
        )
        retrieval_depth = self._retrieval_depth(
            query=query,
            coarse_class=coarse_class,
            confidence=confidence,
            probe_result=probe_result,
            detail_level_override=detail_level_override,
            fallback_depth=fallback_depth,
        )

        return RetrievalPlan(
            target_memory_kinds=target_memory_kinds,
            query_plan=MemoryQueryPlan(
                anchors=anchors,
                rewrite_mode=rewrite_mode,
            ),
            search_profile=search_profile,
            retrieval_depth=retrieval_depth,
        )

    def _infer_class_prior(
        self,
        query: str,
        probe_result: SearchResult,
    ) -> MemoryCoarseClass:
        hit_kinds = {hit.memory_kind for hit in probe_result.candidate_entries}
        if MemoryKind.RELATION in hit_kinds:
            return MemoryCoarseClass.RELATIONAL
        if hit_kinds & {
            MemoryKind.PROFILE,
            MemoryKind.PREFERENCE,
            MemoryKind.CONSTRAINT,
        }:
            return MemoryCoarseClass.PROFILE
        if MemoryKind.DOCUMENT_CHUNK in hit_kinds and (
            probe_result.evidence.candidate_count >= 2 or _EXPLORE_HINT_RE.search(query)
        ):
            return MemoryCoarseClass.EXPLORE
        if _PROFILE_HINT_RE.search(query):
            return MemoryCoarseClass.PROFILE
        if _RELATIONAL_HINT_RE.search(query):
            return MemoryCoarseClass.RELATIONAL
        if _EXPLORE_HINT_RE.search(query):
            return MemoryCoarseClass.EXPLORE
        return MemoryCoarseClass.LOOKUP

    def _extract_anchors(
        self,
        query: str,
        probe_result: SearchResult,
    ) -> list[QueryAnchor]:
        anchors: list[QueryAnchor] = []

        for match in _TIME_RE.findall(query):
            anchors.append(QueryAnchor(kind=QueryAnchorKind.TIME, value=match))

        for match in _CAPITALIZED_ENTITY_RE.findall(query):
            normalized = match.strip()
            if normalized.lower() in _STOP_WORDS:
                continue
            anchors.append(QueryAnchor(kind=QueryAnchorKind.ENTITY, value=normalized))

        if not anchors:
            cjk_tokens = _CJK_TOKEN_RE.findall(query)
            for token in cjk_tokens[:4]:
                if token not in _STOP_WORDS:
                    anchors.append(QueryAnchor(kind=QueryAnchorKind.TOPIC, value=token))

        if _PROFILE_HINT_RE.search(query):
            anchors.append(QueryAnchor(kind=QueryAnchorKind.PROFILE, value="profile"))

        for candidate_anchor in probe_result.anchor_hits:
            if any(anchor.value == candidate_anchor for anchor in anchors):
                continue
            anchors.append(QueryAnchor(kind=QueryAnchorKind.TOPIC, value=candidate_anchor))
            if len(anchors) >= 6:
                return anchors[:6]

        for token in _TOKEN_RE.findall(query):
            lowered = token.lower()
            if lowered in _STOP_WORDS:
                continue
            if any(anchor.value.lower() == lowered for anchor in anchors):
                continue
            anchors.append(QueryAnchor(kind=QueryAnchorKind.TOPIC, value=token))
            if len(anchors) >= 6:
                break

        return anchors[:6]

    def _rewrite_mode(
        self,
        query: str,
        coarse_class: MemoryCoarseClass,
        anchors: list[QueryAnchor],
    ) -> QueryRewriteMode:
        if coarse_class == MemoryCoarseClass.EXPLORE and len(anchors) >= 3:
            return QueryRewriteMode.DECOMPOSE
        if coarse_class == MemoryCoarseClass.RELATIONAL:
            return QueryRewriteMode.LIGHT
        if _EXPLORE_HINT_RE.search(query):
            return QueryRewriteMode.LIGHT
        if len(anchors) >= 2:
            return QueryRewriteMode.LIGHT
        return QueryRewriteMode.NONE

    def _target_memory_kinds(
        self,
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

        if any(anchor.kind == QueryAnchorKind.ENTITY for anchor in anchors):
            if coarse_class == MemoryCoarseClass.RELATIONAL:
                self._promote(ordered, MemoryKind.RELATION)
            else:
                self._promote(ordered, MemoryKind.EVENT)

        return ordered

    def _search_profile(
        self,
        *,
        coarse_class: MemoryCoarseClass,
        confidence: float,
        max_items: int,
        probe_result: SearchResult,
    ) -> MemorySearchProfile:
        policy = _BASE_SEARCH_PROFILE[coarse_class]
        widening = max(0.0, min(1.0, 1.0 - confidence))
        recall_budget = min(
            1.0,
            policy["recall"] + widening * 0.25 + min(max_items, 10) / 100.0,
        )
        if probe_result.evidence.candidate_count == 0:
            recall_budget = min(1.0, recall_budget + 0.15)
        association_budget = policy["association"]
        if self._cone_enabled:
            association_budget = min(1.0, association_budget + widening * 0.20)
        else:
            association_budget = 0.0
        rerank = bool(
            policy["rerank"]
            or confidence < 0.55
            or probe_result.evidence.candidate_count == 0
        )
        return MemorySearchProfile(
            recall_budget=round(recall_budget, 4),
            association_budget=round(association_budget, 4),
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
        fallback_depth: Optional[RetrievalDepth],
    ) -> RetrievalDepth:
        if detail_level_override:
            try:
                return RetrievalDepth(detail_level_override)
            except ValueError:
                if fallback_depth is not None:
                    return fallback_depth

        if _FULL_EVIDENCE_RE.search(query):
            return RetrievalDepth.L2
        if (
            coarse_class == MemoryCoarseClass.LOOKUP
            and self._l0_is_sufficient(probe_result, confidence)
        ):
            return RetrievalDepth.L0
        if fallback_depth is not None:
            base_depth = fallback_depth
        elif coarse_class in {
            MemoryCoarseClass.PROFILE,
            MemoryCoarseClass.RELATIONAL,
        }:
            base_depth = RetrievalDepth.L1
        else:
            base_depth = RetrievalDepth.L0

        if confidence < 0.25:
            if base_depth == RetrievalDepth.L0:
                return RetrievalDepth.L2
            if base_depth == RetrievalDepth.L1:
                return RetrievalDepth.L2
        if confidence < 0.5 and base_depth == RetrievalDepth.L0:
            return RetrievalDepth.L1

        return base_depth

    @staticmethod
    def _probe_confidence(probe_result: SearchResult) -> float:
        top_score = probe_result.evidence.top_score or 0.0
        score_gap = probe_result.evidence.score_gap or 0.0
        candidate_count = probe_result.evidence.candidate_count
        confidence = top_score
        confidence += min(score_gap, 0.25)
        if candidate_count == 0:
            confidence *= 0.4
        return round(max(0.0, min(1.0, confidence)), 4)

    @staticmethod
    def _l0_is_sufficient(
        probe_result: SearchResult,
        confidence: float,
    ) -> bool:
        return bool(
            probe_result.candidate_entries
            and probe_result.evidence.candidate_count <= 2
            and confidence >= 0.7
            and (probe_result.evidence.score_gap or 0.0) >= 0.08
        )

    @staticmethod
    def _promote(values: list[MemoryKind], target: MemoryKind) -> None:
        if target not in values:
            values.insert(0, target)
            return
        values.insert(0, values.pop(values.index(target)))
