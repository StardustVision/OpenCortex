# SPDX-License-Identifier: Apache-2.0
"""Session recomposition engine extracted from ``ContextManager``.

Owns the segmentation, clustering, merging, and LLM-derivation pipeline
that transforms incremental conversation records into structured directory
and session-summary hierarchies.  The engine takes a back-reference to the
``ContextManager`` at construction (sync, no I/O) and manages the focused
recomposition concern.

This is part of the multi-PR decomposition documented in
``docs/plans/2026-04-27-017-refactor-contextmanager-recomposition-plan.md``.

No re-exports — import directly from the submodule, e.g.
``from opencortex.context.recomposition_engine import SessionRecompositionEngine``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencortex.context.manager import ContextManager


class SessionRecompositionEngine:
    """Owns the conversation recomposition pipeline for ContextManager.

    The engine manages segmentation (anchor clustering + time-based splitting),
    LLM-driven parent derivation, merge buffer flushing, and session summary
    generation. It holds the task coordination state for merge and recompose
    background work.
    """

    def __init__(self, manager: ContextManager) -> None:
        self._mgr = manager
