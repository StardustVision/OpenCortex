---
title: Refactor Session Trace Lifecycle Logic Into SessionLifecycleService
created: 2026-04-28
status: completed
type: refactor
origin: "$lfg 抽离 MemoryOrchestrator 的 session/trace lifecycle 逻辑"
---

# Refactor Session Trace Lifecycle Logic Into SessionLifecycleService

## Problem Frame

`MemoryOrchestrator` has already delegated write, derive, retrieval, knowledge,
system-status, background-task, and bootstrap domains, but it still owns the
session/trace lifecycle surface directly. The remaining lifecycle methods mix
Observer session recording, immediate-message persistence, trace splitting,
autophagy owner bootstrap, access-stat updates, benchmark ingest delegation, and
skill/archivist triggers in the top-level orchestrator.

This phase is a behavior-preserving extraction. The goal is to make the
orchestrator a thin facade for lifecycle calls while moving session/trace domain
logic into a dedicated service.

## Requirements

- R1: Add `src/opencortex/services/session_lifecycle_service.py` as the owner
  for session/trace lifecycle behavior currently implemented in
  `src/opencortex/orchestrator.py`.
- R2: Keep existing `MemoryOrchestrator` method names as compatibility wrappers,
  including private helpers used by `ContextManager`, services, and tests.
- R3: Move immediate-message write behavior behind the new service while
  preserving the `_write_immediate(...)` wrapper and current record shape.
- R4: Move trace lifecycle helpers into the service:
  `_initialize_autophagy_owner_state`, `_on_trace_saved`,
  `_resolve_and_update_access_stats`, and `_update_access_stats_batch`.
- R5: Move session facade methods into the service:
  `session_begin`, `session_message`, `benchmark_conversation_ingest`, and
  `session_end`.
- R6: Preserve ContextManager behavior and lifecycle contracts. Do not change
  `ContextManager.prepare/commit/end`, idle close semantics, benchmark ingest
  traceability, or MCP-facing response shapes.
- R7: Preserve monkeypatch compatibility for tests that patch orchestrator
  private methods such as `_write_immediate`, `_get_record_by_uri`, and
  `_resolve_and_update_access_stats`.
- R8: Run focused lifecycle, benchmark ingest, trace, and perf tests plus style
  gates.

## Scope Boundaries

- Do not redesign session lifecycle semantics or ContextManager state handling.
- Do not change HTTP route contracts for session or benchmark ingest endpoints.
- Do not change trace splitting, TraceStore, Archivist, SkillEvaluator, or
  AutophagyKernel algorithms.
- Do not remove compatibility wrappers from `MemoryOrchestrator`.
- Do not move generic retrieval hot-path bookkeeping beyond the access-stat
  lifecycle helper names in this phase.
- Do not fix unrelated full-suite failures.

## Current Code Evidence

- `src/opencortex/orchestrator.py` implements `_write_immediate` around the
  immediate conversation event write path.
- `src/opencortex/orchestrator.py` implements trace/autophagy/access-stat helpers
  immediately before the session management section.
- `src/opencortex/orchestrator.py` implements `session_begin`,
  `session_message`, `benchmark_conversation_ingest`, `session_end`, and
  `_run_archivist` directly.
- `src/opencortex/context/manager.py` calls orchestrator lifecycle wrappers from
  commit/end paths, including `_write_immediate` and `session_end`.
- `tests/test_context_manager.py`, `tests/test_conversation_immediate.py`,
  `tests/test_perf_fixes.py`, and benchmark ingest tests patch or call these
  orchestrator names directly.

## Key Technical Decisions

- Add a lazy `MemoryOrchestrator._session_lifecycle_service` property matching
  existing service extraction patterns.
- Keep `SessionLifecycleService` as a back-reference service over orchestrator
  owned subsystems. Use explicit bridge accessors/helpers instead of broad
  `__getattr__`.
- Keep wrappers in `MemoryOrchestrator` for:
  `_write_immediate`, `_resolve_memory_owner_ids`, `_get_record_by_uri`,
  `_initialize_autophagy_owner_state`, `_on_trace_saved`,
  `_resolve_and_update_access_stats`, `_update_access_stats_batch`,
  `session_begin`, `session_message`, `benchmark_conversation_ingest`,
  `session_end`, and `_run_archivist`.
- Route ContextManager-facing behavior through orchestrator wrappers where
  monkeypatch compatibility matters. In particular, do not update
  `ContextManager` to call the service directly in this phase.
- Leave `_schedule_recall_bookkeeping` and `_recall_bookkeeping_tasks_set`
  delegated to `BackgroundTaskManager`; they are lifecycle-adjacent but already
  extracted.
- Keep `_run_archivist` as a compatibility wrapper to `KnowledgeService`; the
  new service may call `orch._knowledge_service.run_archivist(...)` directly for
  session-end trigger behavior.

## Implementation Units

### U1. Add SessionLifecycleService shell and lazy property

**Goal:** Establish the lifecycle-domain owner without behavior changes.

**Files:**
- Add: `src/opencortex/services/session_lifecycle_service.py`
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
- Add `_session_lifecycle_service_instance` in orchestrator initialization.
- Add lazy `_session_lifecycle_service` property using the existing
  service-property pattern.
- Add explicit bridge helpers for config, storage, fs, embedder, observer,
  trace splitter/store, knowledge service, skill evaluator, autophagy kernel,
  collection name, and initialization checks.

**Test Scenarios:**
- Import/startup still works with the new service module.
- `MemoryOrchestrator.__new__` test fixtures can access lifecycle wrappers
  without missing cached-service attributes.

### U2. Move immediate write and record lookup helpers

**Goal:** Move immediate session-event persistence and simple record lookup out
of the orchestrator body.

**Files:**
- Modify: `src/opencortex/services/session_lifecycle_service.py`
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
- Move `_write_immediate` implementation into the service.
- Move `_resolve_memory_owner_ids` and `_get_record_by_uri` implementations into
  the service, retaining wrappers on orchestrator.
- Preserve local fallback embedding behavior, TTL computation, abstract JSON,
  anchor projection sync, entity-index updates, and CortexFS write behavior.

**Test Scenarios:**
- `tests/test_conversation_immediate.py` validates immediate record shape,
  timeout fallback, and non-retryable error behavior.
- `tests/test_context_manager.py` still passes where it patches
  `orch._write_immediate`.
- `tests/test_benchmark_ingest_service.py` still passes lookup-count behavior.

### U3. Move trace/autophagy/access-stat helpers

**Goal:** Make trace lifecycle side effects service-owned.

**Files:**
- Modify: `src/opencortex/services/session_lifecycle_service.py`
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
- Move `_initialize_autophagy_owner_state`, `_on_trace_saved`,
  `_resolve_and_update_access_stats`, and `_update_access_stats_batch`.
- Preserve logging levels/messages unless a service label is clearer.
- Keep storage batch behavior and exception-swallowing semantics unchanged.

**Test Scenarios:**
- `tests/test_perf_fixes.py` access-stat tests still pass.
- TraceStore callback wiring through `src/opencortex/lifecycle/bootstrapper.py`
  still calls `orch._on_trace_saved`.

### U4. Move session facade methods

**Goal:** Move public lifecycle facade behavior behind orchestrator wrappers.

**Files:**
- Modify: `src/opencortex/services/session_lifecycle_service.py`
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
- Move `session_begin`, `session_message`, `benchmark_conversation_ingest`, and
  `session_end` implementations into the service.
- Preserve observer session-id normalization, request identity handling, admin
  enforcement, ContextManager benchmark ingest delegation, trace split/save
  loop, archivist trigger, skill evaluator trigger, and response shapes.
- Keep `MemoryOrchestrator._run_archivist` as a thin compatibility wrapper to
  `KnowledgeService`.

**Test Scenarios:**
- `tests/test_context_manager.py` session end paths still pass.
- `tests/test_http_server.py` benchmark conversation ingest contract tests
  still pass.
- `tests/test_mcp_qdrant.py` session lifecycle smoke still passes where
  environment allows.
- `tests/test_phase2_shrinkage.py` no-trace disabled behavior still passes.

### U5. Review, todo-resolve, browser gate, and PR

**Goal:** Finish the LFG pipeline with review and validation evidence.

**Files:**
- No planned production files beyond U1-U4.

**Approach:**
- Manually review the diff for behavior drift, service import cycles, wrapper
  coverage, and monkeypatch compatibility.
- Run review/autofix and persist any safe fixes.
- Inspect `.context/compound-engineering/todos/` and resolve only ready todos.
- Run browser gate in pipeline mode; expected to be no-op because this refactor
  should not touch frontend code.

**Validation Commands:**
- `uv run --group dev pytest tests/test_conversation_immediate.py tests/test_context_manager.py tests/test_benchmark_ingest_service.py tests/test_perf_fixes.py tests/test_trace_store.py tests/test_phase2_shrinkage.py -q`
- `uv run --group dev pytest tests/test_http_server.py::TestHTTPServer::test_04d_benchmark_conversation_ingest_preserves_traceability_contract tests/test_http_server.py::TestHTTPServer::test_04e_benchmark_conversation_ingest_direct_evidence_shape -q`
- `uv run --group dev ruff check .`
- `uv run --group dev ruff format --check .`

## Risks

| Risk | Mitigation |
|------|------------|
| ContextManager/tests patch orchestrator private lifecycle helpers | Keep every existing orchestrator name as a wrapper |
| Import cycle from new service importing orchestrator types | Use `TYPE_CHECKING` and localized runtime imports |
| Immediate write record shape drift | Move code without semantic edits and run immediate-write tests |
| Session-end side effects drift | Preserve trace split/save, archivist task, and skill evaluator trigger order |
| Refactor boundary remains too broad | Use explicit service bridges and avoid direct ContextManager-to-service calls |
