---
title: "refactor: Extract SubsystemBootstrapper from MemoryOrchestrator (Phase 5)"
type: refactor
status: active
date: 2026-04-26
origin: docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md
---

# refactor: Extract SubsystemBootstrapper from MemoryOrchestrator (Phase 5)

## Overview

Phase 5 of the 8-phase God Object decomposition. Extracts the 11-step `init()` boot sequence and its helper methods (~300 lines) from `MemoryOrchestrator` into a dedicated `SubsystemBootstrapper` at `src/opencortex/lifecycle/bootstrapper.py`. The orchestrator's `init()` shrinks to a thin delegate call. Same MOVE-not-REWRITE discipline as Phases 1-4 — method bodies land verbatim, orchestrator keeps a one-line delegate, all existing tests pass unchanged.

---

## Problem Frame

Plan 010 defines the full decomposition roadmap. Phases 1-4 (MemoryService, KnowledgeService, BackgroundTaskManager, SystemStatusService, PRs #16-#19) moved ~2400 lines. The orchestrator is now ~4042 lines. Phase 5 targets the initialization pathway: the `init()` method (lines 339-463, ~125 lines) and its four helper methods (`_init_cognition`, `_init_alpha`, `_init_skill_engine`, `_create_default_embedder`, `_startup_maintenance`, `_check_and_reembed` — ~180 lines combined). The bootstrapper owns subsystem creation and wiring; the orchestrator becomes a thin facade that delegates `init()` to the bootstrapper.

---

## Requirements Trace

- R1. `SubsystemBootstrapper` lives at `src/opencortex/lifecycle/bootstrapper.py` and follows the established back-reference pattern: `SubsystemBootstrapper(orchestrator)` with `self._orch` access. (origin: plan 010 Phase 5 spec)
- R2. `MemoryOrchestrator.init()` becomes a one-line delegate: `return await self._bootstrapper.init()`. The bootstrapper's `init()` runs the same 11-step sequence with identical behavior. (origin: plan 010)
- R3. Helper methods (`_init_cognition`, `_init_alpha`, `_init_skill_engine`, `_create_default_embedder`, `_startup_maintenance`, `_check_and_reembed`) move to the bootstrapper. All `self._X` references become `self._orch._X`. (origin: plan 010)
- R4. `SubsystemBootstrapper` follows Google Python Style: docstrings on every public method, full type hints, `[SubsystemBootstrapper]` logger prefixes.
- R5. All existing tests pass without modification. Only NEW tests for the bootstrapper surface are added.

---

## Scope Boundaries

- This is a MOVE, not a REWRITE. Method bodies land verbatim with only `self._X` → `self._orch._X` renames.
- No changes to HTTP routes, admin routes, MCP plugin, or any existing test file.
- `__init__` (constructor) stays on the orchestrator — it initializes attribute defaults only, no subsystem wiring. Only `init()` (the async boot method) moves.
- `_DeriveTask` dataclass stays in `orchestrator.py` — it's a module-level class shared by write-path code.
- `_build_probe_filter` and `_build_search_filter` stay on the orchestrator — they're retrieval-time helpers, not boot-time helpers.
- The lazy `@property _bootstrapper` on the orchestrator mirrors the established pattern from Phases 1-4.

### Deferred to Follow-Up Work

- Phase 6: Style sweep + facade hardening across all remaining orchestrator methods
- Phase 7: ContextManager recomposition refactor (independent, can run in parallel)
- Phase 8 (optional): Benchmark adapter consolidation

---

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/lifecycle/background_tasks.py` — most recent lifecycle-tier extraction: same `self._orch` back-reference, same `getattr` guard lazy property pattern
- `src/opencortex/orchestrator.py` lines 339-463 (`init()`), 465-637 (`_init_cognition`, `_init_alpha`, `_init_skill_engine`), 639-680 (`_create_default_embedder`), 892-936 (`_check_and_reembed`), 937-968 (`_startup_maintenance`) — the target methods
- `src/opencortex/orchestrator.py` lines 2084-2151 — lazy property pattern to mirror for `_bootstrapper`
- `src/opencortex/lifecycle/__init__.py` — already mentions Phase 5 will add `SubsystemBootstrapper`

### Method Inventory

| Method | Lines | Role | Self-referencing calls after move |
|--------|-------|------|-----------------------------------|
| `init` | ~125 | 11-step boot sequence | `self._orch._storage`, `self._orch._embedder`, `self._orch._config`, `self._orch._fs`, `self._orch._analyzer`, `self._orch._user`, `self._create_default_embedder()` (same-service), `self._init_cognition()` (same-service), `self._init_alpha()` (same-service), `self._init_skill_engine()` (same-service), `self._startup_maintenance()` (same-service), `self._orch._get_collection()`, `self._orch._background_task_manager._start_*()` |
| `_init_cognition` | ~23 | Cognition component wiring | `self._orch._storage`, `self._orch._cognitive_state_store`, etc. |
| `_init_alpha` | ~65 | Alpha pipeline wiring | `self._orch._storage`, `self._orch._embedder`, `self._orch._llm_completion`, `self._orch._config`, `self._orch._observer`, etc. |
| `_init_skill_engine` | ~82 | Skill engine wiring | `self._orch._storage`, `self._orch._embedder`, `self._orch._llm_completion`, `self._orch._config`, `self._orch._trace_store`, `self._orch._skill_*` |
| `_create_default_embedder` | ~40 | Embedder factory | `self._orch._config` |
| `_startup_maintenance` | ~32 | Post-init background tasks | `self._orch._storage`, `self._orch._fs`, `self._orch._get_collection()`, `self._check_and_reembed()` (same-service), `self._orch._background_task_manager._recover_pending_derives()` |
| `_check_and_reembed` | ~45 | Model-change re-embed | `self._orch._embedder`, `self._orch._config`, `self._orch._storage`, `self._orch._get_collection()` |

### Intra-service call analysis

After move, same-service calls stay as `self.method()`:
- `init()` → `self._create_default_embedder()`, `self._init_cognition()`, `self._init_alpha()`, `self._init_skill_engine()`, `self._startup_maintenance()`
- `startup_maintenance()` → `self._check_and_reembed()`

Cross-service calls go through `self._orch._background_task_manager._X()`:
- `init()` → `self._orch._background_task_manager._start_derive_worker()`, `self._orch._background_task_manager._start_autophagy_sweeper()`, `self._orch._background_task_manager._start_connection_sweeper()`
- `startup_maintenance()` → `await self._orch._background_task_manager._recover_pending_derives()` (via delegate on orch)

---

## Key Technical Decisions

- **`src/opencortex/lifecycle/` namespace**: Bootstrapper is a lifecycle concern (subsystem creation + wiring). Same namespace as `BackgroundTaskManager`.
- **`_startup_maintenance` moves with bootstrapper**: It's exclusively called from `init()` (fire-and-forget `asyncio.create_task`). Its helper `_check_and_reembed` also moves (only called from `_startup_maintenance`).
- **`_build_probe_filter` stays on orchestrator**: Called at retrieval time, not boot time. Wrong service boundary.
- **`init()` sets `self._orch._initialized = True`**: The `_initialized` flag remains on the orchestrator. The bootstrapper sets it at the end of the boot sequence.
- **Logger name**: `opencortex.lifecycle.bootstrapper` — follows the `opencortex.lifecycle.background_tasks` precedent.
- **Lazy property with `getattr` guard**: Mirrors Phases 1-4. Tests that bypass `__init__` via `__new__` get a working bootstrapper instance on first access.

---

## Implementation Units

### U1. Create `SubsystemBootstrapper` shell + lazy property

**Goal:** Land the module, class, and lazy property before moving any real code.

**Requirements:** R1, R4.

**Dependencies:** None.

**Files:**
- Create: `src/opencortex/lifecycle/bootstrapper.py` (class skeleton + module docstring)
- Modify: `src/opencortex/orchestrator.py` (add `_bootstrapper_instance` field + lazy `@property _bootstrapper`)
- Modify: `src/opencortex/lifecycle/__init__.py` (update docstring)
- Create: `tests/test_subsystem_bootstrapper.py` (construction smoke tests)

**Approach:**
- `SubsystemBootstrapper` class with `__init__(self, orchestrator)` storing `self._orch = orchestrator`.
- Module docstring explains WHY: decomposition Phase 5, owns the 11-step boot sequence.
- Add `self._bootstrapper_instance: Optional[Any] = None` to `MemoryOrchestrator.__init__`.
- Add lazy `@property _bootstrapper` with `getattr` guard pattern.
- Update `lifecycle/__init__.py` docstring to mention `SubsystemBootstrapper`.
- Smoke tests: construction with mock orchestrator, lazy property caching.

**Patterns to follow:**
- `src/opencortex/lifecycle/background_tasks.py` constructor shape.
- `src/opencortex/orchestrator.py` lines 2137-2151 lazy `_background_task_manager` property.

**Test scenarios:**
- *Happy path*: `SubsystemBootstrapper(MagicMock())` constructs without error. `bs._orch is mock` is true.
- *Lazy property*: `MemoryOrchestrator.__new__()` → `orch._bootstrapper` returns a `SubsystemBootstrapper` instance.
- *Lazy property caches*: Two accesses return the same instance.
- *Back-reference*: `orch._bootstrapper._orch is orch`.

**Verification:**
- `tests/test_subsystem_bootstrapper.py` collects and passes.
- `from opencortex.lifecycle.bootstrapper import SubsystemBootstrapper` works.

---

### U2. Move `init()` and all boot helper methods

**Goal:** Move the complete boot sequence into `SubsystemBootstrapper`, replace orchestrator's `init()` with a one-line delegate.

**Requirements:** R1, R2, R3, R5.

**Dependencies:** U1.

**Files:**
- Modify: `src/opencortex/lifecycle/bootstrapper.py` (add `init`, `_init_cognition`, `_init_alpha`, `_init_skill_engine`, `_create_default_embedder`, `_startup_maintenance`, `_check_and_reembed` methods)
- Modify: `src/opencortex/orchestrator.py` (remove method bodies, replace `init()` with delegate, remove helper methods)

**Approach:**
- Move each method body verbatim from `orchestrator.py` to `bootstrapper.py`.
- Rename `self._X` → `self._orch._X` for all orchestrator-level attributes. Same-service calls (e.g., `self._init_alpha()`) stay as `self.method()`.
- Cross-service calls: `self._start_derive_worker()` → `self._orch._background_task_manager._start_derive_worker()` (these are already delegates on the orchestrator that forward to the manager; calling through the orchestrator delegate or directly through the manager are both valid — use the delegate `self._orch._start_derive_worker()` for consistency with the existing `init()` behavior).
- Logger prefix change: `[MemoryOrchestrator]` → `[SubsystemBootstrapper]` in the moved methods.
- Orchestrator's `init()` becomes: `return await self._bootstrapper.init()`.
- Add Google-style docstrings to all moved methods.
- Add `from __future__ import annotations` at module top.
- Keep `TYPE_CHECKING` imports for type hints on orchestrator reference.

**Patterns to follow:**
- `src/opencortex/lifecycle/background_tasks.py` — same MOVE pattern with `self._orch._X` renames.
- `src/opencortex/services/system_status_service.py` — Google-style docstrings on every method.

**Rename checklist** (from `self._X` to `self._orch._X`):
- `_config`, `_storage`, `_embedder`, `_rerank_config`, `_llm_completion`
- `_user`, `_fs`, `_analyzer`, `_initialized`
- `_observer`, `_trace_store`, `_trace_splitter`, `_knowledge_store`, `_archivist`
- `_context_manager`, `_parser_registry`, `_memory_probe`
- `_cognitive_state_store`, `_candidate_store`, `_recall_mutation_engine`
- `_consolidation_gate`, `_cognitive_metabolism_controller`, `_autophagy_kernel`
- `_skill_manager`, `_skill_event_store`, `_skill_evaluator`
- `_entity_index`, `_cone_scorer`, `_recall_planner`, `_memory_runtime`
- `_rerank_client`, `_insights_llm_completion`
- `_connection_sweep_task`, `_connection_sweep_guard`, `_last_connection_sweep_at`, `_last_connection_sweep_status`
- `_derive_queue`, `_derive_worker_task`, `_inflight_derive_uris`
- `_immediate_fallback_embedder`, `_immediate_fallback_embedder_attempted`
- `_autophagy_sweep_task`, `_autophagy_startup_sweep_task`, `_autophagy_sweep_cursors`, `_autophagy_sweep_guard`

**Note on cross-service calls within init():**
The current `init()` calls `self._start_derive_worker()`, `self._start_autophagy_sweeper()`, `self._start_connection_sweeper()` — these are delegates on the orchestrator that forward to `self._background_task_manager._start_X()`. In the bootstrapper, these become `self._orch._start_derive_worker()` etc. (calling through the delegate). This preserves the existing call chain.

Similarly, `_startup_maintenance()` calls `await self._recover_pending_derives()` — in the bootstrapper this becomes `await self._orch._recover_pending_derives()` (also a delegate).

**Test scenarios:**
- *Integration*: All existing tests in `tests/test_e2e_phase1.py`, `tests/test_perf_fixes.py`, `tests/test_connection_sweeper.py`, `tests/test_background_task_manager.py` pass unchanged.
- *Service-level*: New test in `tests/test_subsystem_bootstrapper.py`:
  - `init()` with mock orchestrator (no real I/O) returns the orchestrator.
  - `init()` sets `_initialized = True`.
  - `_create_default_embedder()` with config mock returns embedder or None.
  - Docstring presence on all public methods.

**Verification:**
- `init()`, `_init_cognition()`, `_init_alpha()`, `_init_skill_engine()`, `_create_default_embedder()`, `_startup_maintenance()`, `_check_and_reembed()` physically live in `bootstrapper.py`, not `orchestrator.py`.
- Orchestrator's `init()` is a one-line delegate: `return await self._bootstrapper.init()`.
- Wider regression sweep shows the same pass/fail count as pre-extraction.

---

## System-Wide Impact

- **External callers**: No change. HTTP server lifespan calls `await orch.init()` — same interface.
- **Test fixtures**: Tests that call `orch.init()` exercise the delegate path, which calls through to the bootstrapper. No test changes needed.
- **`__new__` bypass tests**: The lazy property with `getattr` guard handles this — same defense as Phases 1-4.
- **Error propagation**: The bootstrapper raises the same exceptions the original `init()` raised. The delegate does not catch.
- **State lifecycle**: All subsystem attributes remain on the orchestrator. The bootstrapper writes them via `self._orch._X = ...`.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Attribute rename typo (`self._X` → `self._orch._X`) causes subtle bugs | Every attribute access in moved methods must be audited. The existing test suite (200+ tests) covers the boot sequence indirectly — any missed rename triggers test failures. |
| `__new__` bypass tests crash on missing `_bootstrapper_instance` | Same defense as Phases 1-4: lazy property with `getattr(self, "_bootstrapper_instance", None)` guard. |
| Import cycle between bootstrapper and lifecycle/background_tasks | Both use `TYPE_CHECKING` guard and runtime imports are deferred. Bootstrapper imports `BackgroundTaskManager` only inside `init()` via `self._orch._background_task_manager` (lazy property) — no direct import needed. |
| Logger name change breaks `assertLogs` in existing tests | Existing tests use `assertLogs("opencortex.orchestrator")` or `assertLogs("opencortex.lifecycle.background_tasks")`. The bootstrapper uses `opencortex.lifecycle.bootstrapper`. Search existing tests for any that assert on init-time logs — none found (init tests mock rather than assert logs). |

---

## Sources & References

- `src/opencortex/orchestrator.py` (target of the refactor)
- `src/opencortex/lifecycle/background_tasks.py` (pattern reference — most recent lifecycle-tier extraction)
- `src/opencortex/services/system_status_service.py` (pattern reference — Google-style docstrings)
- `docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md` (master decomposition plan)
- `docs/plans/2026-04-26-014-refactor-background-task-manager-plan.md` (Phase 3 plan — closest template)
