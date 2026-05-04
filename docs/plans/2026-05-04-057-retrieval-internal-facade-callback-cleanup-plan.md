---
status: completed
created: 2026-05-04
origin: user request
scope: retrieval internal facade callback cleanup
---

# Retrieval Internal Facade Callback Cleanup

## Problem

Recent facade cleanup removed one private probe-filter wrapper hop, but
`RetrievalService` can still call back into `MemoryOrchestrator` for helper
logic that is already owned by `RetrievalService` itself. The known residual
case is `_apply_cone_rerank()` calling:

```python
self._orch._cone_query_entities(...)
```

That creates an unnecessary `RetrievalService -> MemoryOrchestrator ->
RetrievalService` path in the recall mainline.

## Scope

In scope:

- Audit `src/opencortex/services/retrieval_service.py` for `self._orch._*`
  calls that are actually internal helper callbacks back to the same service.
- Change `_apply_cone_rerank()` to call `self._cone_query_entities(...)`
  directly.
- Apply any other same-service self-call cleanup only when the behavior is
  exactly equivalent.
- Keep `MemoryOrchestrator` public API and compatibility wrappers unchanged.
- Preserve existing recall, cone rerank, object retrieval, and HTTP behavior.

Out of scope:

- Removing `MemoryOrchestrator` wrappers.
- Reworking object-query execution or ACL filters.
- Renaming public methods or test seams.
- Changing rerank scoring semantics.

## Implementation Units

### 1. Retrieval Callback Audit

Use `rg` and direct file inspection to identify `self._orch._*` calls in
`RetrievalService`.

Classify each call:

- Orchestrator-owned dependency access, keep.
- Storage/bootstrap/config access, keep.
- Same-service helper callback, replace with direct `self.*`.

### 2. Direct Self-Call Cleanup

Update only the confirmed same-service helper callback:

- `_apply_cone_rerank()` should call `_cone_query_entities()` directly.

If no other equivalent callbacks are found, leave the rest unchanged and record
that in the PR body.

## Test Plan

Focused tests:

- `uv run --group dev pytest tests/test_object_cone.py tests/test_object_rerank.py -q`
- `uv run --group dev pytest tests/test_memory_recall_pipeline_service.py tests/test_retrieval_candidate_service.py tests/test_retrieval_support.py -q`
- `uv run --group dev pytest tests/test_context_manager.py -q`

Static checks:

- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`

LFG checks:

- `ce-code-review mode:autofix plan:docs/plans/2026-05-04-057-retrieval-internal-facade-callback-cleanup-plan.md`
- `ce-test-browser mode:pipeline`
- Commit, push, and open PR.

## Risks

- `RetrievalService` still legitimately reaches through the orchestrator for
  subsystem dependencies. Do not convert dependency access into speculative
  constructor injection in this pass.
- Some private orchestrator wrappers are still test compatibility seams. This
  pass must not delete them.
