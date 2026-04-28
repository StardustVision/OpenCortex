---
title: refactor: Extract Retrieval Object Query Service
type: refactor
status: completed
date: 2026-04-28
---

# refactor: Extract Retrieval Object Query Service

## Overview

Extract object-query execution from `RetrievalService` into a dedicated
`RetrievalObjectQueryService`. The public and compatibility surfaces stay the
same: `MemoryRecallPipelineService` still calls `MemoryOrchestrator`, the
orchestrator wrapper still delegates to `RetrievalService._execute_object_query`,
and `RetrievalService._execute_object_query` remains as a thin compatibility
wrapper.

## Problem Frame

`src/opencortex/services/retrieval_service.py` is still close to 1,000 lines
after the recall cleanup. The largest remaining cohesive block is object-query
execution: filter construction, three-layer search, projected leaf hydration,
URI-path attribution, rescoring, and `QueryResult` assembly. Keeping all of
that in `RetrievalService` makes the recall path hard to scan and risks future
growth in the main retrieval facade.

## Requirements Trace

- R1. Preserve `/api/v1/memory/search` behavior and the probe/plan/bind/execute
  recall chain.
- R2. Preserve `MemoryOrchestrator._execute_object_query` and
  `RetrievalService._execute_object_query` as compatibility wrappers.
- R3. Move object query execution helpers out of `RetrievalService`, including
  kind/scope/start filters, candidate limit calculation, layer search,
  projected leaf hydration, URI path source attribution, rescoring, and final
  `QueryResult` assembly.
- R4. Keep object rerank, cone rerank, projection hydrate, HTTP search, and
  recall pipeline tests behaviorally unchanged.
- R5. Do not change scoring formulas, retrieval filters, timing keys, explain
  fields, or ACL behavior in this pass.

## Scope Boundaries

- Do not change store/write behavior.
- Do not modify planner/probe policy.
- Do not remove compatibility wrappers in `MemoryOrchestrator` or
  `RetrievalService`.
- Do not move `RetrievalCandidateService` responsibilities in this pass.
- Do not convert the internal filter DSL to another expression API here; keep
  this extraction mechanical.

## Current Code Context

- Recall orchestration:
  `src/opencortex/services/memory_recall_pipeline_service.py`
- Retrieval facade and compatibility wrapper:
  `src/opencortex/services/retrieval_service.py`
- Candidate scoring/projection helper pattern:
  `src/opencortex/services/retrieval_candidate_service.py`
- Orchestrator compatibility wrapper:
  `src/opencortex/orchestrator.py`
- Primary tests:
  `tests/test_object_rerank.py`,
  `tests/test_object_cone.py`,
  `tests/test_retrieval_candidate_service.py`,
  `tests/test_memory_recall_pipeline_service.py`,
  `tests/test_memory_signal_integration.py`,
  `tests/test_http_server.py`

Local patterns are strong and this is a behavior-preserving service extraction,
so external research is intentionally skipped.

## Implementation Units

- U1. **Introduce RetrievalObjectQueryService**
  - **Goal:** Add `src/opencortex/services/retrieval_object_query_service.py`
    with a service class that composes `RetrievalService`.
  - **Requirements:** R2, R3
  - **Dependencies:** none
  - **Files:**
    - Add: `src/opencortex/services/retrieval_object_query_service.py`
    - Modify: `src/opencortex/services/retrieval_service.py`
  - **Approach:** Follow the existing `RetrievalCandidateService` composition
    pattern: hold the parent retrieval service, expose narrow proxy properties
    for orchestrator-owned subsystems, and keep imports local to the new
    service where possible.
  - **Test scenarios:**
    - `RetrievalService` lazily constructs the object-query service.
    - Existing callers can still invoke `RetrievalService._execute_object_query`.
  - **Verification:** Importing `RetrievalService` and the new service does not
    introduce circular import failures.

- U2. **Move Object Query Filters and Layer Search**
  - **Goal:** Move kind/scope/start filter construction, candidate limit
    calculation, projection target extraction, and leaf/anchor/fact-point search
    into `RetrievalObjectQueryService`.
  - **Requirements:** R3, R5
  - **Dependencies:** U1
  - **Files:**
    - Modify: `src/opencortex/services/retrieval_object_query_service.py`
    - Modify: `src/opencortex/services/retrieval_service.py`
  - **Approach:** Mechanically move
    `_object_query_kind_filter`, `_object_query_scope_filter`,
    `_object_query_candidate_limit`, `_projection_target_uri`,
    `_object_query_filters`, and `_search_object_layers`. Preserve existing
    filter object shapes, layer limits, exception handling, and logger behavior.
  - **Test scenarios:**
    - Object search still applies leaf-only, anchor projection, and fact-point
      filters.
    - Candidate limits remain sensitive to bound plan, recall budget, and rerank
      enablement.
  - **Verification:** Object cone and rerank tests continue to pass.

- U3. **Move Projection Hydration, URI Path Attribution, Rescore, and Assembly**
  - **Goal:** Move the rest of object-query execution into the new service.
  - **Requirements:** R3, R4, R5
  - **Dependencies:** U2
  - **Files:**
    - Modify: `src/opencortex/services/retrieval_object_query_service.py`
    - Modify: `src/opencortex/services/retrieval_service.py`
  - **Approach:** Move `_load_missing_projected_leaves`,
    `_object_query_path_source`, `_rescore_object_records`,
    `_object_query_result`, and the full `_execute_object_query` body. Calls
    that tests patch through orchestrator compatibility wrappers must continue
    to call `self._orch._embed_retrieval_query`, `_record_passes_acl`,
    `_apply_cone_rerank`, `_score_object_record`, `_matched_record_anchors`,
    and `_records_to_matched_contexts`.
  - **Test scenarios:**
    - Projection hits still hydrate missing leaf records and apply ACL before
      appending them.
    - URI path attribution still distinguishes direct, anchor, and fact-point
      paths.
    - Rescoring keeps final score ordering, match reasons, cone metadata, and
      matched anchors.
    - `QueryResult.timing_ms` and `SearchExplain` fields remain unchanged.
  - **Verification:** Existing object rerank/cone tests pass without expected
    output changes.

- U4. **Wire Compatibility Wrappers and Clean Imports**
  - **Goal:** Make `RetrievalService._execute_object_query` delegate to
    `RetrievalObjectQueryService`, leaving the rest of the facade readable.
  - **Requirements:** R1, R2, R4
  - **Dependencies:** U3
  - **Files:**
    - Modify: `src/opencortex/services/retrieval_service.py`
    - Modify tests only if a focused import or wrapper assertion is needed:
      `tests/test_retrieval_candidate_service.py`
  - **Approach:** Add a lazy `_retrieval_object_query_service` property, remove
    imports from `retrieval_service.py` that only supported object execution,
    and keep wrapper signatures unchanged. Do not edit
    `MemoryOrchestrator._execute_object_query` unless current code requires an
    import-only adjustment.
  - **Test scenarios:**
    - `MemoryRecallPipelineService` can still call the orchestrator wrapper.
    - Direct tests calling `orch._execute_object_query` still pass.
    - Direct tests calling `RetrievalService._execute_object_query` still pass.
  - **Verification:** Recall pipeline, signal, HTTP search, object rerank, and
    cone tests pass.

## System-Wide Impact

This is a service-boundary refactor. Public HTTP behavior, planner behavior,
storage schemas, filter DSL shapes, reranking, and explain payloads should not
change. The expected impact is reduced size and responsibility in
`RetrievalService` while preserving current compatibility wrappers for tests
and in-process callers.

## Risks and Mitigations

- **Risk:** Moving helpers changes classmethod/staticmethod binding behavior.
  **Mitigation:** Keep method signatures equivalent and adjust internal calls to
  the new service class explicitly.
- **Risk:** Hidden tests patch object-query helpers on `RetrievalService`.
  **Mitigation:** Preserve only the requested `_execute_object_query`
  compatibility wrapper; helper movement is intentional because those helpers
  are internal domain logic.
- **Risk:** Circular imports between retrieval services.
  **Mitigation:** Use `TYPE_CHECKING` imports and lazy construction, matching
  `RetrievalCandidateService`.
- **Risk:** Behavior drift in filter composition or timing/explain fields.
  **Mitigation:** Move code mechanically and run object, recall, and HTTP tests.

## Verification Plan

- `uv run --group dev ruff check .`
- `uv run --group dev pytest tests/test_object_rerank.py tests/test_object_cone.py tests/test_retrieval_candidate_service.py -q`
- `uv run --group dev pytest tests/test_memory_recall_pipeline_service.py tests/test_memory_signal_integration.py -q`
- `uv run --group dev pytest tests/test_http_server.py::TestHTTPServer::test_04_memory_search_returns_results_after_storing tests/test_http_server.py::TestHTTPServer::test_04f_memory_search_exposes_pipeline_trace -q`

## Deferred to Implementation

- Whether a tiny focused wrapper test is needed after the move; prefer existing
  behavior tests if they already exercise the compatibility wrappers.
- Whether full `tests/test_http_server.py` is feasible locally after targeted
  HTTP checks pass.
