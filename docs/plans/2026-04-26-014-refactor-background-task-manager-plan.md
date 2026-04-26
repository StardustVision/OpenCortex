---
title: "refactor: Extract BackgroundTaskManager from MemoryOrchestrator (Phase 3)"
type: refactor
status: active
date: 2026-04-26
origin: docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md
---

# refactor: Extract BackgroundTaskManager from MemoryOrchestrator (Phase 3)

## Overview

Phase 3 of the 8-phase God Object decomposition. Extracts all background task infrastructure (~630 lines of method bodies) from `MemoryOrchestrator` into a dedicated `BackgroundTaskManager` at `src/opencortex/lifecycle/background_tasks.py`. Covers four clusters: autophagy sweeper, connection sweeper, derive worker, and recall bookkeeping. Same MOVE-not-REWRITE discipline as Phases 1-2-4 — method bodies land verbatim, orchestrator keeps thin delegate stubs for externally-called methods, all existing tests pass unchanged.

---

## Problem Frame

Plan 010 defines the full decomposition roadmap. Phases 1-2-4 (MemoryService, KnowledgeService, SystemStatusService, PRs #16-#19) moved ~1800 lines. Phase 3 targets background lifecycle: four independent async loops (autophagy sweeper, connection pool inspector, document derive worker, recall bookkeeping dispatcher) plus their reverse-order teardown. The orchestrator is ~4606 lines post-Phase-4; this extraction removes ~630 lines of method bodies (replaced by one-line delegates or direct manager calls). (Audit Priority A2 in plan 010.)

---

## Requirements Trace

- R1. `BackgroundTaskManager` lives at `src/opencortex/lifecycle/background_tasks.py` and follows the established back-reference pattern: `BackgroundTaskManager(orchestrator)` with `self._orch` access to subsystems. (origin: plan 010 Phase 3 spec)
- R2. Every externally-called method (`_recover_pending_derives`, `_drain_derive_queue`) retains its exact name + signature + behavior on the orchestrator as a one-line delegate. Purely internal methods (sweeper loops, helper private methods) are removed from the orchestrator surface and called via the manager from `init()` and `close()`.
- R3. `BackgroundTaskManager` follows Google Python Style: docstrings on every public method, full type hints, `[BackgroundTaskManager]` logger prefixes.
- R4. All existing tests pass without modification. Only NEW tests for `BackgroundTaskManager` surface are added.

---

## Scope Boundaries

- This is a MOVE, not a REWRITE. Method bodies land verbatim with only `self._X` → `self._orch._X` renames.
- `_complete_deferred_derive` is **NOT** moved — it is called from the conversation write path (`_write_immediate`), not from a background loop. Moving it would create an awkward cross-service call from the write path into `BackgroundTaskManager`.
- `_DeriveTask` dataclass stays in `orchestrator.py` (used by both `_write_immediate` and `_process_derive_task`). `BackgroundTaskManager` imports it under `TYPE_CHECKING`. This is safe because `_process_derive_task` only accesses `_DeriveTask` instance attributes at runtime (e.g., `task.parent_uri`, `task.chunks`) — it never constructs a `_DeriveTask` or calls `isinstance(task, _DeriveTask)`. With `from __future__ import annotations` the type annotation `task: "_DeriveTask"` is never evaluated at runtime.
- `_schedule_recall_bookkeeping` has zero call sites in the current codebase (confirmed by grep). It is orphaned recall bookkeeping infrastructure that was never wired to a caller. It moves to `BackgroundTaskManager` to colocate the full recall cluster in one place; no caller needs to be added by this plan.
- No changes to HTTP routes (`server.py`), admin routes (`admin_routes.py`), or any test file.
- All task handle attributes (`_connection_sweep_task`, `_autophagy_sweep_task`, `_autophagy_startup_sweep_task`, `_derive_worker_task`, `_derive_queue`, `_inflight_derive_uris`, `_recall_bookkeeping_tasks`, etc.) remain on the orchestrator. `BackgroundTaskManager` reads/writes them via `self._orch._X`.
- Status reporting attributes (`_last_connection_sweep_at`, `_last_connection_sweep_status`) remain on the orchestrator — the admin route at `/admin/health/connections` reads them via `getattr(_orchestrator, ...)` and must not require route changes.

### Deferred to Follow-Up Work

- Phase 5: Extract `SubsystemBootstrapper` at `src/opencortex/lifecycle/bootstrapper.py` (origin: plan 010)
- Phase 6: Style sweep + facade hardening across all remaining orchestrator methods

---

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/services/system_status_service.py` — most recent validated extraction pattern: `self._orch` back-reference, `self._orch._X` subsystem access, one-line delegates via lazy `@property`
- `src/opencortex/orchestrator.py` lines 2609-2627 — lazy `_system_status_service` property to mirror for `_background_task_manager`
- `src/opencortex/http/admin_routes.py` lines ~428-434 — reads `_last_connection_sweep_at` / `_last_connection_sweep_status` via `getattr(_orchestrator, ...)` — these attributes must remain on the orchestrator
- `tests/test_document_async_derive.py` — calls `orch._drain_derive_queue()` and `orch._recover_pending_derives()` directly — both need delegate stubs on orchestrator
- `tests/test_document_mode.py` — calls `orch._drain_derive_queue()` directly

### Method Inventory

| Cluster | Method | Lines | Role | Self-referencing calls after move |
|---------|--------|-------|------|-----------------------------------|
| Autophagy | `_start_autophagy_sweeper` | ~35 | Starts tasks | `self._autophagy_sweep_loop()` (same-service) |
| Autophagy | `_run_autophagy_sweep_once` | ~37 | Sweep implementation | `self._orch._autophagy_kernel`, `self._orch._config`, `self._orch._autophagy_sweep_guard`, `self._orch._autophagy_sweep_cursors` |
| Autophagy | `_autophagy_sweep_loop` | ~11 | Periodic loop | `self._run_autophagy_sweep_once()` (same-service) |
| Connection | `_start_connection_sweeper` | ~27 | Starts task | `self._connection_sweep_loop()` (same-service) |
| Connection | `_run_connection_sweep_once` | ~66 | Sweep implementation | `self._orch._llm_completion`, `self._orch._rerank_client`, `self._orch._last_connection_sweep_at`, `self._orch._last_connection_sweep_status`, `self._maybe_warn_pool()` (same-service) |
| Connection | `_maybe_warn_pool` | ~22 | Pool warn helper | `self._orch._config` |
| Connection | `_connection_sweep_loop` | ~28 | Periodic loop | `self._run_connection_sweep_once()` (same-service) |
| Derive | `_start_derive_worker` | ~4 | Starts task | `self._orch._derive_worker_task`, `self._derive_worker()` (same-service) |
| Derive | `_derive_worker` | ~17 | Queue consumer | `self._orch._derive_queue`, `self._process_derive_task()` (same-service) |
| Derive | `_process_derive_task` | ~188 | Derive implementation | `self._orch.add()`, `self._orch.update()`, `self._orch._derive_parent_summary()`, `self._orch._fs`, `self._orch._config`, `self._orch._inflight_derive_uris` |
| Derive | `_recover_pending_derives` | ~64 | Crash recovery | `self._orch._config`, `self._orch._parser_registry`, `self._orch._inflight_derive_uris`, `self._orch._derive_queue` |
| Derive | `_drain_derive_queue` | ~3 | Test-only drain | `self._orch._derive_queue` |
| Recall | `_schedule_recall_bookkeeping` | ~27 | Task dispatcher | `self._run_recall_bookkeeping()` (same-service), `self._recall_bookkeeping_tasks_set()` (same-service) |
| Recall | `_recall_bookkeeping_tasks_set` | ~5 | Task set accessor | `self._orch._recall_bookkeeping_tasks` |
| Recall | `_run_recall_bookkeeping` | ~29 | Bookkeeping impl | `self._orch._resolve_memory_owner_ids()`, `self._orch._autophagy_kernel` |
| Teardown | `close` | ~58 | Cancel + drain all tasks | `self._orch._connection_sweep_task`, `self._orch._autophagy_sweep_task`, `self._orch._autophagy_startup_sweep_task`, `self._orch._derive_worker_task`, `self._orch._derive_queue`, `self._recall_bookkeeping_tasks_set()` (same-service) |

### Orchestrator init() changes

`init()` calls `_start_derive_worker()`, `_start_autophagy_sweeper()`, `_start_connection_sweeper()` directly. After extraction these become:

```
btm = self._background_task_manager
btm._start_derive_worker()
btm._start_autophagy_sweeper()   # still conditional on cognition_enabled
btm._start_connection_sweeper()
```

`_startup_maintenance()` calls `await self._recover_pending_derives()` — this continues to work because `_recover_pending_derives` has a one-line delegate on the orchestrator.

### Orchestrator close() changes

The first ~58 lines of `close()` (background task cancellation + derive drain) are replaced by `await self._background_task_manager.close()`. Remaining teardown (context_manager, llm_completion, storage) is unchanged.

### Institutional Learnings

- **Back-reference pattern validated** across 4 extractions (BenchmarkConversationIngestService, MemoryService, KnowledgeService, SystemStatusService). Constructor stores `self._orch`, methods access `self._orch._X`. (plans 010-013)
- **Lazy property with `getattr` guard** handles tests that bypass `__init__` via `__new__`. (plan 011 actual implementation)
- **Same-service intra-method calls** (e.g., `system_status` → `health_check` in Phase 4) stay as `self.method()` — no rename needed. (plan 013)
- **TYPE_CHECKING guard** prevents circular imports. (established in plans 011-013)

---

## Key Technical Decisions

- **`src/opencortex/lifecycle/` namespace** (not `services/`): Background sweepers and workers are lifecycle concerns, not service-tier concerns. The audit's `src/opencortex/lifecycle/background_tasks.py` target path reflects this distinction. Phase 5 (`SubsystemBootstrapper`) will land in the same namespace.
- **`_maybe_warn_pool` moves with connection sweeper**: Exclusively called by `_run_connection_sweep_once`. Moving it avoids an awkward `self._orch._background_task_manager._maybe_warn_pool(...)` call from within the sweeper. (Unlike plan 013's deferral of this method, Phase 3 is the right home.)
- **`_complete_deferred_derive` stays on orchestrator**: Called from the conversation write path (`_write_immediate`), not from a background loop. Moving it would couple the write path to `BackgroundTaskManager`.
- **`_DeriveTask` stays in orchestrator.py**: Used by both `_write_immediate` (stays on orchestrator) and `_process_derive_task` (moves). Shared module-level class; moving it would require updating import in all consumers. Imported under `TYPE_CHECKING` in `background_tasks.py`.
- **All task handle attributes remain on orchestrator**: `_connection_sweep_task`, `_autophagy_sweep_task`, `_derive_worker_task`, `_last_connection_sweep_at`, `_last_connection_sweep_status`, etc. Admin route reads these via `getattr(_orchestrator, ...)`. No route changes needed.
- **`BackgroundTaskManager.close()` owns task teardown**: The first 58 lines of `close()` (background task cancellation + derive drain) become `BackgroundTaskManager.close()`. The manager resets task attributes on the orchestrator via `self._orch._X = None` after cancellation to mirror current behavior.
- **Delegate stubs only where needed**: `_recover_pending_derives` and `_drain_derive_queue` get delegate stubs (test callers). Purely internal sweeper methods (`_start_*`, loops, helpers) do not get delegates — `init()` calls them directly via `self._background_task_manager._start_*()`.

---

## Output Structure

```
src/opencortex/lifecycle/
  __init__.py              # module docstring only, no re-exports
  background_tasks.py      # BackgroundTaskManager class

tests/
  test_background_task_manager.py   # new test file
```

---

## Implementation Units

- U1. **Create BackgroundTaskManager and move all methods**

**Goal:** Create `src/opencortex/lifecycle/background_tasks.py` with all 15 methods + `close()` moved from orchestrator. Update `init()` to route through manager. Update `close()` to delegate task teardown. Add delegate stubs for externally-called methods.

**Requirements:** R1, R2, R4

**Dependencies:** None

**Files:**
- Create: `src/opencortex/lifecycle/__init__.py`
- Create: `src/opencortex/lifecycle/background_tasks.py`
- Modify: `src/opencortex/orchestrator.py` (delegate stubs + `_background_task_manager` lazy property + `_background_task_manager_instance` init + `init()` start-method routing + `close()` delegation)
- Create: `tests/test_background_task_manager.py`

**Approach:**
1. Create `BackgroundTaskManager` class with `__init__(self, orchestrator)` storing `self._orch`
2. Move all 15 methods + teardown verbatim, applying `self._X` → `self._orch._X` renames:
   - `self._autophagy_kernel` → `self._orch._autophagy_kernel`
   - `self._autophagy_sweep_cursors` → `self._orch._autophagy_sweep_cursors`
   - `self._autophagy_sweep_guard` → `self._orch._autophagy_sweep_guard`
   - `self._autophagy_sweep_task` → `self._orch._autophagy_sweep_task`
   - `self._autophagy_startup_sweep_task` → `self._orch._autophagy_startup_sweep_task`
   - `self._connection_sweep_task` → `self._orch._connection_sweep_task`
   - `self._connection_sweep_guard` → `self._orch._connection_sweep_guard`
   - `self._last_connection_sweep_at` → `self._orch._last_connection_sweep_at`
   - `self._last_connection_sweep_status` → `self._orch._last_connection_sweep_status`
   - `self._llm_completion` → `self._orch._llm_completion`
   - `self._rerank_client` → `self._orch._rerank_client`
   - `self._config` → `self._orch._config`
   - `self._derive_worker_task` → `self._orch._derive_worker_task`
   - `self._derive_queue` → `self._orch._derive_queue`
   - `self._inflight_derive_uris` → `self._orch._inflight_derive_uris`
   - `self._parser_registry` → `self._orch._parser_registry`
   - `self._fs` → `self._orch._fs`
   - `self._recall_bookkeeping_tasks` → `self._orch._recall_bookkeeping_tasks`
   - `self._autophagy_kernel` → `self._orch._autophagy_kernel`
   - `self._connection_sweep_guard` → `self._orch._connection_sweep_guard`
   - `self._autophagy_sweep_guard` → `self._orch._autophagy_sweep_guard`
   - Same-service intra-cluster calls stay as `self.method()` (no rename)
3. Add `BackgroundTaskManager.close()` containing the current `close()` lines 4207-4264 verbatim
4. Add `self._background_task_manager_instance: Optional[Any] = None` to orchestrator `__init__`
5. Add lazy `@property _background_task_manager` on orchestrator (mirror `_system_status_service` pattern)
6. Update orchestrator `init()`: replace `self._start_derive_worker()`, `self._start_autophagy_sweeper()`, `self._start_connection_sweeper()` with `self._background_task_manager._start_derive_worker()` etc.
7. Update orchestrator `close()`: replace lines 4207-4264 with `await self._background_task_manager.close()`
8. Add delegate stubs on orchestrator for `_recover_pending_derives` and `_drain_derive_queue` only
9. Remove the 15 moved method bodies from orchestrator (private methods with no delegates)
10. Update `src/opencortex/lifecycle/__init__.py` with a module docstring explaining the lifecycle namespace (Phase 3 = BackgroundTaskManager, Phase 5 = SubsystemBootstrapper, no re-exports)

**Imports needed in BackgroundTaskManager:**
- `asyncio`, `logging`
- `typing`: `TYPE_CHECKING`, `Any`, `Dict`, `List`, `Optional`
- `contextlib.suppress`
- `TYPE_CHECKING` guard: `from opencortex.orchestrator import MemoryOrchestrator, _DeriveTask`
- `from opencortex.observability.pool_stats import extract_pool_stats, POOL_DEGRADED_THRESHOLD` (local import, same pattern as orchestrator)

**Patterns to follow:**
- `src/opencortex/services/system_status_service.py` — back-reference pattern, `self._orch._X` renames, module docstring boundary description
- Orchestrator lazy property at lines 2609-2627

**Test scenarios:**
- Happy path: `BackgroundTaskManager(mock_orch)` stores orchestrator reference
- Happy path: `BackgroundTaskManager(None)` stores None without validation
- Happy path: lazy `_background_task_manager` property on orchestrator via `__new__` bypass succeeds
- Happy path: lazy property caches service instance (two reads return same object)
- Happy path: `_recall_bookkeeping_tasks_set()` returns empty set on fresh orchestrator (no `_recall_bookkeeping_tasks` attr)
- Happy path: `_recall_bookkeeping_tasks_set()` returns existing set when attr is present
- Happy path: `_schedule_recall_bookkeeping()` returns early when no memories passed
- Happy path: `_schedule_recall_bookkeeping()` returns early when `_autophagy_kernel` is None
- Happy path: `close()` completes without error on orchestrator with all task attrs as None
- Happy path: `close()` cancels a running `_connection_sweep_task` and sets it to None
- Happy path: `close()` cancels running autophagy tasks and sets them to None
- Happy path: all public methods have non-empty docstrings
- Integration: `orch._drain_derive_queue()` delegate works (queue drains)
- Integration: `orch._recover_pending_derives()` delegate works (no markers → no-op)

**Verification:**
- `uv run python3 -m unittest discover -s tests -v` passes with zero new failures
- `uv run --group dev ruff check .` passes with zero new errors
- `BackgroundTaskManager` has `close()` + 15 methods with correct signatures
- Orchestrator has two one-line delegates (`_recover_pending_derives`, `_drain_derive_queue`)
- Orchestrator `init()` routes start calls through `self._background_task_manager`
- Orchestrator `close()` opens with `await self._background_task_manager.close()`

---

- U2. **Style polish**

**Goal:** Google-style docstrings on all public methods, `[BackgroundTaskManager]` logger prefix on any logging calls inside service methods, type annotation check.

**Requirements:** R3

**Dependencies:** U1

**Files:**
- Modify: `src/opencortex/lifecycle/background_tasks.py`

**Approach:**
1. Verify all public methods have Google-style docstrings (Args/Returns/Raises)
2. Confirm `logger = logging.getLogger(__name__)` is set and any in-method logging uses `[BackgroundTaskManager]` prefix
3. Confirm type annotations on all method signatures are complete
4. Confirm no `# noqa` or `type: ignore` are needed beyond pre-existing patterns

**Patterns to follow:**
- `src/opencortex/services/system_status_service.py` — docstring style, logger prefix format

**Test scenarios:**
- Happy path: all public methods have non-empty docstrings (smoke test from U1)

**Verification:**
- Docstring smoke tests pass
- `uv run --group dev ruff check src/opencortex/lifecycle/` passes clean

---

## System-Wide Impact

- **No API surface change**: `_recover_pending_derives` and `_drain_derive_queue` retain identical signatures on the orchestrator (one-line delegates). HTTP routes, admin routes, and all tests require zero changes.
- **State lifecycle risks**: None. `BackgroundTaskManager` writes orchestrator attributes only in `close()` (setting task handles to None) — same behavior as today.
- **Task handle ownership**: All asyncio.Task handles remain on the orchestrator. `BackgroundTaskManager` stores no task handles of its own; it reads/writes `self._orch._X_task`.
- **Connection sweep status**: `_last_connection_sweep_at` and `_last_connection_sweep_status` remain on the orchestrator; `BackgroundTaskManager._run_connection_sweep_once()` writes them via `self._orch._X`. Admin route reads continue to work unchanged.
- **`_complete_deferred_derive` race**: Stays on orchestrator; increments/decrements `self._deferred_derive_count` directly. No new concurrency surface.
- **Teardown ordering**: `BackgroundTaskManager.close()` preserves the exact cancellation order from the original `close()` — connection sweeper first, then autophagy tasks, then recall bookkeeping, then derive worker.
- **Unchanged invariants**: All public orchestrator methods (`add`, `update`, `search`, `system_status`, etc.), all HTTP routes, all MCP tools, all test fixtures — untouched.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Circular import between `BackgroundTaskManager` and orchestrator | Use `TYPE_CHECKING` guard; no module-level orchestrator import. `_DeriveTask` also under `TYPE_CHECKING`. |
| `_DeriveTask` type unavailable at runtime in `BackgroundTaskManager` | Use `from __future__ import annotations` so `_DeriveTask` annotation is a string; runtime only needs the actual object, not the class reference. |
| `_last_connection_sweep_at/status` access broken in admin route | These stay on orchestrator; BackgroundTaskManager writes to them via `self._orch._X`. Admin route `getattr` pattern is unchanged. |
| Tests calling `orch._drain_derive_queue()` break | Delegate stub on orchestrator preserves the call surface; test files need zero changes. |
| `close()` teardown ordering changes | `BackgroundTaskManager.close()` preserves exact order and guard patterns verbatim. |
| `__new__`-bypass tests crash on `_background_task_manager` access | Lazy property with `getattr` guard handles this (validated in all prior phases). |

---

## Sources & References

- **Origin document:** [docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md](docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md)
- Related: [docs/plans/2026-04-26-013-refactor-system-status-service-extraction-plan.md](docs/plans/2026-04-26-013-refactor-system-status-service-extraction-plan.md) (Phase 4, validated pattern)
- Related code: `src/opencortex/services/system_status_service.py` (validated pattern)
- Related code: `src/opencortex/services/knowledge_service.py` (validated pattern)
