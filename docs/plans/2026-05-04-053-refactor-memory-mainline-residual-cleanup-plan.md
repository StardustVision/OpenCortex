---
status: active
created: 2026-05-04
task: refactor-memory-mainline-residual-cleanup
---

# Refactor Memory Mainline Residual Cleanup Plan

## Problem

The memory store/recall decomposition is mostly complete, but a residual review
found three mainline cleanup issues:

- `MemoryRecallPipelineService` still routes through orchestrator compatibility
  wrappers for typed-query construction, object-query execution, and result
  aggregation even though narrower service owners now exist.
- Search/list/index/dedup filters still hand-build the storage filter DSL in
  several places, and dedup project matching is stricter than recall/list
  visibility.
- `/api/v1/memory/search` exposes a narrower transport shape than the service
  pipeline supports, forcing benchmark/internal callers to encode scope through
  generic metadata filters.

This work should clean those residuals while preserving current public behavior
for existing callers and tests.

## Scope

In scope:

- Add a shared typed filter helper for memory ACL/scope filters.
- Update recall/list/index/dedup to use the shared helper.
- Keep the existing storage filter dict format as the Qdrant adapter contract.
- Let `MemoryRecallPipelineService` call the real query/retrieval services
  directly where possible, keeping orchestrator wrappers for compatibility only.
- Extend the HTTP search request/client with optional service-supported fields:
  `target_uri`, `score_threshold`, `target_doc_id`, and `session_context`.
- Add focused regression tests for the new helper and transport fields.

Out of scope:

- Changing the storage adapter filter DSL.
- Removing orchestrator compatibility wrappers.
- Changing `/api/v1/memory/search` response shape.
- Changing probe/planner/rerank algorithms.
- Changing benchmark scoring semantics.

## Existing Patterns

- `src/opencortex/services/memory_write_dedup_service.py` already has a small
  `FilterExpr` dataclass, but it is private to dedup and only supports part of
  the active DSL.
- `src/opencortex/intent/retrieval_support.py` owns scoped retrieval helpers
  such as `build_scope_filter`, which should stay focused on request scope
  rather than user ACL.
- `src/opencortex/services/retrieval_service.py` owns
  `_build_search_filter`, `_execute_object_query`, and `_aggregate_results`.
- `src/opencortex/http/models.py` keeps transport request/response models close
  to FastAPI routes.

## Key Decisions

1. **Use a shared service-level filter helper, not a new storage abstraction.**
   The storage adapter already accepts dict filters and has tests around that
   DSL. A lightweight helper avoids changing adapter contracts while removing
   duplicated operator dictionaries from memory services.

2. **Normalize ACL semantics around recall visibility.**
   Dedup should not accidentally diverge from recall/list visibility. It should
   reuse the same tenant, scope, and project clauses, then add dedup-specific
   `memory_kind`, `merge_signature`, and `is_leaf` clauses.

3. **Keep compatibility wrappers, but stop using them from the new mainline.**
   `MemoryOrchestrator` and `MemoryService` wrappers remain for tests/external
   private callers, while `MemoryRecallPipelineService` calls
   `MemoryQueryService` and `RetrievalService` directly.

4. **Transport expansion is additive only.**
   New search request fields are optional and default to current behavior.
   Existing clients keep working.

## Implementation Units

### Unit 1: Shared Memory Filter Helper

Files:

- `src/opencortex/services/memory_filters.py`
- `src/opencortex/services/memory_write_dedup_service.py`
- `src/opencortex/services/retrieval_service.py`
- `src/opencortex/services/memory_query_service.py`
- `tests/test_memory_filters.py`
- `tests/test_memory_write_dedup_service.py`

Work:

- Move/replace the private dedup `FilterExpr` with a reusable helper.
- Support `must`, `must_not`, `and`, `or`, and `prefix` shapes used by the
  current memory paths.
- Add helpers for:
  - tenant/source filter
  - private-own-or-shared scope filter
  - project visibility filter
  - full memory visibility filter
- Update search/list/index/dedup to build filters from the helper.
- Align dedup project visibility with recall/list by allowing the current
  project plus public/legacy empty project where the current project is not
  `public`.

Tests:

- Verify helper output for public project and non-public project.
- Verify dedup filter includes memory-kind/merge-signature plus the shared
  visibility clauses.
- Verify list/search filters still exclude staging and superseded records where
  applicable.

### Unit 2: Recall Pipeline Direct Service Calls

Files:

- `src/opencortex/services/memory_recall_pipeline_service.py`
- `src/opencortex/services/memory_query_service.py`
- `src/opencortex/services/retrieval_service.py`
- `tests/test_memory_recall_pipeline_service.py`

Work:

- Replace internal calls to `orch._build_search_filter`,
  `orch._execute_object_query`, and `orch._aggregate_results` with direct
  calls to `orch._retrieval_service`.
- Replace the `MemoryService._build_typed_queries` hop with direct
  `MemoryQueryService._build_typed_queries`.
- Preserve method signatures and wrapper behavior in `MemoryService` and
  `MemoryOrchestrator`.
- Keep timing, explain summary, signal publication, and no-plan behavior
  unchanged.

Tests:

- Update pipeline tests to assert the real owner service is invoked.
- Keep compatibility wrapper tests where they already exist.
- Verify recall signal still publishes without calling skill search.

### Unit 3: HTTP Search Transport Parity

Files:

- `src/opencortex/http/models.py`
- `src/opencortex/http/server.py`
- `src/opencortex/http/client.py`
- `benchmarks/oc_client.py`
- `tests/test_http_server.py`
- `tests/test_benchmark_runner.py`

Work:

- Add optional `target_uri`, `score_threshold`, `target_doc_id`, and
  `session_context` fields to `MemorySearchRequest`.
- Pass these fields through the FastAPI handler to `_orchestrator.search`.
- Preserve `category` and `metadata_filter` merge behavior.
- Let the HTTP client and benchmark client send the new fields when provided.
- Keep old request bodies valid.

Tests:

- Verify HTTP search forwards new transport fields to orchestrator search.
- Verify metadata/category filter behavior still merges correctly.
- Verify benchmark `search_payload` can pass target scope without breaking
  existing metadata-filter retrieval tests.

## Validation

- `uv run --group dev pytest tests/test_memory_filters.py tests/test_memory_write_dedup_service.py -q`
- `uv run --group dev pytest tests/test_memory_recall_pipeline_service.py tests/test_memory_signal_integration.py -q`
- `uv run --group dev pytest tests/test_http_server.py tests/test_benchmark_runner.py -q`
- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`

## Risks

- Dedup project visibility changes can alter whether a new write merges into
  an existing public/shared record. Tests must pin the intended ACL behavior.
- Existing tests may monkeypatch orchestrator wrappers. Wrapper behavior should
  remain intact even if the new mainline stops using them internally.
- HTTP transport additions must be optional to avoid breaking older clients.
