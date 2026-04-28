---
title: Memory Retrieval Test Stability Sweep
created: 2026-04-28
status: completed
type: test
origin: "$compound-engineering:lfg 做一轮 memory/retrieval 测试稳定性专项：全量 pytest 基线、定位 flaky/慢测/warning/mock wrapper 漂移，并修复低风险问题"
---

# Memory Retrieval Test Stability Sweep

## Problem Frame

Recent memory and retrieval refactors split logic out of `MemoryOrchestrator`,
`MemoryService`, and `RetrievalService` into narrower services while preserving
compatibility wrappers. The highest-value next step is not another structural
split, but a stability pass over the test suite: establish a full pytest
baseline, identify memory/retrieval-specific slow tests, warnings, async
lifecycle issues, and mock patch paths that drifted after wrapper extraction,
then apply only low-risk fixes.

## Requirements

- R1: Run a full pytest baseline for the current branch and capture failures,
  warnings, and slow tests.
- R2: Focus investigation on memory/retrieval/session/context tests and wrapper
  patch points around `MemoryOrchestrator`, `RetrievalService`, and extracted
  helper services.
- R3: Fix low-risk test instability only: stale mock targets, fragile sleeps,
  missing cleanup, noisy warnings from test-controlled construction, or
  assertions that should bind to the current service boundary.
- R4: Do not change memory or retrieval product behavior unless a test failure
  exposes a small, confirmed bug with a focused regression.
- R5: Do not continue broad service extraction in this phase.
- R6: Record the observed baseline and validation commands in this plan before
  shipping.

## Scope Boundaries

- Do not split `MemoryWriteService`, `RetrievalService`, or
  `MemoryOrchestrator`.
- Do not rewrite large test files for style-only reasons.
- Do not silence warnings globally unless the warning is understood and
  intentionally test-owned.
- Do not add sleeps as a default stabilization mechanism; prefer events,
  explicit awaits, or deterministic mocks.
- Do not touch frontend code.

## Current Code Evidence

- `src/opencortex/services/retrieval_service.py` delegates candidate projection
  helpers to `src/opencortex/services/retrieval_candidate_service.py` while
  keeping wrapper method names.
- `src/opencortex/orchestrator.py` still exposes compatibility wrappers such as
  `_execute_object_query`, `_aggregate_results`, `_score_object_record`, and
  `_records_to_matched_contexts`.
- Existing wrapper patch usage appears in `tests/test_perf_fixes.py` and
  `tests/test_context_manager.py`.
- Memory/retrieval coverage is spread across `tests/test_memory_service.py`,
  `tests/test_e2e_phase1.py`, `tests/test_context_manager.py`,
  `tests/test_perf_fixes.py`, `tests/test_recall_planner.py`,
  `tests/test_object_rerank.py`, `tests/test_object_cone.py`, and
  `tests/test_retrieval_candidate_service.py`.

## Key Technical Decisions

- Start with characterization, not refactor: run the suite first and let the
  actual failures/warnings determine the patch.
- Treat compatibility wrappers as public test seams for now. Update a mock path
  only when the test is clearly asserting the extracted service boundary rather
  than the legacy orchestrator facade.
- Prefer targeted tests after each fix, then rerun the full baseline or the
  largest relevant slice.
- If full pytest is too slow for iterative debugging, use the first full run as
  the baseline and focused slices for fix verification, then repeat the affected
  broad slice before commit.

## Implementation Units

### U1. Establish Full Pytest Baseline

**Goal:** Capture the current stability picture before changing tests.

**Files:**
- Read: `tests/`
- Update: this plan's Observed Results section after execution

**Approach:**
- Run `uv run --group dev pytest`.
- Capture failing tests, warning categories, and slowest memory/retrieval-related
  tests from pytest output.
- If the full run is interrupted by a deterministic failure, reproduce that
  failure with the smallest focused command before patching.

**Test Scenarios:**
- Full suite command completes or produces a reproducible failure report.
- Baseline notes distinguish behavior failures from warning/noise issues.

### U2. Patch Low-Risk Stability Issues

**Goal:** Fix confirmed low-risk instability without changing runtime behavior.

**Files:**
- Modify only files implicated by U1, expected candidates:
  - `tests/test_perf_fixes.py`
  - `tests/test_context_manager.py`
  - `tests/test_memory_service.py`
  - `tests/test_e2e_phase1.py`
  - service tests near retrieval wrapper boundaries

**Approach:**
- Replace stale wrapper patches with the current intended boundary when needed.
- Replace fragile timing with deterministic synchronization where practical.
- Ensure async tasks and orchestrators created in tests are closed or cancelled.
- Narrow warning filters to test-owned warnings only if a warning cannot be
  eliminated at construction.

**Test Scenarios:**
- Each changed test file passes independently.
- Relevant neighboring memory/retrieval slices pass.
- No broad warning filter hides unrelated runtime problems.

### U3. Verification, Review, Browser Gate, and PR

**Goal:** Complete the LFG pipeline with durable evidence.

**Validation Commands:**
- `uv run --group dev pytest`
- `uv run --group dev pytest tests/test_perf_fixes.py tests/test_context_manager.py -q`
- `uv run --group dev pytest tests/test_memory_service.py tests/test_e2e_phase1.py -q`
- `uv run --group dev pytest tests/test_object_rerank.py tests/test_object_cone.py tests/test_retrieval_candidate_service.py -q`
- `uv run --group dev ruff check .`
- `uv run --group dev ruff format --check .`

## Risks

| Risk | Mitigation |
|------|------------|
| Full suite takes too long | Keep first run as baseline and use focused slices for iteration |
| Warning fix hides real issue | Prefer construction cleanup; use narrow filters only for understood third-party warnings |
| Mock path update breaks compatibility coverage | Keep orchestrator wrapper tests when they intentionally protect legacy facade |
| Stability sweep grows into refactor | Explicitly avoid service extraction and behavior changes |

## Observed Results

### Baseline

- Initial full suite: `uv run --group dev pytest`
  - 15 failed, 1263 passed, 34 skipped, 5 warnings in 222.04s.
- Baseline failures:
  - `tests/insights/test_collector.py::TestInsightsCollector::test_fetch_traces_filters_by_tenant`
    returned a cross-tenant mock record because the test mock did not
    understand the current canonical `conds` filter shape.
  - `tests/test_live_servers.py::TestHTTPLive::*` ran against a locally
    reachable but incompatible server; health was unauthenticated but mutable
    endpoints returned 401 and the embedder was unavailable.
  - `tests/test_live_servers.py` also still contained stale MCP live tests even
    though `src/opencortex/mcp_server.py` no longer exists.
  - `tests/test_local_embedder.py::*` constructed `LocalEmbedder` with
    `__new__` but did not initialize the current thread-local model and
    availability fields.
- Memory/retrieval slices passed functionally, but
  `tests/test_memory_service.py tests/test_e2e_phase1.py -q` emitted pending
  `CortexFS.write_context()` task destruction messages from loop-based tests.

### Fixes Applied

- Updated the Insights collector test mock to support canonical `conds` filter
  clauses.
- Made live HTTP tests explicitly opt-in with `OPENCORTEX_RUN_LIVE_TESTS=1`,
  avoiding accidental full-suite failures when a stale or incompatible local
  server is listening.
- Removed stale MCP live tests from `tests/test_live_servers.py`.
- Updated LocalEmbedder tests to construct against the current thread-local
  model lifecycle.
- Added loop task draining in `tests/test_e2e_phase1.py` before closing custom
  event loops so fire-and-forget CortexFS writes complete deterministically.
- Removed low-risk warnings from pytest collection, `datetime.utcnow()`, and
  tests that accidentally created a real default LocalEmbedder.

### Validation Results

- `uv run --group dev pytest tests/insights/test_collector.py tests/test_local_embedder.py tests/test_live_servers.py -q`:
  passed, 14 tests and 8 live HTTP tests skipped by default.
- `uv run --group dev pytest tests/test_memory_service.py tests/test_e2e_phase1.py -q`:
  passed, 54 tests with no pending-task destruction output.
- `uv run --group dev pytest tests/test_perf_fixes.py tests/test_context_manager.py -q`:
  passed, 89 tests.
- `uv run --group dev pytest tests/test_object_rerank.py tests/test_object_cone.py tests/test_retrieval_candidate_service.py -q`:
  passed, 5 tests.
- Final full suite: `uv run --group dev pytest`
  - 1270 passed, 34 skipped, 1 warning.
- Remaining warning:
  - `tests/test_batch_add_hierarchy.py::TestBatchAddHierarchy::test_batch_creates_directory_nodes`
    hits local Qdrant's expected "Payload indexes have no effect" warning.
