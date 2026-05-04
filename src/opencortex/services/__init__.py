# SPDX-License-Identifier: Apache-2.0
"""Service-tier modules extracted from ``CortexMemory``.

This namespace holds single-responsibility service classes that were
previously methods on the memory facade. Each service takes
a back-reference to CortexMemory at construction (sync, no I/O)
and exposes a focused method surface.

This is Phase 1-4 of the multi-PR decomposition documented in
``docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md``.
Phase 1 added ``MemoryService``; Phase 2 added ``KnowledgeService``;
Phase 4 added ``SystemStatusService``.
Future phases will add a lifecycle-coordination layer here.

No re-exports — import directly from the submodule, e.g.
``from opencortex.services.memory_service import MemoryService``.
"""
