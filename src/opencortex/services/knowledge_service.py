# SPDX-License-Identifier: Apache-2.0
"""Knowledge management service extracted from MemoryOrchestrator.

All 6 public knowledge methods plus the ``run_archivist`` helper have
been extracted from ``MemoryOrchestrator`` as part of plan 012 (Phase 2
of the God Object decomposition). This module owns the knowledge
lifecycle: search, approval/rejection, candidate listing, and
archivist triggering/status.

Boundary
--------
``KnowledgeService`` is responsible for:
- Knowledge search and retrieval (``knowledge_search``)
- Knowledge lifecycle management (``knowledge_approve``,
  ``knowledge_reject``, ``knowledge_list_candidates``)
- Archivist control (``archivist_trigger``, ``archivist_status``)
- Background archivist execution (``run_archivist``)

It is explicitly NOT responsible for:
- Memory record CRUD — owned by ``MemoryService``
- System status reporting — Phase 4
- Subsystem boot sequencing — Phase 5
- Periodic background tasks — Phase 6

Design
------
The service holds a back-reference to the orchestrator
(``self._orch``) and reaches into orchestrator-owned subsystems
(``_knowledge_store``, ``_archivist``, ``_trace_store``,
``_llm_completion``, ``_config``) at call time. This mirrors the
pattern established by ``MemoryService`` in plans 010/011.

Construction is sync and cheap — no I/O, no model loading. The
orchestrator lazily builds a single ``KnowledgeService`` instance
via the ``_knowledge_service`` property so that delegate methods can
call ``self._knowledge_service.X`` without ``if None`` guards.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opencortex.http.request_context import get_effective_identity

if TYPE_CHECKING:
    from opencortex.orchestrator import MemoryOrchestrator

logger = logging.getLogger(__name__)


class KnowledgeService:
    """Knowledge management + archivist control surface.

    All 7 public methods have been extracted from
    ``MemoryOrchestrator`` as part of plan 012. The service is
    lazily constructed by the orchestrator and delegates to
    orchestrator-owned subsystems via ``self._orch``.
    """

    def __init__(self, orchestrator: "MemoryOrchestrator") -> None:
        """Bind the service to its parent orchestrator.

        Args:
            orchestrator: The ``MemoryOrchestrator`` instance whose
                subsystems (``_knowledge_store``, ``_archivist``,
                ``_trace_store``, ``_llm_completion``, ``_config``)
                this service reaches into at call time. Stored as
                ``self._orch``; not validated.
        """
        self._orch = orchestrator

    # =========================================================================
    # Archivist runner
    # =========================================================================

    async def run_archivist(self, tenant_id: str, user_id: str) -> Dict[str, int]:
        """Run Archivist in background to extract knowledge from traces.

        Safety invariants:
        - Only traces whose derived knowledge ALL saved successfully
          are marked processed. Failed traces remain unprocessed for retry.
        - If archivist.run() returns [] (e.g. concurrent run already active),
          no traces are marked processed.
        - Per-knowledge errors are isolated — one failure doesn't block others.

        Args:
            tenant_id: Tenant identifier.
            user_id: User identifier.

        Returns:
            Dict with ``knowledge_candidates`` and ``knowledge_active`` counts.
        """
        orch = self._orch
        stats: Dict[str, int] = {"knowledge_candidates": 0, "knowledge_active": 0}
        if not orch._archivist or not orch._trace_store or not orch._knowledge_store:
            return stats
        try:
            from opencortex.alpha.types import KnowledgeScope, KnowledgeStatus
            from opencortex.alpha.sandbox import evaluate as sandbox_evaluate

            traces = await orch._trace_store.list_unprocessed(tenant_id)
            if not traces:
                return stats

            knowledge_items = await orch._archivist.run(
                traces, tenant_id, user_id, KnowledgeScope.USER,
            )

            # Guard: if archivist returned nothing (concurrent run or no
            # patterns found), do NOT mark traces — leave for retry.
            if not knowledge_items:
                return stats

            alpha_cfg = orch._config.cortex_alpha
            succeeded_trace_ids: set = set()
            failed_trace_ids: set = set()

            for k in knowledge_items:
                source_ids = set(k.source_trace_ids) if k.source_trace_ids else set()
                try:
                    evidence_traces = [
                        t for t in traces
                        if t.get("trace_id", t.get("id", "")) in source_ids
                    ]

                    # Run Sandbox evaluation
                    if evidence_traces and orch._llm_completion:
                        eval_result = await sandbox_evaluate(
                            knowledge_dict=k.to_dict(),
                            traces=evidence_traces,
                            llm_fn=orch._llm_completion,
                            min_traces=alpha_cfg.sandbox_min_traces,
                            min_success_rate=alpha_cfg.sandbox_min_success_rate,
                            min_source_users=alpha_cfg.sandbox_min_source_users,
                            min_source_users_private=alpha_cfg.sandbox_min_source_users_private,
                            llm_sample_size=alpha_cfg.sandbox_llm_sample_size,
                            llm_min_pass_rate=alpha_cfg.sandbox_llm_min_pass_rate,
                            require_human_approval=alpha_cfg.sandbox_require_human_approval,
                            user_auto_approve_confidence=alpha_cfg.user_auto_approve_confidence,
                        )
                        status_map = {
                            "needs_more_traces": KnowledgeStatus.CANDIDATE,
                            "needs_improvement": KnowledgeStatus.CANDIDATE,
                            "verified": KnowledgeStatus.VERIFIED,
                            "active": KnowledgeStatus.ACTIVE,
                        }
                        k.status = status_map.get(eval_result.status, KnowledgeStatus.CANDIDATE)

                    await orch._knowledge_store.save(k)
                    succeeded_trace_ids.update(source_ids)

                    if k.status == KnowledgeStatus.ACTIVE:
                        stats["knowledge_active"] += 1
                    else:
                        stats["knowledge_candidates"] += 1
                except Exception as exc:
                    failed_trace_ids.update(source_ids)
                    logger.warning(
                        "[KnowledgeService] Sandbox/save failed for knowledge %s: %s",
                        k.knowledge_id, exc,
                    )

            # Only mark traces whose knowledge all saved successfully.
            # Traces linked to failed knowledge stay unprocessed for retry.
            safe_ids = succeeded_trace_ids - failed_trace_ids
            if safe_ids:
                await orch._trace_store.mark_processed(list(safe_ids))

            logger.info(
                "[KnowledgeService] Archivist: %d candidates, %d active from %d traces "
                "(%d traces marked processed, %d retained for retry)",
                stats["knowledge_candidates"], stats["knowledge_active"],
                len(traces), len(safe_ids), len(failed_trace_ids),
            )
        except Exception as exc:
            logger.warning("[KnowledgeService] Archivist failed: %s", exc)
        return stats

    # =========================================================================
    # Knowledge API
    # =========================================================================

    async def knowledge_search(
        self,
        query: str,
        types: Optional[List[str]] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """Search the Knowledge Store.

        Args:
            query: Search query string.
            types: Optional list of knowledge types to filter by.
            limit: Maximum number of results to return.

        Returns:
            Dict with ``results`` list and ``count``.
        """
        orch = self._orch
        orch._ensure_init()
        if not orch._knowledge_store:
            return {"results": [], "error": "Knowledge store not initialized"}
        tid, uid = get_effective_identity()
        results = await orch._knowledge_store.search(
            query,
            tid,
            uid,
            types=types,
            limit=limit,
        )
        return {"results": results, "count": len(results)}

    async def knowledge_approve(self, knowledge_id: str) -> Dict[str, Any]:
        """Approve a knowledge candidate (move to active).

        Args:
            knowledge_id: ID of the knowledge candidate to approve.

        Returns:
            Dict with ``ok`` boolean, ``knowledge_id``, and ``status``.
        """
        orch = self._orch
        orch._ensure_init()
        if not orch._knowledge_store:
            return {"ok": False, "error": "Knowledge store not initialized"}
        ok = await orch._knowledge_store.approve(knowledge_id)
        return {
            "ok": ok,
            "knowledge_id": knowledge_id,
            "status": "active" if ok else "not_found",
        }

    async def knowledge_reject(self, knowledge_id: str) -> Dict[str, Any]:
        """Reject a knowledge candidate (deprecate).

        Args:
            knowledge_id: ID of the knowledge candidate to reject.

        Returns:
            Dict with ``ok`` boolean, ``knowledge_id``, and ``status``.
        """
        orch = self._orch
        orch._ensure_init()
        if not orch._knowledge_store:
            return {"ok": False, "error": "Knowledge store not initialized"}
        ok = await orch._knowledge_store.reject(knowledge_id)
        return {
            "ok": ok,
            "knowledge_id": knowledge_id,
            "status": "deprecated" if ok else "not_found",
        }

    async def knowledge_list_candidates(self) -> Dict[str, Any]:
        """List knowledge candidates pending approval.

        Returns:
            Dict with ``candidates`` list and ``count``.
        """
        orch = self._orch
        orch._ensure_init()
        if not orch._knowledge_store:
            return {"candidates": [], "error": "Knowledge store not initialized"}
        tid, uid = get_effective_identity()
        candidates = await orch._knowledge_store.list_candidates(tid, uid)
        return {"candidates": candidates, "count": len(candidates)}

    async def archivist_trigger(self) -> Dict[str, Any]:
        """Manually trigger the Archivist.

        Returns:
            Dict with ``ok`` boolean and ``status``.
        """
        orch = self._orch
        orch._ensure_init()
        if not orch._archivist:
            return {"ok": False, "error": "Archivist not initialized"}
        tid, uid = get_effective_identity()
        asyncio.create_task(self.run_archivist(tid, uid))
        return {"ok": True, "status": "triggered"}

    async def archivist_status(self) -> Dict[str, Any]:
        """Get Archivist status.

        Returns:
            Dict with ``enabled`` boolean and archivist status details.
        """
        orch = self._orch
        if not orch._archivist:
            return {"enabled": False}
        return {"enabled": True, **orch._archivist.status}
