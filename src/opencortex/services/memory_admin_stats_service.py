# SPDX-License-Identifier: Apache-2.0
"""Admin memory statistics service for OpenCortex."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from opencortex.services.memory_filters import FilterExpr

if TYPE_CHECKING:
    from opencortex.orchestrator import MemoryOrchestrator


class MemoryAdminStatsService:
    """Own admin/insights memory statistics queries."""

    def __init__(self, orchestrator: "MemoryOrchestrator") -> None:
        self._orch = orchestrator

    async def get_user_memory_stats(
        self,
        tenant_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """Get memory statistics for a user."""
        orch = self._orch
        orch._ensure_init()

        filter_expr = FilterExpr.all(
            FilterExpr.neq("context_type", "staging"),
            FilterExpr.eq("source_tenant_id", tenant_id),
            FilterExpr.eq("source_user_id", user_id),
        ).to_dict()

        memories = await orch._storage.filter(
            orch._get_collection(),
            filter_expr,
            limit=10000,
        )

        created_in_session: Dict[str, int] = {}
        total_positive = 0
        total_negative = 0

        for mem in memories:
            session_id = mem.get("session_id", "unknown")
            created_in_session[session_id] = created_in_session.get(session_id, 0) + 1
            total_positive += mem.get("positive_feedback_count", 0) or 0
            total_negative += mem.get("negative_feedback_count", 0) or 0

        return {
            "created_in_session": created_in_session,
            "total_memories": len(memories),
            "total_positive_feedback": total_positive,
            "total_negative_feedback": total_negative,
        }
