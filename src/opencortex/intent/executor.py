# SPDX-License-Identifier: Apache-2.0
"""Phase 3 runtime for bounded adaptive memory retrieval."""

from __future__ import annotations

from typing import Any, Dict, List

from opencortex.intent.types import (
    ExecutionResult,
    ExecutionTrace,
    MemoryRuntimeDegrade,
    RetrievalPlan,
    ScopeLevel,
    SearchResult,
)
from opencortex.memory import retrieval_hints_for_kinds


class MemoryExecutor:
    """Bind planner posture into execution facts without semantic replanning."""

    @staticmethod
    def _raw_candidate_cap(
        *,
        max_items: int,
        recall_budget: float,
        rerank: bool,
    ) -> int:
        cap = max_items + max(4, int(round(recall_budget * 20)))
        if rerank:
            cap += 8
        return max(max_items, min(64, cap))

    @staticmethod
    def _association_mode(association_budget: float) -> str:
        if association_budget <= 0.0:
            return "off"
        if association_budget <= 0.35:
            return "light"
        if association_budget <= 0.7:
            return "normal"
        return "strong"

    def bind(
        self,
        *,
        probe_result: SearchResult,
        retrieve_plan: RetrievalPlan,
        max_items: int,
        session_id: str,
        tenant_id: str,
        user_id: str,
        project_id: str,
        include_knowledge: bool,
    ) -> Dict[str, Any]:
        """Bind the planner output into a concrete execution posture."""
        hints = retrieval_hints_for_kinds(retrieve_plan.target_memory_kinds)
        recall_budget = retrieve_plan.search_profile.recall_budget
        memory_limit = max(
            max_items,
            int(round(max_items + recall_budget * max(max_items, 4))),
        )
        knowledge_limit = 0
        sources = ["memory"]
        if include_knowledge and "resource" in hints.context_types:
            sources.append("knowledge")
            knowledge_limit = min(3, max_items)

        association_mode = self._association_mode(
            retrieve_plan.search_profile.association_budget
        )
        raw_candidate_cap = self._raw_candidate_cap(
            max_items=max_items,
            recall_budget=recall_budget,
            rerank=retrieve_plan.search_profile.rerank,
        )
        seed_uri_cap = min(12, max(3, max_items * 2))
        anchor_cap = min(8, max(3, max_items + 1))
        bind_start_points = bool(
            retrieve_plan.scope_level == ScopeLevel.GLOBAL
            and (
                probe_result.candidate_entries
                or probe_result.anchor_hits
                or retrieve_plan.query_plan.anchors
            )
        )

        return {
            "probe": probe_result.to_dict(),
            "planner": retrieve_plan.to_dict(),
            "scope": {
                "session_id": session_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "project_id": project_id,
            },
            "sources": sources,
            "context_types": hints.context_types or ["memory"],
            "category_filter": hints.categories,
            "memory_limit": memory_limit,
            "knowledge_limit": knowledge_limit,
            "planned_depth": retrieve_plan.retrieval_depth.value,
            "effective_depth": retrieve_plan.retrieval_depth.value,
            "association_mode": association_mode,
            "rerank": retrieve_plan.search_profile.rerank,
            "raw_candidate_cap": raw_candidate_cap,
            "seed_uri_cap": seed_uri_cap,
            "anchor_cap": anchor_cap,
            "bind_start_points": bind_start_points,
            "degrade": MemoryRuntimeDegrade().to_dict(),
        }

    def finalize(
        self,
        *,
        bound_plan: Dict[str, Any],
        items: List[Dict[str, Any]],
        latency_ms: int,
        stage_timing_ms: Dict[str, int] | None = None,
        retrieve_breakdown_ms: Dict[str, float] | None = None,
        hydration_actions: List[Dict[str, Any]] | None = None,
        early_stop: bool = False,
    ) -> ExecutionResult:
        """Emit runtime result facts for a completed execution."""
        trace = ExecutionTrace(
            probe=dict(bound_plan.get("probe") or {}),
            planner=dict(bound_plan.get("planner") or {}),
            effective={
                "sources": list(bound_plan["sources"]),
                "context_types": list(bound_plan["context_types"]),
                "category_filter": list(bound_plan["category_filter"]),
                "memory_limit": bound_plan["memory_limit"],
                "knowledge_limit": bound_plan["knowledge_limit"],
                "retrieval_depth": bound_plan["effective_depth"],
                "association_mode": bound_plan["association_mode"],
                "rerank": bound_plan["rerank"],
                "raw_candidate_cap": bound_plan["raw_candidate_cap"],
                "seed_uri_cap": bound_plan["seed_uri_cap"],
                "anchor_cap": bound_plan["anchor_cap"],
                "bind_start_points": bound_plan["bind_start_points"],
                "early_stop": early_stop,
            },
            hydration=list(hydration_actions or []),
            latency_ms={
                "execution": latency_ms,
                "stages": dict(stage_timing_ms or {}),
                "retrieve": dict(retrieve_breakdown_ms or {}),
            },
        )
        return ExecutionResult(
            items=items,
            trace=trace,
            degrade=MemoryRuntimeDegrade(**dict(bound_plan["degrade"])),
        )

    def apply_degrade(
        self,
        *,
        bound_plan: Dict[str, Any],
        reasons: List[str],
        actions: List[str],
    ) -> Dict[str, Any]:
        """Apply a deterministic degrade action list."""
        degraded = {
            **bound_plan,
            "degrade": MemoryRuntimeDegrade(
                applied=True,
                reasons=list(reasons),
                actions=list(actions),
            ).to_dict(),
        }
        if "disable_rerank" in actions:
            degraded["rerank"] = False
        if "disable_association" in actions:
            degraded["association_mode"] = "off"
        if "narrow_recall" in actions:
            degraded["memory_limit"] = max(1, int(degraded["memory_limit"] * 0.7))
            degraded["raw_candidate_cap"] = max(
                degraded["memory_limit"],
                int(degraded["raw_candidate_cap"] * 0.7),
            )
        runtime_result = self.finalize(
            bound_plan=degraded,
            items=[],
            latency_ms=0,
        )
        return runtime_result.to_dict()
