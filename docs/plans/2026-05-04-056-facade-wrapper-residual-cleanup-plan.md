---
status: completed
created: 2026-05-04
origin: user request
scope: facade wrapper residual cleanup and simple filter expressions
---

# Facade Wrapper Residual Cleanup

## Problem

Recent refactors moved store, recall, recomposition, derivation, and
orchestrator service ownership out of large facade classes. The remaining
surface still contains old migration seams:

- Some private compatibility wrappers are pure forwarding methods.
- `RetrievalService._build_probe_filter()` still calls back through
  `MemoryOrchestrator._build_search_filter()` instead of using its own
  local filter builder.
- Several low-risk single-field storage filters are still hand-written
  dictionaries even though `FilterExpr` exists.

The goal is cleanup, not behavior change.

## Scope

In scope:

- Scan `MemoryOrchestrator`, `ContextManager`, and `RetrievalService` for
  private wrappers that only forward to extracted services.
- Remove only private wrappers with no live source or test references.
- Keep public APIs, documented compatibility surfaces, and `__new__` bypass
  service access contracts intact.
- Remove the `RetrievalService -> MemoryOrchestrator -> RetrievalService`
  callback in `_build_probe_filter()`.
- Convert low-risk single-field filters to `FilterExpr` where the shape is
  exactly equivalent:
  - `uri`
  - `session_id`
  - simple `prefix`
  - simple `must_not`
- Keep more complex ACL or object query filter restructuring out of this pass
  unless it is trivially one-to-one.

Out of scope:

- Removing public facade methods from `MemoryOrchestrator`.
- Removing `ContextManager` wrappers still used by benchmark or tests.
- Reworking retrieval object query semantics.
- Changing visibility/ACL behavior.
- Renaming service APIs.

## Implementation Units

### 1. Wrapper Reference Audit

Use `rg` against `src/` and `tests/` before deleting any wrapper. Candidate
wrappers must meet all conditions:

- private method name starts with `_`
- body only delegates to another service/static helper
- no references outside its defining module after excluding the method
  definition itself
- no tests pinning the wrapper as compatibility behavior

Expected likely outcome: most `MemoryOrchestrator` private wrappers stay
because tests and migration seams still reference them. This unit may produce
few deletions; the audit is still required to avoid guessing.

### 2. Retrieval Probe Filter Callback Cleanup

Update `src/opencortex/services/retrieval_service.py`:

- Change `_build_probe_filter()` to call `self._build_search_filter()` directly.
- Keep `MemoryOrchestrator._build_search_filter()` for compatibility if it has
  live references.

This removes one unnecessary facade callback without changing serialized filter
shape.

### 3. Simple FilterExpr Cleanup

Update low-risk call sites that build single-field filter dictionaries:

- `uri` lookups
- `session_id` lookups
- `prefix` URI subtree lookups
- simple `must_not`

Candidate files from the initial scan:

- `src/opencortex/context/recomposition_input.py`
- `src/opencortex/context/recomposition_state.py`
- `src/opencortex/context/recomposition_write.py`
- `src/opencortex/context/session_records.py`
- `src/opencortex/services/memory_scoring_service.py`
- `src/opencortex/services/memory_mutation_service.py`
- `src/opencortex/services/memory_directory_record_service.py`
- `src/opencortex/services/memory_record_service.py`
- `src/opencortex/services/session_lifecycle_service.py`
- `src/opencortex/services/system_status_service.py`
- `src/opencortex/services/memory_sharing_service.py`

Decision: use `FilterExpr.eq`, `FilterExpr.neq`, `FilterExpr.prefix`, and
`FilterExpr.all(...).to_dict()` only where the generated dictionary matches
the current storage DSL exactly.

## Test Plan

Focused tests:

- `uv run --group dev pytest tests/test_memory_filters.py -q`
- `uv run --group dev pytest tests/test_memory_service.py tests/test_memory_admin_stats_service.py tests/test_memory_query_service.py -q`
- `uv run --group dev pytest tests/test_recomposition_input.py tests/test_recomposition_state.py tests/test_recomposition_write.py tests/test_context_manager.py -q`
- `uv run --group dev pytest tests/test_retrieval_object_query_service.py tests/test_memory_recall_pipeline_service.py -q` if those files exist.

Static checks:

- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`

LFG checks:

- `ce-code-review mode:autofix plan:docs/plans/2026-05-04-056-facade-wrapper-residual-cleanup-plan.md`
- `ce-test-browser mode:pipeline`
- Commit, push, and open PR.

## Risks

- Private wrappers can still be compatibility contracts in this repo. Delete
  only when the reference audit proves no live caller.
- Some storage filters are composed with externally supplied metadata filters.
  Preserve ordering and shape when converting to `FilterExpr`.
- `__new__` bypass tests require lazy service properties to remain robust even
  without `MemoryOrchestrator.__init__`.
