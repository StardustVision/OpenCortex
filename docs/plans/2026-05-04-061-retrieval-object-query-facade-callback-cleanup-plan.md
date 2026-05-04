---
status: completed
created: 2026-05-04
origin: user request
scope: retrieval object query CortexMemory facade callback cleanup
---

# Retrieval Object Query Facade Callback Cleanup

## Problem

`RetrievalObjectQueryService` owns object-aware recall execution, but after the
last structure cleanup it still calls back through the top-level `CortexMemory`
compatibility facade for several retrieval-domain operations:

- ACL checks
- cone rerank
- object record scoring
- matched anchor extraction
- retrieval query embedding
- raw record to `MatchedContext` projection

Those methods already belong to `RetrievalService` or its candidate helper. The
facade callback makes the recall mainline harder to read because a call that is
logically internal to retrieval appears to leave the retrieval service boundary.

## Scope

In scope:

- Change `src/opencortex/services/retrieval_object_query_service.py` so
  object-query execution calls `RetrievalService` directly for:
  - `_record_passes_acl`
  - `_apply_cone_rerank`
  - `_score_object_record`
  - `_matched_record_anchors`
  - `_embed_retrieval_query`
  - `_records_to_matched_contexts`
- Keep `CortexMemory` compatibility wrappers intact.
- Keep recall ACL, score, rerank, and `QueryResult` semantics unchanged.
- Keep existing recall/object rerank/cone/http tests green.

Out of scope:

- Deleting public or compatibility wrappers from `CortexMemory`.
- Changing `RetrievalService` scoring or ACL logic.
- Changing storage filter semantics.
- Extracting a new service.
- Renaming `_orch` fields in unrelated services.

## Implementation Units

### 1. Direct RetrievalService Calls

Replace `self._orch.<retrieval helper>` calls inside
`RetrievalObjectQueryService` with `self._service.<retrieval helper>` calls.
The direct owner is already injected in the constructor, so this is a boundary
cleanup rather than a behavior change.

### 2. Local Convenience Boundary

Keep the existing `_config`, `_storage`, and `_get_collection` convenience
properties. Remove `_orch` from `RetrievalObjectQueryService` only if no longer
needed after the direct-call cleanup.

### 3. Regression Validation

Run the focused recall tests that exercise object rerank, cone expansion,
pipeline integration, and HTTP exposure.

## Test Plan

Focused tests:

- `uv run --group dev pytest tests/test_object_rerank.py tests/test_object_cone.py -q`
- `uv run --group dev pytest tests/test_memory_recall_pipeline_service.py tests/test_retrieval_candidate_service.py -q`
- `uv run --group dev pytest tests/test_http_server.py -q`

Static checks:

- `uv run --group dev ruff format --check src/opencortex/services/retrieval_object_query_service.py`
- `uv run --group dev ruff check src/opencortex/services/retrieval_object_query_service.py`

## Risks

- Tests may monkeypatch `CortexMemory` compatibility methods. Keep wrappers
  untouched and validate whether focused tests depend on facade-level patches.
- Object recall scoring is sensitive. Do not change arguments, candidate order,
  score thresholds, or timing/result assembly.
