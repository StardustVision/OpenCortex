---
status: active
created: 2026-04-28
task: refactor-memory-recall-pipeline-service
---

# Refactor Memory Recall Pipeline Service Plan

## Problem

`MemoryQueryService.search()` currently owns the complete recall orchestration
flow: probe, plan, runtime binding, scope-filter construction, typed-query
target binding, object-query gathering, aggregation, leaf filtering, runtime
finalization, explain summary creation, recall signal publication, and timing
logs. That makes the query service mix public facade methods with the recall
pipeline itself.

The store path has already been split into focused services while preserving
compatibility wrappers. Recall should follow the same pattern so
`MemoryQueryService.search()` is a small compatibility wrapper and the pipeline
logic has a dedicated home.

## Scope

In scope:

- Add `src/opencortex/services/memory_recall_pipeline_service.py`.
- Move the body of `MemoryQueryService.search()` into
  `MemoryRecallPipelineService.search(...)`.
- Keep `MemoryQueryService.search()` as a compatibility wrapper with the same
  signature and return behavior.
- Keep existing `_build_typed_queries()` and
  `_summarize_retrieve_breakdown()` compatibility helpers in
  `MemoryQueryService` for this phase, because tests and orchestrator facades
  still monkeypatch/call those names.
- Add focused unit tests for the extracted pipeline wrapper and recall signal
  behavior.

Out of scope:

- Changing `RetrievalService._execute_object_query()`.
- Changing planner/probe/runtime semantics.
- Removing compatibility helpers from `MemoryService`, `MemoryQueryService`, or
  `MemoryOrchestrator`.
- Changing `/api/v1/memory/search` response schema or benchmark attribution.

## Existing Patterns

- `src/opencortex/services/memory_write_service.py` now delegates write subflows
  to composed services while keeping compatibility wrappers.
- `tests/test_memory_signal_integration.py` already verifies that recall emits
  `RecallCompletedSignal` without directly invoking the skill engine.
- `tests/test_perf_fixes.py` monkeypatches orchestrator-level
  `_build_typed_queries()` and `_summarize_retrieve_breakdown()`, so the new
  pipeline must continue calling through the existing facade path.

## Implementation Units

1. `src/opencortex/services/memory_recall_pipeline_service.py`
   - Define `MemoryRecallPipelineService`, bound to `MemoryQueryService`.
   - Move recall search orchestration from `MemoryQueryService.search()`.
   - Use `self._query_service._service._build_typed_queries(...)` and
     `_summarize_retrieve_breakdown(...)` so existing compatibility monkeypatches
     continue to work.
   - Preserve timing keys, no-plan short-circuit behavior, explain summary
     behavior, runtime finalization, and recall signal publication.

2. `src/opencortex/services/memory_query_service.py`
   - Add lazy `_recall_pipeline_service`.
   - Replace `search()` body with a delegate preserving the existing signature.
   - Remove imports only needed by the moved pipeline body.
   - Keep typed-query and retrieve-breakdown helpers in place.

3. `tests/test_memory_recall_pipeline_service.py`
   - Verify `MemoryQueryService.search()` delegates to the pipeline service.
   - Verify the pipeline publishes `RecallCompletedSignal` and does not call
     skill search directly.
   - Verify no-plan short-circuit returns an empty `FindResult` with
     `probe_result`.
   - Verify the pipeline still calls existing compatibility helpers for typed
     queries and retrieve breakdown.

## Validation

- `uv run --group dev pytest tests/test_memory_recall_pipeline_service.py -q`
- `uv run --group dev pytest tests/test_memory_signal_integration.py tests/test_perf_fixes.py -q`
- `uv run --group dev pytest tests/test_http_server.py::TestHTTPServer::test_03_search tests/test_http_server.py::TestHTTPServer::test_03c_memory_search_exposes_probe_and_runtime_contract_flags -q`
- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`

## Risks

- Existing tests monkeypatch orchestrator-level recall helper methods; the new
  service must keep the same facade call chain.
- The no-plan short-circuit must not publish recall-completed signals or attach
  runtime results.
- Pipeline timing keys must remain stable because HTTP and benchmark
  attribution read the memory pipeline trace.
