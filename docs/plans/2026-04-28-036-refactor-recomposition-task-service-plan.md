---
title: "refactor: Extract recomposition task lifecycle service"
type: refactor
status: completed
date: 2026-04-28
origin: user request
---

# refactor: Extract recomposition task lifecycle service

## Overview

Move recomposition background task lifecycle state out of `ContextManager` and
`SessionRecompositionEngine` into `ContextRecompositionTaskService`. The new
service should own merge/full-recompose/follow-up task registries, failure
collection, pending-task tracking, session task cleanup, and close-time draining.

`SessionRecompositionEngine` should keep merge-buffer, snapshot/restore,
full-session recomposition, session summary, and recomposition write logic. It
should no longer own the task registry mechanics.

## Problem Frame

After the segmentation extraction, `SessionRecompositionEngine` is still over
1,300 lines and mixes core recomposition execution with background task
bookkeeping. `ContextManager` also still owns the task dictionaries:
`_session_merge_tasks`, `_session_merge_task_failures`,
`_session_merge_followup_tasks`, `_session_merge_followup_failures`,
`_session_full_recompose_tasks`, and `_pending_tasks`.

That coupling leaks into the main lifecycle services: `CommitService` writes
`_pending_tasks` and `_session_merge_locks` directly, while `EndService` reads
`_session_full_recompose_tasks` directly after spawning full recomposition.
This makes the context main path harder to scan and keeps task lifecycle state
spread across three classes.

## Requirements Trace

- R1. Add `ContextRecompositionTaskService` as the owner of recomposition task
  lifecycle state.
- R2. Move merge task spawn/wait/failure collection, full-recompose task
  spawn/wait/cancel cleanup, merge follow-up tracking/wait/failure collection,
  pending task tracking, and session task cleanup into the service.
- R3. `ContextManager` no longer directly stores recomposition task dicts or the
  global pending task set.
- R4. `CommitService` and `EndService` coordinate through the task service
  instead of direct manager task dictionaries.
- R5. Keep `ContextManager` compatibility wrappers for current private tests and
  callers.
- R6. Do not move merge-buffer/snapshot/full recomposition algorithms in this
  PR.
- R7. Preserve existing behavior: one merge task per session, one full
  recomposition task per session, follow-up failure propagation, fail-fast
  behavior, close-time awaiting, and cleanup semantics.

## Scope Boundaries

- Do not extract `ConversationBuffer` ownership in this PR. Merge locks protect
  buffer snapshot/restore and may move with the task service only as the lock
  registry, not the buffer data.
- Do not change recomposition scheduling thresholds, buffer flush behavior,
  full recomposition timeouts, derived record writes, or session summary logic.
- Do not remove `ContextManager._spawn_merge_task`,
  `_spawn_full_recompose_task`, `_wait_for_merge_task`, or
  `_wait_for_merge_followup_tasks`; keep them as thin wrappers.
- Do not introduce an abstract task framework. Prefer a concrete composed
  service with small methods and typed callables.

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/context/manager.py` currently initializes recomposition task
  dicts and `_pending_tasks`, drains `_pending_tasks` in `close()`, and pops
  task state in `_cleanup_session()`.
- `src/opencortex/context/recomposition_engine.py` currently implements
  `_spawn_merge_task`, `_spawn_full_recompose_task`,
  `_wait_for_merge_task`, `_track_session_merge_followup_task`, and
  `_wait_for_merge_followup_tasks`.
- `src/opencortex/context/commit_service.py` schedules cited reward tasks by
  writing `manager._pending_tasks`, and uses `manager._session_merge_locks` for
  buffer append locking.
- `src/opencortex/context/end_service.py` waits for merge/follow-up tasks via
  manager wrappers, but reads `manager._session_full_recompose_tasks` directly
  for full recomposition wait/cancel.
- `tests/test_context_manager.py` asserts current private task dict behavior and
  needs boundary-preserving updates.

### Institutional Learnings

- Recent context refactors have kept behavior stable by moving ownership behind
  composed services while keeping manager compatibility wrappers.
- Session keys are collection-scoped tuples:
  `(collection, tenant_id, user_id, session_id)`. The task service must keep
  that key shape intact.

### External References

- Not used. This is an internal Python lifecycle refactor using existing local
  service-composition patterns.

## Key Technical Decisions

- Create `src/opencortex/context/recomposition_tasks.py` with
  `ContextRecompositionTaskService`.
- The task service owns:
  - `session_merge_locks`
  - `session_merge_tasks`
  - `session_merge_task_failures`
  - `session_merge_followup_tasks`
  - `session_merge_followup_failures`
  - `session_full_recompose_tasks`
  - `pending_tasks`
- Pass async callables into task-spawn methods so the service manages lifecycle
  without importing or owning recomposition algorithms.
- Add `wait_for_full_recompose_task(...)` on the task service so `EndService`
  stops reading task dictionaries directly.
- Add `track_pending_task(...)` for cited reward tasks and any fire-and-forget
  work that should be drained on close.
- Keep manager wrapper properties/methods only where needed for compatibility,
  but route them to the task service.

## Open Questions

### Resolved During Planning

- Should merge locks move too? Yes. They are task-lifecycle coordination state
  and are currently one of the direct leaks into `CommitService` and
  `RecompositionEngine`.
- Should conversation buffers move? No. That would expand scope into data
  lifecycle and merge-buffer behavior.
- Should full recomposition wait/cancel move? Yes. Otherwise `EndService` still
  depends on the task registry internals.

### Deferred to Implementation

- Whether tests should assert via manager compatibility accessors or directly
  against the task service. Prefer direct service tests for lifecycle behavior
  and only minimal wrapper assertions for manager compatibility.

## Implementation Units

- U1. **Introduce task lifecycle service**

**Goal:** Add a service that owns task registries, pending tracking, lock access,
cleanup, and close-time drain.

**Requirements:** R1, R2, R3, R7

**Dependencies:** None

**Files:**
- Create: `src/opencortex/context/recomposition_tasks.py`
- Modify: `src/opencortex/context/manager.py`
- Test: `tests/test_recomposition_tasks.py`

**Approach:**
- Implement `ContextRecompositionTaskService` as a composed service taking the
  manager only for current request context and logging-compatible behavior.
- Add `merge_lock(sk)`, `track_pending_task(task)`, `close()`, and
  `cleanup_session(sk)`.
- Move failure deduplication into the service.
- Update `ContextManager.__init__`, `close()`, and `_cleanup_session()` to call
  the task service.

**Patterns to follow:** Existing composed services
`ContextCommitService`, `ContextEndService`, and
`RecompositionSegmentationService`.

**Test scenarios:**
- Pending tasks are drained and cleared on close.
- Session cleanup removes merge/full/follow-up task state.
- Failure deduplication returns one instance when callbacks and gather observe
  the same exception.

**Verification:** Direct task-service tests pass and `ContextManager` no longer
initializes recomposition task dictionaries directly.

- U2. **Move spawn/wait lifecycle out of recomposition engine**

**Goal:** Make `SessionRecompositionEngine` provide recomposition work callables,
while the task service owns spawn/wait/failure mechanics.

**Requirements:** R2, R5, R6, R7

**Dependencies:** U1

**Files:**
- Modify: `src/opencortex/context/recomposition_engine.py`
- Modify: `src/opencortex/context/manager.py`
- Test: `tests/test_recomposition_engine.py`
- Test: `tests/test_context_manager.py`

**Approach:**
- Replace engine task lifecycle methods with thin delegation to
  `manager._recomposition_tasks`.
- Task service creates merge tasks using the engine's `_merge_buffer(...)`
  coroutine callable.
- Task service creates full-recompose tasks using the engine's
  `_run_full_session_recomposition(...)` coroutine callable.
- Keep `_track_session_merge_followup_task` available through the engine or
  manager wrapper for the current merge-buffer implementation, but route it to
  the task service.

**Patterns to follow:** The existing manager compatibility wrapper style used
for recomposition segmentation.

**Test scenarios:**
- Starting a merge twice while the first task is active keeps one task.
- Merge task failures surface through `_wait_for_merge_task`.
- Follow-up task failures surface through `_wait_for_merge_followup_tasks`.

**Verification:** Existing context manager task tests continue to pass after
being updated to inspect the task service boundary.

- U3. **Route CommitService and EndService through task service**

**Goal:** Remove direct task-dict access from lifecycle services.

**Requirements:** R3, R4, R7

**Dependencies:** U1, U2

**Files:**
- Modify: `src/opencortex/context/commit_service.py`
- Modify: `src/opencortex/context/end_service.py`
- Test: `tests/test_context_manager.py`
- Test: `tests/test_benchmark_ingest_lifecycle.py`

**Approach:**
- Replace cited reward pending tracking with
  `manager._recomposition_tasks.track_pending_task(task)`.
- Replace direct merge lock lookup with
  `manager._recomposition_tasks.merge_lock(sk)`.
- Replace `EndService` direct lookup of
  `manager._session_full_recompose_tasks` with a service method such as
  `wait_for_full_recompose_task(sk, timeout=120.0)`.
- Keep error handling and fail-fast behavior in `EndService`; the task service
  should provide lifecycle primitives, not end-run policy.

**Patterns to follow:** Existing `EndRunState` policy handling stays in
`end_service.py`; task service owns mechanics only.

**Test scenarios:**
- End waits on the full recomposition task without accessing manager internals.
- Full recomposition timeout still cancels and records partial/failure state.
- Commit still drains cited reward tasks on close.

**Verification:** Focused context lifecycle tests pass and `rg` shows no direct
references to the moved task dictionaries outside the task service and
compatibility wrappers.

## Verification Plan

- `uv run --group dev pytest tests/test_recomposition_tasks.py tests/test_recomposition_engine.py tests/test_context_manager.py -q`
- `uv run --group dev pytest tests/test_benchmark_ingest_lifecycle.py tests/test_benchmark_ingest_service.py -q`
- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`
