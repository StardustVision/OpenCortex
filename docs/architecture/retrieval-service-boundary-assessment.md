# RetrievalService Boundary Assessment

Created: 2026-04-28

## Current State

`src/opencortex/services/retrieval_service.py` is currently 999 lines. It is
still below the local 1000-line target, but it is now the largest service after
`MemoryOrchestrator` cleanup.

The service owns several distinct concerns:

| Concern | Methods | Current owner fit |
|---|---|---|
| Probe/planner/runtime binding | `probe_memory`, `memory_probe_mode`, `memory_probe_trace`, `plan_memory`, `bind_memory_runtime` | Cohesive but separable from execution |
| Filter construction and scope extraction | `_build_search_filter`, `_build_probe_filter`, `_cone_query_entities` | Shared retrieval support logic |
| Query embedding and cone/rerank prep | `_embed_retrieval_query`, `_apply_cone_rerank` | Execution-adjacent |
| Object record scoring and ACL projection | `_score_object_record`, `_record_passes_acl`, `_matched_record_anchors`, `_records_to_matched_contexts` | Candidate projection concern |
| Main object query execution | `_execute_object_query` | Largest and most risk-bearing method |
| Session-aware search | `session_search` | Public retrieval entrypoint |
| Result aggregation | `_aggregate_results` | Small but patched by tests |

## Existing Compatibility Seams

Several tests patch or call the orchestrator wrappers directly:

- `tests/test_perf_fixes.py` patches `bind_memory_runtime`,
  `_execute_object_query`, and `_aggregate_results`.
- `tests/test_object_rerank.py` and `tests/test_object_cone.py` call
  `_execute_object_query` through the orchestrator.
- `tests/test_recall_planner.py` calls `plan_memory`.
- `tests/test_context_manager.py` patches `probe_memory` and `plan_memory`, and
  has multiple direct `_execute_object_query` calls.

Any future split must keep `MemoryOrchestrator` wrappers and likely keep
`RetrievalService` wrappers for at least one phase, because tests and upstream
services rely on those names.

## Recommended Next Split

Do not split by public method first. The next low-risk split is to extract the
candidate projection helpers:

- `_score_object_record`
- `_record_passes_acl`
- `_matched_record_anchors`
- `_records_to_matched_contexts`

Proposed owner: `src/opencortex/services/retrieval_candidate_service.py`.

Why this seam first:

- It removes a coherent block from `RetrievalService` without changing the
  probe/planner/runtime public path.
- It is called by `_execute_object_query`, so `RetrievalService` can keep the
  main orchestration flow while delegating projection details.
- It has concrete behavioral tests through object rerank, object cone, context
  manager object-query cases, and memory service search tests.
- It does not touch `MemoryOrchestrator` wrapper signatures.

## Defer These Splits

Defer splitting probe/planner/runtime binding until after candidate projection
is extracted. Those methods are small and tightly connected to public search
flow semantics.

Defer splitting `_execute_object_query` itself until its helper dependencies are
smaller. It is the highest-risk method because it coordinates filters, query
embedding, dense/sparse search, cone expansion, rerank, ACL projection, detail
levels, and explain metadata.

Defer extracting `_aggregate_results` alone. It is small and currently patched
by tests through the orchestrator; moving it alone creates compatibility churn
without meaningful ownership improvement.

## Required Guard Tests For Future Split

Before and after a retrieval candidate split, run:

- `uv run --group dev pytest tests/test_object_rerank.py tests/test_object_cone.py -q`
- `uv run --group dev pytest tests/test_perf_fixes.py -q`
- `uv run --group dev pytest tests/test_recall_planner.py -q`
- `uv run --group dev pytest tests/test_context_manager.py -q`
- `uv run --group dev pytest tests/test_memory_service.py tests/test_e2e_phase1.py -q`

## Decision

The next retrieval cleanup should be a bounded candidate-projection extraction,
not a broader planner/runtime split. That keeps the riskiest method in place
while reducing the helper surface around it.
