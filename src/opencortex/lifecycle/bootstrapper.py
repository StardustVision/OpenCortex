# SPDX-License-Identifier: Apache-2.0
"""Subsystem boot sequencing service extracted from MemoryOrchestrator.

The 11-step ``init()`` boot sequence and its helper methods have been
extracted from ``MemoryOrchestrator`` as part of plan 015 (Phase 5 of
the God Object decomposition). This module owns the creation and wiring
of every subsystem that the orchestrator depends on.

Boundary
--------
``SubsystemBootstrapper`` is responsible for:
- The full ``init()`` boot sequence (storage, embedder, CortexFS,
  collections, intent analyzer, cone retrieval, memory probe,
  background maintenance, cognition, alpha pipeline, skill engine)
- Helper methods: ``_init_cognition``, ``_init_alpha``,
  ``_init_skill_engine``, ``_create_default_embedder``,
  ``_startup_maintenance``, ``_check_and_reembed``

It is explicitly NOT responsible for:
- Memory record CRUD — owned by ``MemoryService``
- Knowledge lifecycle — owned by ``KnowledgeService``
- Background task lifecycle — owned by ``BackgroundTaskManager``
- System status reporting — owned by ``SystemStatusService``
- Retrieval-time helpers (``_build_probe_filter``, etc.) — stay on
  the orchestrator

Design
------
The service holds a back-reference to the orchestrator (``self._orch``)
and creates/wires subsystems by assigning to ``self._orch._X`` attributes
at boot time. All subsystem attributes remain on the orchestrator for
admin route compatibility. Construction is sync and cheap — no I/O, no
model loading. The orchestrator lazily builds a single
``SubsystemBootstrapper`` instance via the ``_bootstrapper`` property.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from opencortex.orchestrator import MemoryOrchestrator

logger = logging.getLogger(__name__)


class SubsystemBootstrapper:
    """Subsystem creation and wiring for MemoryOrchestrator.

    Owns the 11-step boot sequence that creates storage, embedder,
    CortexFS, intent analyzer, cognition components, alpha pipeline,
    skill engine, and all supporting subsystems. The orchestrator's
    ``init()`` delegates to ``SubsystemBootstrapper.init()``.

    Args:
        orchestrator: The parent MemoryOrchestrator instance.
            Subsystems are assigned as attributes on this object.
    """

    def __init__(self, orchestrator: MemoryOrchestrator) -> None:
        self._orch = orchestrator
