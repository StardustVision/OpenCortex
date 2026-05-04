---
status: completed
created: 2026-05-04
origin: user request
scope: retrieval object query internal structure cleanup
---

# Retrieval Object Query Structure Cleanup

## Problem

`RetrievalObjectQueryService` owns object-aware recall execution. It is no
longer oversized, but it still mixes:

- ad hoc single-field storage filter dictionaries,
- leaf / anchor / fact-point search filter construction,
- projected leaf hydration,
- rescoring,
- `QueryResult` assembly.

The goal is to make the internal structure easier to read without changing
retrieval behavior.

## Scope

In scope:

- Convert simple internal filter builders to `FilterExpr` where the serialized
  DSL is identical:
  - `parent_uri`
  - `session_id`
  - `source_doc_id`
  - `is_leaf`
  - `retrieval_surface`
  - `uri`
- Group and name small helpers around the three object-query stages:
  - leaf search
  - anchor projection search
  - fact-point search
- Keep `QueryResult` assembly behavior unchanged.
- Keep existing recall/object rerank/cone tests green.

Out of scope:

- Extracting a new service.
- Changing ACL behavior.
- Changing score/rescore semantics.
- Reworking candidate limits or search fanout.
- Renaming public wrappers on `RetrievalService` / `CortexMemory`.

## Implementation Units

### 1. Read Current Object Query Flow

Inspect `src/opencortex/services/retrieval_object_query_service.py` and classify
existing helpers:

- scope/start filters
- dense/sparse search calls
- projection hydration
- rescore/result assembly

### 2. FilterExpr Cleanup

Replace low-risk filter dicts with `FilterExpr` helpers. Avoid changing
composed filters where shape/order is not trivially identical.

### 3. Helper Naming and Grouping

Rename or add small internal helpers only when it clarifies one of the object
query stages. Keep helper scope inside `RetrievalObjectQueryService`.

### 4. Validation

Run focused retrieval tests and static checks.

## Test Plan

Focused tests:

- `uv run --group dev pytest tests/test_object_rerank.py tests/test_object_cone.py -q`
- `uv run --group dev pytest tests/test_memory_recall_pipeline_service.py tests/test_retrieval_candidate_service.py tests/test_retrieval_support.py -q`
- `uv run --group dev pytest tests/test_context_manager.py -q`

Static checks:

- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`

LFG checks:

- `ce-code-review mode:autofix plan:docs/plans/2026-05-04-060-retrieval-object-query-structure-cleanup-plan.md`
- `ce-test-browser mode:pipeline`
- Commit, push, and open PR.

## Risks

- Object query scoring is sensitive. Do not touch scorer inputs, order, or
  weights.
- Filter order can matter for tests that assert exact dictionaries. Preserve
  shape where tests pin it.
- Hydration of projected leaves is part of recall correctness; only rename and
  isolate helpers, do not change fallback behavior.
