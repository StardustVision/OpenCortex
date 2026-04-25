# SPDX-License-Identifier: Apache-2.0
"""Memory record CRUD + scoring service extracted from MemoryOrchestrator.

This module is Phase 1 of the
``docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md``
decomposition. It hosts the methods that read and write individual
memory records and adjust their reward / decay / protection state.

Boundary
--------
``MemoryService`` is responsible for:
- Memory record CRUD: ``add``, ``update``, ``remove``, ``batch_add``
- Memory record queries: ``search``, ``list_memories``, ``memory_index``,
  ``list_memories_admin``
- Memory record scoring + lifecycle adjuncts: ``feedback``,
  ``feedback_batch``, ``decay``, ``cleanup_expired_staging``,
  ``protect``, ``get_profile``

It is explicitly NOT responsible for:
- Knowledge management (``knowledge_*``, archivist) — Phase 2
- System status reporting — Phase 3
- Subsystem boot sequencing — Phase 4
- Periodic background tasks (autophagy / connection sweepers / derive
  worker) — Phase 5
- Conversation lifecycle (``session_*``, benchmark ingest) — already
  delegated to ``ContextManager``
- Storage adapters, embedders, recall planning, intent routing — owned
  by their respective modules

Design
------
The service holds a back-reference to the orchestrator
(``self._orch``) and reaches into orchestrator-owned subsystems
(``_storage``, ``_embedder``, ``_fs``, ``_recall_planner``, etc.) at
call time. This mirrors the precedent set by
``BenchmarkConversationIngestService``. Phase 4's
``SubsystemBootstrapper`` will eventually replace the back-reference
with a typed ``SubsystemContainer`` parameter; doing both swaps in
one PR would be needless churn.

Construction is sync and cheap — no I/O, no model loading. The
orchestrator builds a single ``MemoryService`` instance in
``__init__`` so that delegate methods can blindly call
``self._memory_service.X`` without ``if None`` guards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencortex.orchestrator import MemoryOrchestrator


class MemoryService:
    """Memory record CRUD + scoring surface.

    Methods are added in subsequent units of plan 010 (U2 CRUD, U3
    queries, U4 scoring). U1 lands the scaffolding only so the move
    operations stay reviewable as one-method-per-commit diffs.
    """

    def __init__(self, orchestrator: "MemoryOrchestrator") -> None:
        """Bind the service to its parent orchestrator.

        Args:
            orchestrator: The ``MemoryOrchestrator`` instance whose
                subsystems (``_storage``, ``_embedder``, ``_fs``,
                ``_recall_planner``, etc.) this service reaches into
                at call time. Stored as ``self._orch``; not validated.
        """
        self._orch = orchestrator
