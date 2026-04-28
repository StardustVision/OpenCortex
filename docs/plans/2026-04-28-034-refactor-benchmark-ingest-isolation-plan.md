---
title: "refactor: Isolate benchmark ingest from ContextManager"
type: refactor
status: active
date: 2026-04-28
origin: docs/brainstorms/2026-04-23-benchmark-offline-conversation-ingest-requirements.md
---

# refactor: Isolate benchmark ingest from ContextManager

## Overview

Move the remaining benchmark-only ingest logic out of `ContextManager` so the
normal context lifecycle class owns commit/end orchestration only. The admin
benchmark ingest API should still behave exactly as it does today, but it should
reach a benchmark-specific service/facade directly instead of using
`ContextManager.benchmark_ingest_conversation` or benchmark private helpers on
the manager.

---

## Problem Frame

`BenchmarkConversationIngestService` exists, but the current extraction is only
partial: the service holds a back-reference to `ContextManager` and borrows
benchmark-only helpers for cleanup, `run_complete`, torn-run purge, response
export/hydration, recomposition entry construction, and direct-evidence URI
generation. This keeps benchmark infrastructure out of the runtime hot path, but
it still pollutes the main lifecycle class and tests still patch benchmark
behavior through `ContextManager`.

The user direction is to remove that compatibility burden: benchmark ingest is
independent infrastructure and should not live on the main commit/end class.

---

## Requirements Trace

- R1. Benchmark ingest remains admin-only and keeps the existing HTTP request,
  response, timeout, and error behavior.
- R2. Production context lifecycle behavior remains unchanged; `handle()` keeps
  supporting only `commit` and `end`.
- R3. `ContextManager` no longer exposes `benchmark_ingest_conversation` and no
  longer owns benchmark-only cleanup, run marker, torn-run purge, response
  export/hydrate, recomposition-entry, segment-meta, or evidence-URI helpers.
- R4. `BenchmarkConversationIngestService` no longer depends on a manager
  back-reference for benchmark-only helper behavior.
- R5. Existing LoCoMo/LongMemEval benchmark adapters and admin route tests keep
  passing without response shape changes.
- R6. Tests should assert the new service boundary directly instead of
  monkeypatching benchmark-only private methods on `ContextManager`.

---

## Scope Boundaries

- Do not change benchmark ingest product behavior, DTO field names, URI
  contracts, idempotency semantics, or cleanup semantics.
- Do not change normal `context_commit`, `context_end`, idle sweep, observer,
  trace, or recomposition behavior except for mechanical imports/collaborator
  wiring required by this extraction.
- Do not move generic recomposition engine behavior out of
  `RecompositionEngine`; benchmark service may call manager-compatible wrappers
  for shared generic behavior only if needed during this PR.
- Do not introduce abstract factories or strategy hierarchies. Prefer a plain
  service plus small dataclass/helper functions.

---

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/context/benchmark_ingest_service.py` already contains the
  public benchmark ingest orchestration but currently stores `manager` and calls
  many `manager._...` methods.
- `src/opencortex/context/manager.py` still defines `_BenchmarkRunCleanup`,
  `_mark_source_run_complete`, `_purge_torn_benchmark_run`,
  `_benchmark_segment_meta`, `_export_memory_record`,
  `_hydrate_record_contents`, `_benchmark_recomposition_entries`,
  `benchmark_ingest_conversation`, and `_benchmark_evidence_uri`.
- `src/opencortex/services/session_lifecycle_service.py` currently routes
  benchmark ingest through `_context_manager.benchmark_ingest_conversation`.
- `src/opencortex/orchestrator.py` exposes the public
  `benchmark_conversation_ingest` facade consumed by
  `src/opencortex/http/admin_routes.py`.
- `tests/test_benchmark_ingest_service.py`,
  `tests/test_benchmark_ingest_lifecycle.py`, `tests/test_http_server.py`, and
  `tests/test_context_manager.py` cover benchmark ingest and will need boundary
  adjustments.

### Institutional Learnings

- `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md`
  favors phase-native boundaries over reviving broad flat manager surfaces.
- `docs/solutions/best-practices/single-bucket-scoped-probe-2026-04-16.md`
  reinforces carrying tenant/user/source scope through queries and delete paths.

### External References

- Not used. This is an internal boundary cleanup with strong local patterns.

---

## Key Technical Decisions

- Make benchmark ingest a direct service dependency of the public session
  lifecycle/orchestrator facade, not a `ContextManager` method.
- Move `_BenchmarkRunCleanup` into benchmark ingest ownership and make
  compensation depend on the orchestrator/remove collaborator instead of the
  manager object.
- Move benchmark-only helpers into `BenchmarkConversationIngestService` first.
  If the file becomes hard to scan, split pure helpers into
  `src/opencortex/context/benchmark_support.py` during implementation.
- Keep shared generic operations such as conversation source persistence,
  recomposition segment building, full-session recomposition, summary
  generation, and subtree purge behind existing manager/recomposition wrappers
  only where they are truly shared with normal lifecycle behavior.

---

## Open Questions

### Resolved During Planning

- Should admin route call the benchmark service directly? Yes, but through the
  existing public orchestrator/session lifecycle facade so HTTP auth and DTO
  boundaries stay stable.
- Should manager keep a compatibility wrapper? No. The request explicitly
  removes `ContextManager.benchmark_ingest_conversation`.

### Deferred to Implementation

- Whether to keep all moved helper code in `benchmark_ingest_service.py` or split
  a small `benchmark_support.py`: decide after moving code and checking file
  readability.

---

## Implementation Units

- U1. **Move benchmark cleanup and helper ownership**

**Goal:** Remove benchmark-only cleanup/run-marker/export/hydrate/recomposition
helper methods from `ContextManager` and make them owned by benchmark ingest.

**Requirements:** R2, R3, R4

**Dependencies:** None

**Files:**
- Modify: `src/opencortex/context/benchmark_ingest_service.py`
- Modify: `src/opencortex/context/manager.py`
- Modify: `src/opencortex/context/recomposition_types.py`
- Test: `tests/test_benchmark_ingest_service.py`

**Approach:**
- Move `_BenchmarkRunCleanup` or an equivalent `BenchmarkRunCleanup` dataclass
  into benchmark ingest ownership.
- Move `_mark_source_run_complete`, `_purge_torn_benchmark_run`,
  `_benchmark_segment_meta`, `_export_memory_record`, `_hydrate_record_contents`,
  `_benchmark_recomposition_entries`, and `_benchmark_evidence_uri` to the
  benchmark service or support module.
- Replace service calls like `manager._benchmark_segment_meta(...)` with
  service-owned methods.
- Keep calls to shared generic wrappers only for behavior that is not
  benchmark-only.

**Patterns to follow:** Existing `SessionRecordsRepository` scope discipline and
current `BenchmarkConversationIngestService` responsibility sections.

**Test scenarios:**
- Service unit test covers idempotent hit response building with moved hydrate
  and export helpers.
- Service unit test covers direct evidence URI generation and metadata without a
  fake manager implementing benchmark helper methods.
- Service unit test covers torn-run purge with tenant/user/source scope.

**Verification:** `ContextManager` no longer contains benchmark-only helper
method definitions and service tests pass.

- U2. **Wire benchmark ingest outside ContextManager**

**Goal:** Route public benchmark ingest through a benchmark service/facade
without `ContextManager.benchmark_ingest_conversation`.

**Requirements:** R1, R3, R5

**Dependencies:** U1

**Files:**
- Modify: `src/opencortex/services/session_lifecycle_service.py`
- Modify: `src/opencortex/orchestrator.py`
- Modify: `src/opencortex/context/manager.py`
- Test: `tests/test_http_server.py`
- Test: `tests/test_conversation_mapping.py`
- Test: `tests/test_beam_bench.py`
- Test: `tests/test_locomo_bench.py`

**Approach:**
- Construct/own `BenchmarkConversationIngestService` at the facade layer that
  already exposes `benchmark_conversation_ingest`, or expose it from a narrow
  dependency that is not a manager method.
- Preserve admin enforcement in `SessionLifecycleService`.
- Remove the `ContextManager.benchmark_ingest_conversation` wrapper.
- Keep `MemoryOrchestrator.benchmark_conversation_ingest` as the public API for
  HTTP and benchmark adapters.

**Patterns to follow:** Existing `MemoryOrchestrator` public facade methods and
admin route DTO validation.

**Test scenarios:**
- Admin benchmark endpoint still returns the same valid
  `BenchmarkConversationIngestResponse` shape.
- Non-admin benchmark endpoint request still fails permission enforcement.
- Benchmark adapters still call `benchmark_conversation_ingest` through the
  orchestrator/client abstraction.

**Verification:** No source call site references
`ContextManager.benchmark_ingest_conversation`.

- U3. **Adjust boundary tests and compatibility assertions**

**Goal:** Update tests so they validate the new benchmark service boundary and
normal lifecycle isolation.

**Requirements:** R2, R3, R6

**Dependencies:** U1, U2

**Files:**
- Modify: `tests/test_benchmark_ingest_service.py`
- Modify: `tests/test_benchmark_ingest_lifecycle.py`
- Modify: `tests/test_context_manager.py`
- Modify: `tests/test_http_server.py`

**Approach:**
- Replace direct monkeypatches of manager benchmark private helpers with service
  monkeypatches or integration-level assertions.
- Move benchmark splitter tests from `test_context_manager.py` to service tests
  when they exercise benchmark-only entry construction.
- Add a focused assertion that `ContextManager` has no
  `benchmark_ingest_conversation` attribute.

**Patterns to follow:** Current benchmark service direct unit tests and HTTP
  integration tests.

**Test scenarios:**
- Existing torn-run purge lifecycle scenario still proves purge occurs through
  the service.
- Existing cancellation cleanup scenario still proves compensation occurs.
- Context manager tests remain focused on commit/end and recomposition wrappers.

**Verification:** Targeted benchmark/context tests pass and no test requires
benchmark helper methods on `ContextManager`.

- U4. **Run focused regression and style checks**

**Goal:** Prove the extraction is behavior-preserving and does not introduce
style drift.

**Requirements:** R1, R2, R5, R6

**Dependencies:** U1, U2, U3

**Files:**
- Test: `tests/test_benchmark_ingest_service.py`
- Test: `tests/test_benchmark_ingest_lifecycle.py`
- Test: `tests/test_http_server.py`
- Test: `tests/test_context_manager.py`
- Test: `tests/test_beam_bench.py`
- Test: `tests/test_locomo_bench.py`

**Approach:**
- Run focused pytest slices for benchmark ingest, HTTP contract, benchmark
  adapters, and context manager.
- Run `ruff format --check` and `ruff check` for the touched Python files or the
  repository default gate if feasible.

**Patterns to follow:** AGENTS.md default Python gates.

**Test scenarios:** Same as U1-U3; this unit is verification-only.

**Verification:** Focused tests and style checks pass, or any failure is
documented with a concrete blocker.

---

## System-Wide Impact

- Developers reading `ContextManager` should no longer see benchmark ingest
  infrastructure mixed into commit/end lifecycle code.
- Benchmark users should see no API or result-shape change.
- Operators keep the same admin-only benchmark endpoint and timeout behavior.

---

## Risks

- The service may still need a small shared collaborator for conversation source
  persistence and recomposition. Mitigation: keep only truly shared wrappers and
  remove benchmark-specific helpers from the manager first.
- Existing tests may encode the old private helper location. Mitigation: update
  tests to assert behavior through the benchmark service and public facade.
