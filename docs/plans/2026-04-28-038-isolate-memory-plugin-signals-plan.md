---
title: "refactor: Isolate memory plugin signals"
type: refactor
status: completed
date: 2026-04-28
origin: user request
---

# refactor: Isolate memory plugin signals

## Overview

Separate the core `store` and `recall` chains from optional `autophagy` and
`skill_engine` behavior. The memory write path should publish lifecycle signals
after durable writes succeed, and the recall path should publish recall
completion signals after memory results are assembled. Optional systems can
subscribe when explicitly enabled by configuration, but the core write/search
services must not call those optional subsystems directly.

## Problem Frame

The current implementation still mixes optional higher-level systems into hot
memory paths:

- `src/opencortex/services/memory_write_service.py` calls
  `_initialize_autophagy_owner_state` directly after memory upsert and dedup
  merge.
- `src/opencortex/services/memory_query_service.py` calls
  `_skill_manager.search` directly and appends skill hits into
  `FindResult.skills`.
- `src/opencortex/lifecycle/bootstrapper.py` initializes skill engine without
  an explicit top-level enable flag.
- `src/opencortex/http/server.py` mounts skill routes opportunistically during
  normal API startup.
- `src/opencortex/lifecycle/background_tasks.py` still owns recall autophagy
  bookkeeping helpers that are not called by the active recall path.

That makes `store` and `recall` harder to reason about. It also blurs the
boundary between core memory semantics and optional plugin behavior.

## Requirements Trace

- R1. Add a lightweight memory lifecycle signal mechanism for
  `memory_stored` and `recall_completed`.
- R2. `MemoryWriteService.add` publishes `memory_stored` asynchronously after
  a successful dedup merge or normal upsert; it must not call autophagy owner
  initialization directly.
- R3. `MemoryQueryService.search` publishes `recall_completed` asynchronously
  after core memory/resource/skill collection results are assembled; it must
  not call `skill_engine` directly.
- R4. Autophagy integrates through signal handlers only when explicitly enabled
  by config.
- R5. Skill engine integrates through explicit config only. Its HTTP routes and
  bootstrap initialization are disabled by default.
- R6. Preserve current `/api/v1/memory/store` and `/api/v1/memory/search`
  memory behavior by default, excluding plugin-provided enrichment.
- R7. Remove unused recall autophagy bookkeeping wrapper/helper residue from
  active orchestrator/background-task surfaces.
- R8. Keep standalone `skill_engine` tests and modules intact; do not delete
  the plugin implementation.

## Scope Boundaries

- Do not remove `src/opencortex/skill_engine/` or cognition/autophagy modules.
- Do not redesign memory record payloads, URI generation, anchor projection,
  dedup, probe/plan/execute, or retrieval ranking.
- Do not add synchronous plugin work to the store or recall request path.
- Do not change benchmark ingest semantics.
- Do not make skill results part of `/api/v1/memory/search` by default.
- Do not remove compatibility methods unless they are confirmed unused residue
  from recall bookkeeping.

## Current Code References

- `src/opencortex/services/memory_write_service.py`:
  direct autophagy owner initialization after dedup merge and normal upsert.
- `src/opencortex/services/memory_query_service.py`:
  direct skill manager search and `FindResult.skills` mutation.
- `src/opencortex/lifecycle/bootstrapper.py`:
  cognition init/sweeper and skill engine init.
- `src/opencortex/http/server.py`:
  skill HTTP route registration.
- `src/opencortex/lifecycle/background_tasks.py` and
  `src/opencortex/orchestrator.py`:
  recall bookkeeping wrappers and task set.
- `tests/test_perf_fixes.py` and `tests/test_background_task_manager.py`:
  current coverage for autophagy startup, recall bookkeeping, and close order.
- `tests/skill_engine/`:
  standalone skill engine coverage that should remain valid.

## Key Technical Decisions

- Introduce a small in-process signal bus under `src/opencortex/services/`
  rather than a framework-level event system. The requirement is local
  decoupling, not distributed events.
- Signal publishing is fire-and-forget from the request path. Handler failures
  are logged and do not alter store/search responses.
- Signal handler registration happens during bootstrap only when the relevant
  feature is explicitly enabled.
- Autophagy owner initialization and recall outcome application move behind
  signal handlers.
- Skill engine search is not wired into default `memory/search`. If future
  product work needs mixed recall, it should be a distinct opt-in API or
  explicit request parameter, not an implicit side effect.
- Add explicit config flags for plugin boundaries. Keep existing
  `cognition_enabled` for cognition internals, but gate autophagy signal
  integration separately so core memory can run without plugin callbacks.

## Implementation Units

- U1. **Add memory signal bus and lifecycle events**

Goal: Provide typed event payloads and async fire-and-forget dispatch.

Requirements: R1

Files:
- Create: `src/opencortex/services/memory_signals.py`
- Modify: `src/opencortex/orchestrator.py`
- Test: `tests/test_memory_signals.py`

Approach:
- Add event dataclasses for `MemoryStoredSignal` and `RecallCompletedSignal`.
- Add `MemorySignalBus` with `subscribe(event_name, handler)` and
  `publish_nowait(signal)` methods.
- Track handler tasks on the bus so orchestrator close can await/cancel them
  without using the removed recall bookkeeping task set.
- Initialize the bus in `MemoryOrchestrator.__init__`.

Test scenarios:
- Publishing with no subscribers is a no-op.
- Async subscribers receive the expected payload.
- Handler exceptions are contained and do not leak to publisher.
- `close()` drains/cancels pending signal tasks.

Verification:
- `uv run --group dev pytest tests/test_memory_signals.py -q`

- U2. **Publish core store and recall signals**

Goal: Replace direct plugin calls in store/recall hot paths with signals.

Requirements: R2, R3, R6

Files:
- Modify: `src/opencortex/services/memory_write_service.py`
- Modify: `src/opencortex/services/memory_query_service.py`
- Test: `tests/test_memory_signal_integration.py`
- Update: `tests/test_perf_fixes.py` if expectations reference old direct
  recall bookkeeping.

Approach:
- After dedup merge returns a persisted target and after normal upsert succeeds,
  publish `MemoryStoredSignal`.
- Include enough payload for plugins: URI, record ID, tenant/user/project,
  context type, category, dedup action, and source record when available.
- After `MemoryQueryService.search` finishes core aggregation and total count,
  publish `RecallCompletedSignal` with query, tenant/user, matched memories,
  resources, and skills from the core result.
- Remove direct `orch._skill_manager.search` call from memory query.

Test scenarios:
- Store publishes one signal after normal upsert.
- Dedup merge publishes one signal for the existing target.
- Search publishes one recall signal and returns memory results unchanged.
- A configured `_skill_manager` does not alter default memory search output.

Verification:
- `uv run --group dev pytest tests/test_memory_signal_integration.py tests/test_perf_fixes.py -q`

- U3. **Register autophagy as an explicit signal plugin**

Goal: Move autophagy store/recall side effects behind configured signal
handlers.

Requirements: R4, R7

Files:
- Modify: `src/opencortex/config.py`
- Modify: `src/opencortex/lifecycle/bootstrapper.py`
- Modify: `src/opencortex/lifecycle/background_tasks.py`
- Modify: `src/opencortex/orchestrator.py`
- Test: `tests/test_perf_fixes.py`
- Test: `tests/test_background_task_manager.py`

Approach:
- Add an explicit `autophagy_plugin_enabled` config flag. Default it off unless
  compatibility tests require a transition default; prefer off per user request.
- Register `memory_stored` and `recall_completed` signal handlers after
  cognition/autophagy components are initialized and the flag is true.
- Handler for `memory_stored` calls autophagy owner initialization.
- Handler for `recall_completed` resolves memory owner IDs and calls
  `apply_recall_outcome`.
- Remove unused recall bookkeeping wrapper/helper methods from
  `BackgroundTaskManager` and `MemoryOrchestrator`.

Test scenarios:
- Default config does not register autophagy signal handlers.
- Enabling the plugin registers handlers and invokes the kernel when signals
  are published.
- Removed recall bookkeeping wrappers are no longer expected in surface tests.
- `close()` still handles autophagy sweeper tasks and signal bus tasks.

Verification:
- `uv run --group dev pytest tests/test_perf_fixes.py tests/test_background_task_manager.py tests/test_orchestrator_close.py -q`

- U4. **Gate skill engine bootstrap and routes**

Goal: Keep skill engine as a standalone optional plugin that does not enrich
  memory recall by default.

Requirements: R5, R6, R8

Files:
- Modify: `src/opencortex/config.py`
- Modify: `src/opencortex/lifecycle/bootstrapper.py`
- Modify: `src/opencortex/http/server.py`
- Test: `tests/test_subsystem_bootstrapper.py`
- Test: `tests/skill_engine/test_skill_manager.py`
- Test: `tests/test_http_server.py`

Approach:
- Add `skill_engine_enabled` config flag, default `False`.
- Skip `_init_skill_engine` unless the flag is true.
- Only mount skill routes when `skill_engine_enabled` is true.
- Keep the `skill_engine` package and direct unit tests intact.

Test scenarios:
- Default bootstrap does not initialize skill engine.
- Enabled bootstrap initializes skill manager when dependencies exist.
- Default HTTP app does not mount skill routes.
- Existing standalone skill engine tests still pass.

Verification:
- `uv run --group dev pytest tests/test_subsystem_bootstrapper.py tests/skill_engine/test_skill_manager.py tests/test_http_server.py -q`

## Verification Plan

- `uv run --group dev pytest tests/test_memory_signals.py tests/test_memory_signal_integration.py -q`
- `uv run --group dev pytest tests/test_perf_fixes.py tests/test_background_task_manager.py tests/test_orchestrator_close.py -q`
- `uv run --group dev pytest tests/test_subsystem_bootstrapper.py tests/skill_engine/test_skill_manager.py tests/test_http_server.py -q`
- `uv run --group dev pytest tests/test_context_manager.py tests/test_recall_planner.py tests/test_eval_contract.py -q`
- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`
