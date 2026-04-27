# SPDX-License-Identifier: Apache-2.0
"""System status and derive-pipeline service extracted from MemoryOrchestrator.

All 6 public methods have been extracted from ``MemoryOrchestrator`` as
part of plan 013 (Phase 4 of the God Object decomposition). This module
owns system health reporting, derive-pipeline state queries, and
re-embedding.

Boundary
--------
``SystemStatusService`` is responsible for:
- System health and statistics (``health_check``, ``stats``, ``system_status``)
- Async derive-pipeline state (``derive_status``, ``wait_deferred_derives``)
- Re-embedding (``reembed_all``)

It is explicitly NOT responsible for:
- Memory record CRUD — owned by ``MemoryService``
- Knowledge lifecycle — owned by ``KnowledgeService``
- Connection sweeper helpers (``_maybe_warn_pool``) — remain on orchestrator
  until Phase 6 (BackgroundTaskManager)
- Subsystem boot sequencing — Phase 5

Design
------
The service holds a back-reference to the orchestrator (``self._orch``)
and reaches into orchestrator-owned subsystems at call time. Construction
is sync and cheap — no I/O, no model loading. The orchestrator lazily
builds a single ``SystemStatusService`` instance via the
``_system_status_service`` property so that delegate methods can call
``self._system_status_service.X`` without ``if None`` guards.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

from opencortex.http.request_context import get_effective_identity

if TYPE_CHECKING:
    from opencortex.orchestrator import MemoryOrchestrator

logger = logging.getLogger(__name__)


class SystemStatusService:
    """System status, derive-pipeline state, and re-embedding surface.

    All 6 public methods have been extracted from ``MemoryOrchestrator``
    as part of plan 013. The service is lazily constructed by the
    orchestrator and delegates to orchestrator-owned subsystems via
    ``self._orch``.
    """

    def __init__(self, orchestrator: "MemoryOrchestrator") -> None:
        """Bind the service to its parent orchestrator.

        Args:
            orchestrator: The ``MemoryOrchestrator`` instance whose
                subsystems this service reaches into at call time.
                Stored as ``self._orch``; not validated.
        """
        self._orch = orchestrator

    # =========================================================================
    # Health and statistics
    # =========================================================================

    async def health_check(self) -> Dict[str, Any]:
        """Check health of all components.

        Returns:
            Dict with ``initialized``, ``storage``, ``embedder``, and
            ``llm`` boolean flags.
        """
        orch = self._orch
        result = {
            "initialized": orch._initialized,
            "storage": False,
            "embedder": orch._embedder is not None,
            "llm": orch._llm_completion is not None,
        }
        if orch._initialized and orch._storage:
            result["storage"] = await orch._storage.health_check()
        return result

    async def stats(self) -> Dict[str, Any]:
        """Get orchestrator statistics.

        Returns:
            Dict with ``tenant_id``, ``user_id``, ``storage``, ``embedder``,
            ``has_llm``, and ``rerank`` fields.
        """
        orch = self._orch
        orch._ensure_init()

        storage_stats = await orch._storage.get_stats()
        rerank_info = {
            "enabled": False,
            "mode": "disabled",
            "model": None,
            "fusion_beta": 0.0,
        }
        rerank_cfg = orch._build_rerank_config()
        if rerank_cfg.is_available():
            rerank_info = {
                "enabled": True,
                "mode": rerank_cfg.provider,
                "model": orch._config.rerank_model or None,
                "fusion_beta": rerank_cfg.fusion_beta,
            }
        tid, uid = get_effective_identity()
        return {
            "tenant_id": tid,
            "user_id": uid,
            "storage": storage_stats,
            "embedder": orch._embedder.model_name if orch._embedder else None,
            "has_llm": orch._llm_completion is not None,
            "rerank": rerank_info,
        }

    async def system_status(self, status_type: str = "doctor") -> Dict[str, Any]:
        """Unified system status endpoint.

        Args:
            status_type: One of ``"health"``, ``"stats"``, or ``"doctor"``
                (default). ``"doctor"`` merges health and stats with an
                ``issues`` list highlighting unavailable components.

        Returns:
            Dict whose shape depends on ``status_type``:
            - ``"health"``: component health flags
            - ``"stats"``: storage and rerank statistics
            - ``"doctor"``: merged health + stats + ``issues`` list
        """
        if status_type == "health":
            return await self.health_check()
        if status_type == "stats":
            return await self.stats()
        # doctor
        health = await self.health_check()
        st = await self.stats()
        issues = []
        if not health.get("storage"):
            issues.append("Storage unavailable")
        if not health.get("embedder"):
            issues.append("Embedder unavailable")
        if not health.get("llm"):
            issues.append(
                "No LLM configured — intent analysis and session extraction disabled"
            )
        return {**health, **st, "issues": issues}

    # =========================================================================
    # Derive pipeline state
    # =========================================================================

    async def derive_status(self, uri: str) -> Dict[str, Any]:
        """Check the async derive status for a document URI.

        Args:
            uri: The document URI to check.

        Returns:
            Dict with ``uri`` and ``status`` key: ``"pending"``,
            ``"completed"``, or ``"not_found"``.
        """
        orch = self._orch
        if uri in orch._inflight_derive_uris:
            return {"uri": uri, "status": "pending"}

        fs_path = orch._fs._uri_to_path(uri)
        try:
            orch._fs.agfs.read(f"{fs_path}/.derive_pending")
            return {"uri": uri, "status": "pending"}
        except FileNotFoundError:
            pass

        records = await orch._storage.filter(
            orch._get_collection(),
            {"op": "must", "field": "uri", "conds": [uri]},
            limit=1,
        )
        if records:
            return {"uri": uri, "status": "completed"}

        return {"uri": uri, "status": "not_found"}

    async def wait_deferred_derives(self, poll_interval: float = 1.0) -> None:
        """Wait until all in-flight deferred derives complete.

        Args:
            poll_interval: Seconds to sleep between polls. Defaults to 1.0.
        """
        orch = self._orch
        while orch._deferred_derive_count > 0:
            logger.info(
                "[SystemStatusService] waiting for %d deferred derives...",
                orch._deferred_derive_count,
            )
            await asyncio.sleep(poll_interval)

    # =========================================================================
    # Re-embedding
    # =========================================================================

    async def reembed_all(self) -> int:
        """Re-embed all records with the current embedder.

        Can be called manually or via the admin HTTP endpoint.

        Returns:
            Number of records updated.
        """
        from opencortex.migration.v040_reembed import reembed_all as _reembed_all

        orch = self._orch
        count = await _reembed_all(
            orch._storage,
            orch._get_collection(),
            orch._embedder,
        )
        # Update model marker
        marker = Path(orch._config.data_root) / ".embedding_model"
        marker.write_text(getattr(orch._embedder, "model_name", ""))
        return count
