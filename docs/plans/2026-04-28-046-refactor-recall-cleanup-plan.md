---
title: refactor: Clean Recall Legacy Paths
type: refactor
status: completed
date: 2026-04-28
---

# refactor: Clean Recall Legacy Paths

## Overview

Clean up the current recall implementation so `/api/v1/memory/search` has one
obvious path: HTTP request conversion, `MemoryRecallPipelineService`, and
`RetrievalService` execution. Remove or quarantine older recall/search entry
points that still construct their own typed queries, filters, or `FindResult`
outside the probe/plan/bind pipeline.

## Problem Frame

The current main recall path is already extracted into
`src/opencortex/services/memory_recall_pipeline_service.py`, but old helpers
remain in `src/opencortex/storage/cortex_fs.py` and
`src/opencortex/services/retrieval_service.py`. They make the codebase harder
to reason about because multiple methods can appear to perform semantic recall
with different filters, planning, and aggregation behavior.

## Requirements Trace

- R1. `/api/v1/memory/search` must continue to use the existing
  probe/plan/bind/retrieve pipeline and preserve its response contract.
- R2. Dead or uncalled legacy recall/search implementations must be removed
  when no active caller exists.
- R3. Any compatibility surface that remains must delegate to the current
  recall pipeline rather than maintaining independent query/filter logic.
- R4. `RetrievalService._execute_object_query` must be split before it grows
  further, keeping existing orchestrator wrappers intact for tests and callers.
- R5. Low-risk cleanup, including duplicate imports, should be included when it
  is directly adjacent to the recall cleanup.

## Scope Boundaries

- Do not change store/write behavior.
- Do not change scoring formulas, cone rerank semantics, URI path scoring, or
  probe/planner policy unless required to preserve existing behavior after the
  split.
- Do not remove `MemoryOrchestrator.search` or private wrapper methods that
  existing tests still patch.
- Do not reintroduce context `prepare`; it remains removed.
- Do not make `skill_engine` or autophagy synchronous parts of recall.

## Current Code Context

- Main HTTP recall route:
  `src/opencortex/http/server.py`
- Facades:
  `src/opencortex/orchestrator.py`,
  `src/opencortex/services/memory_service.py`,
  `src/opencortex/services/memory_query_service.py`
- Main recall orchestration:
  `src/opencortex/services/memory_recall_pipeline_service.py`
- Retrieval execution:
  `src/opencortex/services/retrieval_service.py`
- Candidate projection helpers:
  `src/opencortex/services/retrieval_candidate_service.py`
- Legacy search-like code:
  `src/opencortex/storage/cortex_fs.py`,
  `src/opencortex/retrieve/intent_analyzer.py`

Local pattern is strong and current behavior is repo-specific, so external
research is intentionally skipped.

## Implementation Units

- U1. **Remove CortexFS Recall-Like Search Methods**
  - **Goal:** Remove `CortexFS.find()` and `CortexFS.search()` if current code
    confirms they have no active callers, eliminating a duplicate semantic
    search implementation from storage.
  - **Requirements:** R1, R2
  - **Dependencies:** none
  - **Files:**
    - Modify: `src/opencortex/storage/cortex_fs.py`
    - Modify tests only if they reference removed methods:
      `tests/test_cortexfs_async.py`, `tests/test_memory_store_layers.py`,
      `tests/test_e2e_phase1.py`
  - **Approach:** Re-check caller references before editing. If no active
    callers exist, delete the methods and any imports that only supported
    them. CortexFS should stay focused on layered file persistence and helper
    filesystem behavior, not recall orchestration.
  - **Patterns to follow:** Prior MCP cleanup plans removed compatibility
    wrappers outright when the active API no longer exposed them.
  - **Test scenarios:**
    - Existing CortexFS filesystem tests still pass without `find/search`.
    - Memory store layer tests still initialize and write CortexFS data.
    - `rg "session_info|CortexFS.find|CortexFS.search"` shows no stale active
      callers after removal.
  - **Verification:** No active code imports or calls the removed methods, and
    CortexFS-focused tests pass.

- U2. **Remove Unused Session Search Recall Path**
  - **Goal:** Remove the unused `session_search` path from
    `RetrievalService` and `MemoryOrchestrator` so session recall cannot bypass
    the current pipeline.
  - **Requirements:** R1, R2, R3
  - **Dependencies:** U1
  - **Files:**
    - Modify: `src/opencortex/services/retrieval_service.py`
    - Modify: `src/opencortex/orchestrator.py`
    - Modify: `src/opencortex/retrieve/intent_analyzer.py` only if it becomes
      entirely unused by active code outside skill engine/docs.
    - Modify tests only if they assert the old wrapper surface.
  - **Approach:** Re-check `session_search(` references. If only the wrapper and
    service implementation remain, remove both. Keep `IntentAnalyzer` if still
    used by CortexFS removal fallout, bootstrap setup, or skill engine
    components; otherwise leave broader analyzer cleanup for a separate task.
  - **Patterns to follow:** `ContextManager.handle()` rejects removed prepare
    explicitly; in this case prefer removal over a new deprecated alias because
    no HTTP/API contract exposes `session_search`.
  - **Test scenarios:**
    - `rg "session_search\\(" src/opencortex tests` has no active references.
    - HTTP memory search tests still pass through the new recall pipeline.
  - **Verification:** No active session recall bypass remains, while
    `/api/v1/memory/search` behavior is unchanged.

- U3. **Split Object Query Execution Helpers**
  - **Goal:** Reduce `RetrievalService._execute_object_query` size and make its
    phases readable without changing retrieval behavior.
  - **Requirements:** R1, R4
  - **Dependencies:** U1, U2
  - **Files:**
    - Modify: `src/opencortex/services/retrieval_service.py`
    - Add or modify: `tests/test_retrieval_service.py` if a new focused test
      file is useful; otherwise extend existing retrieval tests:
      `tests/test_object_rerank.py`, `tests/test_object_cone.py`,
      `tests/test_context_manager.py`
  - **Approach:** Extract private helpers inside `RetrievalService` for
    narrowly scoped phases: kind/start/scope filter construction, parallel
    layer search, missing leaf hydration, URI path source calculation, and
    final `QueryResult` assembly. Preserve orchestrator wrapper calls where
    existing tests monkeypatch them (`_embed_retrieval_query`,
    `_score_object_record`, `_records_to_matched_contexts`).
  - **Patterns to follow:**
    - `RetrievalCandidateService` owns candidate projection/scoring helpers.
    - `MemoryRecallPipelineService` uses small private phase methods for
      probe, plan, retrieve, finalize, and signal publishing.
  - **Test scenarios:**
    - Object rerank still returns sorted final scores and match reasons.
    - Cone retrieval still expands candidates and records cone metadata.
    - Document/session scoped retrieval still applies scope filters.
    - Projection hits still hydrate missing leaf records through batch URI
      loading when ACL allows them.
  - **Verification:** Existing object retrieval tests pass and
    `_execute_object_query` is materially smaller while wrappers remain
    compatible.

- U4. **Adjacent Recall Cleanup and Regression Checks**
  - **Goal:** Clean directly adjacent small issues and lock the active recall
    path with targeted checks.
  - **Requirements:** R1, R5
  - **Dependencies:** U3
  - **Files:**
    - Modify: `src/opencortex/intent/executor.py`
    - Modify: `tests/test_memory_recall_pipeline_service.py` if needed
    - Modify: `tests/test_memory_signal_integration.py` if needed
  - **Approach:** Remove the duplicate
    `retrieval_hints_for_kinds` import. Re-run targeted recall, signal,
    object-rerank, object-cone, CortexFS, and HTTP search tests. Add only
    focused regression assertions if cleanup exposes an untested boundary.
  - **Patterns to follow:** Keep cleanup scoped to files touched by recall
    execution; avoid a broad style sweep.
  - **Test scenarios:**
    - `MemoryQueryService.search` delegates to `MemoryRecallPipelineService`.
    - Recall publishes `recall_completed` without calling skill manager search.
    - HTTP `/api/v1/memory/search` still returns pipeline metadata expected by
      existing tests.
  - **Verification:** Targeted tests and `ruff check` pass for touched files.

## System-Wide Impact

The public memory search API should remain unchanged. The main impact is
developer-facing: fewer apparent recall implementations and a smaller retrieval
execution method. Removing uncalled Python methods may break only direct
in-process consumers that bypass the public API; current repo references should
be checked before removal.

## Risks and Mitigations

- **Risk:** Hidden tests import `CortexFS.find/search`.
  **Mitigation:** Remove only after repo-wide caller scan; if compatibility is
  needed, replace with a thin delegating error or documented migration path
  rather than keeping full duplicate logic.
- **Risk:** Splitting `_execute_object_query` accidentally changes filter
  ordering or candidate limits.
  **Mitigation:** Keep extracted helpers mechanically equivalent and run object
  retrieval tests before broader cleanup.
- **Risk:** Removing `session_search` exposes stale docs or plans.
  **Mitigation:** Update only active code/test references in this pass; leave
  historical plans untouched.

## Verification Plan

- `uv run --group dev pytest tests/test_memory_recall_pipeline_service.py tests/test_memory_signal_integration.py -q`
- `uv run --group dev pytest tests/test_object_rerank.py tests/test_object_cone.py tests/test_retrieval_candidate_service.py -q`
- `uv run --group dev pytest tests/test_http_server.py::TestHTTPServer::test_04_memory_search_returns_results_after_storing tests/test_http_server.py::TestHTTPServer::test_04f_memory_search_exposes_pipeline_trace -q`
- `uv run --group dev pytest tests/test_cortexfs_async.py tests/test_memory_store_layers.py -q`
- `uv run --group dev ruff check src/opencortex/storage/cortex_fs.py src/opencortex/services/retrieval_service.py src/opencortex/orchestrator.py src/opencortex/intent/executor.py`

## Deferred to Implementation

- Whether `IntentAnalyzer` has enough remaining active use to keep in
  `src/opencortex/retrieve/intent_analyzer.py` after removing session search.
- Whether hidden consumers require a temporary compatibility shim for
  `CortexFS.find/search`; current repo evidence should drive the default.
