---
title: "refactor: Extract memory write dedup service"
type: refactor
status: completed
date: 2026-04-28
origin: user request
---

# refactor: Extract memory write dedup service

## Overview

Extract the semantic deduplication and merge behavior currently embedded in
`MemoryWriteService.add()` into a focused `MemoryWriteDedupService`.
`MemoryWriteService.add()` should remain the store orchestration path:
ingest-mode resolution, URI/context construction, derivation, embedding,
dedup attempt coordination, normal persistence via `MemoryStoreRecordService`,
and timing logs.

The new service should own duplicate search filter construction, duplicate
target lookup, merge content assembly, update/feedback reinforcement,
`memory_stored` merge signal publication, and the result object that tells
`add()` whether the current write was merged.

## Problem Frame

The previous extraction moved post-`Context` persistence into
`src/opencortex/services/memory_store_record_service.py`, but
`src/opencortex/services/memory_write_service.py` still mixes store
orchestration with semantic dedup/merge domain rules:

- `add()` constructs duplicate-search inputs and handles the merge branch
  inline.
- `_check_duplicate()` builds tenant/scope/project filters directly inside the
  write service.
- `_merge_into()` owns filesystem read, merged content assembly, update, and
  feedback reinforcement.
- dedup merge signal publication lives inside the `add()` branch.

These are stable write-dedup domain operations. Moving them behind a composed
service reduces the main store chain without changing `/api/v1/memory/store`
behavior.

## Requirements Trace

- R1. Add `MemoryWriteDedupService` for write-time semantic dedup and merge.
- R2. Move duplicate search filter construction and threshold handling out of
  `MemoryWriteService`.
- R3. Move merge target record loading and merge result assembly out of
  `MemoryWriteService.add()`.
- R4. Move `_merge_into()` behavior, including filesystem read, update, and
  feedback reinforcement, into the new service.
- R5. Move dedup `MemoryStoredSignal` publication into the new service.
- R6. Keep `MemoryWriteService.add()` responsible for ingest, derive, embed,
  normal store persistence, and existing timing/log behavior.
- R7. Preserve public compatibility wrappers on `MemoryService` and
  `MemoryWriteService` for `_check_duplicate()` and `_merge_into()`.
- R8. Preserve `/api/v1/memory/store` behavior and existing tests.

## Scope Boundaries

- Do not change dedup thresholds, mergeability rules, memory-kind mapping, or
  merge signatures.
- Do not change tenant/scope/project isolation semantics.
- Do not change `_merge_into()` update semantics or feedback reward value.
- Do not alter normal record persistence, anchor projections, entity index
  sync, or CortexFS fire-and-forget writes.
- Do not change recall/search behavior.
- Do not remove compatibility wrappers in this PR.

## Current Code References

- `src/opencortex/services/memory_write_service.py`
  - `add()` contains the inline dedup merge branch.
  - `_check_duplicate()` builds the dedup search filter.
  - `_merge_into()` merges content and reinforces feedback.
- `src/opencortex/services/memory_store_record_service.py`
  - Existing composition pattern for extracting post-`Context` persistence.
- `src/opencortex/services/memory_service.py`
  - Compatibility wrappers delegate `_check_duplicate()` and `_merge_into()`.
- `src/opencortex/services/memory_signals.py`
  - `MemoryStoredSignal` includes `dedup_action`.
- `tests/test_write_dedup.py`
  - Main behavior coverage for write-time dedup.
- `tests/test_context_manager.py`
  - Regression coverage for `_merge_into()` preserving fact points.
- `tests/test_memory_signal_integration.py`
  - Store signal boundary coverage.

## Key Technical Decisions

- Name the new service `MemoryWriteDedupService`, matching the current write
  service ownership while keeping it narrower than a full store pipeline.
- Bind the service to `MemoryWriteService`, consistent with
  `MemoryStoreRecordService` and `MemoryDocumentWriteService`.
- Introduce a small result dataclass that returns whether a merge happened,
  the duplicate target URI/score, the loaded existing record, the updated
  context, and `dedup_ms`.
- Use a tiny typed filter expression builder inside the new service for dedup
  filters instead of spreading hand-written nested storage-DSL dictionaries
  through the domain code. The storage boundary still receives the existing
  dict DSL via `.to_dict()`.
- Keep the final merged-path logger in `MemoryWriteService.add()` so existing
  timing log shape stays centralized with the created-path logger.
- Keep `_check_duplicate()` and `_merge_into()` wrappers on
  `MemoryWriteService`; route them to the new service so tests and legacy
  callers keep working.

## Implementation Units

### U1. Create MemoryWriteDedupService

Goal: Move write-time duplicate search and merge behavior into a focused
service.

Requirements: R1, R2, R3, R4, R5

Files:

- Create: `src/opencortex/services/memory_write_dedup_service.py`
- Modify: `src/opencortex/services/memory_write_service.py`
- Test: `tests/test_memory_write_dedup_service.py`

Approach:

- Add `DuplicateMatch(uri: str, score: float)` or equivalent result shape for
  duplicate search.
- Add `DedupMergeResult` carrying `merged`, `ctx`, `target_uri`,
  `score`, `existing_record`, and `dedup_ms`.
- Move duplicate filter construction into `check_duplicate(...)`.
- Move `_merge_into()` behavior into `merge_into(...)`.
- Add `try_merge_duplicate(...)` that:
  - times duplicate search;
  - returns a non-merged result when no duplicate is found;
  - loads the target record;
  - calls `merge_into(...)`;
  - publishes `MemoryStoredSignal(..., dedup_action="merged")`;
  - updates the returned context URI and dedup metadata.

Test scenarios:

- Duplicate filter includes tenant, leaf, memory kind, merge signature,
  shared/private ownership scope, and project isolation.
- Duplicate search returns `None` below threshold and a match at/above
  threshold.
- Merge signal uses the persisted target record id/project/category/context
  fields and sets `dedup_action="merged"`.
- `merge_into()` reads existing content, appends new content with the same
  separator, delegates update, and reinforces feedback with `0.5`.

Verification:

- `uv run --group dev pytest tests/test_memory_write_dedup_service.py -q`

### U2. Wire MemoryWriteService.add to MemoryWriteDedupService

Goal: Keep `add()` as orchestration and delegate the merge branch.

Requirements: R6, R7, R8

Files:

- Modify: `src/opencortex/services/memory_write_service.py`
- Modify: `src/opencortex/services/memory_service.py` only if wrapper docstrings
  or delegation targets require adjustment.
- Test: `tests/test_write_dedup.py`
- Test: `tests/test_context_manager.py`
- Test: `tests/test_memory_signal_integration.py`

Approach:

- Add a lazy `_write_dedup_service` property.
- Replace the inline dedup branch with a call to `try_merge_duplicate(...)`.
- Keep the existing merged-path timing log in `add()` using `dedup_ms` from the
  service result.
- Keep `_check_duplicate()` and `_merge_into()` methods on
  `MemoryWriteService`, delegating to the new service.
- Leave normal persistence through `MemoryStoreRecordService` unchanged.

Test scenarios:

- Existing write-dedup tests still pass for mergeable/non-mergeable,
  cross-tenant, cross-category, dedup disabled, non-leaf, and no-embedder
  cases.
- `_merge_into()` compatibility path still preserves fact-point projections.
- Store signal integration still emits normal store signals, with dedup merge
  signal behavior unchanged where covered by new tests.

Verification:

- `uv run --group dev pytest tests/test_memory_write_dedup_service.py tests/test_write_dedup.py -q`
- `uv run --group dev pytest tests/test_context_manager.py::TestContextManagerRegression::test_merge_into_preserves_fact_points -q`
- `uv run --group dev pytest tests/test_memory_signal_integration.py -q`

## Verification Plan

- `uv run --group dev pytest tests/test_memory_write_dedup_service.py tests/test_write_dedup.py -q`
- `uv run --group dev pytest tests/test_context_manager.py tests/test_e2e_phase1.py -q`
- `uv run --group dev pytest tests/test_http_server.py tests/test_eval_contract.py tests/test_memory_signal_integration.py -q`
- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`
