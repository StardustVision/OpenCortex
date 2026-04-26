# SPDX-License-Identifier: Apache-2.0
"""Lifecycle-tier modules extracted from ``MemoryOrchestrator``.

This namespace holds services responsible for the runtime lifecycle
of background tasks and subsystem boot sequencing. Each service takes
a back-reference to the orchestrator at construction (sync, no I/O)
and manages a focused lifecycle concern.

This is part of the multi-PR decomposition documented in
``docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md``.
Phase 3 added ``BackgroundTaskManager``; Phase 5 added
``SubsystemBootstrapper``.

No re-exports — import directly from the submodule, e.g.
``from opencortex.lifecycle.background_tasks import BackgroundTaskManager``.
"""
