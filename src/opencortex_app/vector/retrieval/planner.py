# SPDX-License-Identifier: Apache-2.0
"""Recall planner for opencortex_app."""

from __future__ import annotations

from opencortex_app.vector.retrieval.schemas import (
    ConeExpansionPlan,
    DetailLevel,
    ReasonTreePlan,
    RetrievalDecision,
    RetrievalPlan,
    RetrievalProbeResult,
    RetrievalRequest,
    RetrievalSurface,
)


class RetrievalPlanner:
    """Convert a recall request into a bounded retrieval plan."""

    surfaces = [
        RetrievalSurface.L0_OBJECT,
        RetrievalSurface.ANCHOR_INDEX,
        RetrievalSurface.FACT_INDEX,
        RetrievalSurface.ENTITY_INDEX,
        RetrievalSurface.REASON_TREE_INDEX,
    ]
    base_surface_weights = {
        RetrievalSurface.L0_OBJECT: 1.0,
        RetrievalSurface.ANCHOR_INDEX: 0.94,
        RetrievalSurface.FACT_INDEX: 0.98,
        RetrievalSurface.ENTITY_INDEX: 0.88,
        RetrievalSurface.REASON_TREE_INDEX: 0.92,
    }
    base_surface_bonus = {
        RetrievalSurface.L0_OBJECT: 0.0,
        RetrievalSurface.ANCHOR_INDEX: 0.08,
        RetrievalSurface.FACT_INDEX: 0.12,
        RetrievalSurface.ENTITY_INDEX: 0.06,
        RetrievalSurface.REASON_TREE_INDEX: 0.10,
    }

    def plan(
        self,
        request: RetrievalRequest,
        *,
        probe: RetrievalProbeResult,
    ) -> RetrievalPlan:
        """Build a deterministic retrieval plan."""
        confidence = probe_confidence(probe)
        decision = self.decision(confidence, probe)
        candidate_limit = self.candidate_limit(request.limit, confidence, probe)
        surface_weights = self.surface_weights(probe)
        return RetrievalPlan(
            query=request.query.strip(),
            limit=request.limit,
            candidate_limit=candidate_limit,
            surfaces=[]
            if decision == RetrievalDecision.NO_RECALL
            else list(self.surfaces),
            surface_limits={
                surface: self.surface_limit(surface, candidate_limit, confidence)
                for surface in self.surfaces
            },
            surface_weights=surface_weights,
            surface_bonus=dict(self.base_surface_bonus),
            starting_uris=probe.starting_uris,
            search_vectors=probe.search_vectors,
            confidence=confidence,
            decision=decision,
            depth=self.depth(decision),
            reason_tree=self.reason_tree_plan(decision, confidence, probe),
            cone_expansion=self.cone_expansion_plan(decision, probe),
            probe=probe,
        )

    @staticmethod
    def candidate_limit(
        limit: int,
        confidence: float,
        probe: RetrievalProbeResult,
    ) -> int:
        """Return total candidate budget for executor fan-out."""
        base = max(limit * 6, limit + 12)
        if confidence < 0.45 or probe.evidence.candidate_count == 0:
            base += limit * 3
        if probe.evidence.locator_candidate_count > 0:
            base += limit
        return min(96, base)

    @staticmethod
    def surface_limit(
        surface: RetrievalSurface,
        candidate_limit: int,
        confidence: float,
    ) -> int:
        """Return per-surface search budget."""
        if surface == RetrievalSurface.L0_OBJECT:
            return max(8, candidate_limit // 2)
        if surface == RetrievalSurface.REASON_TREE_INDEX:
            return max(6, candidate_limit // 3)
        if confidence < 0.45:
            return max(8, candidate_limit // 3)
        return max(5, candidate_limit // 4)

    def surface_weights(
        self,
        probe: RetrievalProbeResult,
    ) -> dict[RetrievalSurface, float]:
        """Return planner-selected score weights for each surface."""
        weights = dict(self.base_surface_weights)
        if probe.evidence.locator_candidate_count > 0:
            weights[RetrievalSurface.ANCHOR_INDEX] += 0.04
            weights[RetrievalSurface.ENTITY_INDEX] += 0.04
        if probe.evidence.object_candidate_count == 0:
            weights[RetrievalSurface.REASON_TREE_INDEX] += 0.05
            weights[RetrievalSurface.FACT_INDEX] += 0.04
        return weights

    @staticmethod
    def decision(
        confidence: float,
        probe: RetrievalProbeResult,
    ) -> RetrievalDecision:
        """Return a compact explanation of the planned retrieval posture."""
        if not probe.should_recall:
            return RetrievalDecision.NO_RECALL
        if confidence >= 0.76 and probe.evidence.object_candidate_count > 0:
            return RetrievalDecision.FOCUSED
        return RetrievalDecision.EXPAND

    @staticmethod
    def depth(decision: RetrievalDecision) -> DetailLevel:
        """Return hydration depth for the retrieval posture."""
        if decision == RetrievalDecision.NO_RECALL:
            return DetailLevel.L0
        return DetailLevel.L2

    @staticmethod
    def reason_tree_plan(
        decision: RetrievalDecision,
        confidence: float,
        probe: RetrievalProbeResult,
    ) -> ReasonTreePlan:
        """Return optional LLM reason-tree selection plan."""
        multi_query = len(probe.search_vectors) > 1
        locator_only = (
            probe.evidence.object_candidate_count == 0
            and probe.evidence.locator_candidate_count > 0
        )
        should_use_llm = (
            decision != RetrievalDecision.NO_RECALL
            and bool(probe.starting_uris)
            and (multi_query or locator_only or confidence < 0.76)
        )
        return ReasonTreePlan(
            enabled=should_use_llm,
            use_llm=should_use_llm,
            max_nodes=6 if should_use_llm else 0,
        )

    @staticmethod
    def cone_expansion_plan(
        decision: RetrievalDecision,
        probe: RetrievalProbeResult,
    ) -> ConeExpansionPlan:
        """Return optional post-rank cone expansion plan."""
        multi_query = len(probe.search_vectors) > 1
        should_expand = (
            decision != RetrievalDecision.NO_RECALL
            and probe.evidence.candidate_count > 0
            and probe.evidence.locator_candidate_count > 0
            and (multi_query or decision == RetrievalDecision.EXPAND)
        )
        return ConeExpansionPlan(
            enabled=should_expand,
            max_seeds=3 if should_expand else 0,
            max_neighbors_per_seed=2 if should_expand else 0,
        )


def probe_confidence(probe: RetrievalProbeResult) -> float:
    """Project probe evidence into a bounded confidence score."""
    evidence = probe.evidence
    top_score = evidence.top_score or 0.0
    score_gap = evidence.score_gap or 0.0
    object_top = evidence.object_top_score or 0.0
    locator_top = evidence.locator_top_score or 0.0
    confidence = top_score + min(score_gap, 0.18)
    if object_top > 0 and locator_top > 0:
        confidence += 0.05 if abs(object_top - locator_top) <= 0.15 else 0.02
    if evidence.locator_candidate_count > 0:
        confidence += min(0.08, evidence.locator_candidate_count * 0.02)
    if evidence.candidate_count == 0:
        confidence *= 0.45 if locator_top <= 0 else 0.7
    return round(max(0.0, min(1.0, confidence)), 4)
