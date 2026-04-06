"""Explicit planner for recall policies."""

from __future__ import annotations

from typing import Optional

from opencortex.retrieve.types import (
    DetailLevel,
    RecallPlan,
    RecallSurface,
    SearchIntent,
)


class RecallPlanner:
    def __init__(self, *, cone_enabled: bool) -> None:
        self._cone_enabled = cone_enabled

    def plan(
        self,
        *,
        query: str,
        intent: SearchIntent,
        max_items: int,
        recall_mode: str,
        include_knowledge: bool,
        detail_level_override: Optional[str],
    ) -> RecallPlan:
        should_recall = recall_mode == "always" or (
            recall_mode == "auto" and intent.should_recall
        )

        if recall_mode == "never":
            should_recall = False

        detail_level = (
            DetailLevel(detail_level_override)
            if detail_level_override
            else intent.detail_level
        )

        if not should_recall:
            return RecallPlan(
                should_recall=False,
                surfaces=[],
                detail_level=detail_level,
                memory_limit=0,
                knowledge_limit=0,
                enable_cone=False,
                fusion_policy="none",
                reasoning=f"recall_mode={recall_mode}",
            )

        surfaces = [RecallSurface.MEMORY]
        if include_knowledge:
            surfaces.append(RecallSurface.KNOWLEDGE)

        memory_limit = max(max_items, intent.top_k)
        knowledge_limit = min(3, max_items) if include_knowledge else 0
        enable_cone = self._cone_enabled and RecallSurface.MEMORY in surfaces
        fusion_policy = (
            "memory_then_knowledge" if include_knowledge else "memory_only"
        )

        reasoning = (
            f"intent={intent.intent_type} "
            f"recall_mode={recall_mode} "
            f"include_knowledge={include_knowledge}"
        )

        return RecallPlan(
            should_recall=True,
            surfaces=surfaces,
            detail_level=detail_level,
            memory_limit=memory_limit,
            knowledge_limit=knowledge_limit,
            enable_cone=enable_cone,
            fusion_policy=fusion_policy,
            reasoning=reasoning,
        )
