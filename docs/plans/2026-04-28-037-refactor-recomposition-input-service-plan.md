---
title: "refactor: Extract recomposition input service"
type: refactor
status: completed
date: 2026-04-28
origin: user request
---

# refactor: Extract recomposition input service

## Overview

Move recomposition input and record assembly logic out of
`SessionRecompositionEngine` into `RecompositionInputService`. The recomposition
engine should keep merge/full-recomposition orchestration, snapshot/restore, URI
construction, cleanup, and write/derive behavior. The new service owns the
deterministic and storage-backed preparation of records and
`RecompositionEntry` objects consumed by the engine.

## Problem Frame

After segmentation and task lifecycle extraction, `SessionRecompositionEngine`
still mixes orchestration with input preparation. The remaining assembly logic
does not own recomposition writes: it selects the recent merged-tail window,
loads immediate records, hydrates merged-tail content, aggregates metadata from
source records, and creates ordered `RecompositionEntry` values.

Keeping this in the engine makes the class harder to scan and leaves
record-preparation details mixed with `_merge_buffer`,
`_run_full_session_recomposition`, and session summary generation.

## Requirements Trace

- R1. Add `RecompositionInputService` for recomposition input/record assembly.
- R2. Move merged-tail selection, immediate record loading, metadata aggregation,
  and merged-tail plus immediate `RecompositionEntry` building into the service.
- R3. `SessionRecompositionEngine` calls the service for input assembly and
  continues to own merge/full recomposition orchestration.
- R4. Preserve `ContextManager` compatibility wrappers for
  `_build_recomposition_entries`, `_aggregate_records_metadata`, and existing
  input-related behavior.
- R5. Preserve benchmark ingest behavior where it calls manager wrappers for
  metadata aggregation and segment anchor/time extraction.
- R6. Do not change record ordering, entry payload shape, token estimation,
  tail selection limits, metadata aggregation rules, source URI contracts, or
  cleanup behavior.

## Scope Boundaries

- Do not move `_merge_buffer`, `_take_merge_snapshot`, `_restore_merge_snapshot`,
  `_run_full_session_recomposition`, `_generate_session_summary`, or durable
  merged/directory write logic in this PR.
- Do not move task lifecycle or segmentation logic again; those are already
  owned by `ContextRecompositionTaskService` and
  `RecompositionSegmentationService`.
- Do not remove private compatibility wrappers in `ContextManager`.
- Do not change benchmark ingest APIs, response shapes, or adapter behavior.

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/context/recomposition_engine.py` currently owns
  `_select_tail_merged_records`, `_load_immediate_records`,
  `_aggregate_records_metadata`, and `_build_recomposition_entries`.
- `src/opencortex/context/recomposition_segmentation.py` already owns pure
  anchor/time extraction and segmentation algorithms.
- `src/opencortex/context/recomposition_tasks.py` already owns background task
  lifecycle state.
- `src/opencortex/context/manager.py` delegates
  `_build_recomposition_entries` and `_aggregate_records_metadata` to the
  engine and must keep those wrappers.
- `src/opencortex/context/benchmark_ingest_service.py` calls manager wrappers
  for `_aggregate_records_metadata`, `_segment_anchor_terms`, and
  `_segment_time_refs`.
- `tests/test_conversation_merge.py` covers entry construction behavior,
  especially CortexFS L2 hydration and fallback paths.
- `tests/test_benchmark_ingest_service.py` has fake-manager coverage for the
  benchmark metadata wrapper boundary.

### Institutional Learnings

- Keep session keys collection-scoped and preserve current behavior while moving
  ownership behind composed services.
- Recent recomposition refactors have used small concrete services and retained
  manager wrappers until callers can be deliberately cut over.

### External References

- Not used. This is an internal refactor based on local code boundaries.

## Key Technical Decisions

- Create `src/opencortex/context/recomposition_input.py` with
  `RecompositionInputService`.
- Pass the manager into the input service because the service needs
  `_session_records`, `_orchestrator`, `_estimate_tokens`, and conversation
  buffer types through current boundaries.
- Compose the input service inside `SessionRecompositionEngine`.
- Keep thin engine wrapper methods for current internal call sites and manager
  compatibility.
- Add direct service tests for input assembly while leaving existing
  `tests/test_conversation_merge.py` as wrapper/integration guardrails.

## Open Questions

### Resolved During Planning

- Should cleanup helper `_list_immediate_uris` move too? No for this PR. It is
  end-cleanup support, not recomposition input assembly. Immediate record loading
  for recomposition means `_load_immediate_records`.
- Should full recomposition entry assembly move? Yes where it constructs
  `RecompositionEntry` values from records, but the orchestration loop and write
  behavior stay in the engine. Implementation may keep inline full-recompose
  record-to-entry construction if extracting it would expand scope beyond the
  user-requested merge-tail plus immediate assembly.

### Deferred to Implementation

- Whether to expose a small `entry_from_record(...)` helper for the full
  recomposition path, or keep that path inline until full recomposition write
  extraction. Prefer the smaller safe change if test risk rises.

## Implementation Units

- U1. **Create RecompositionInputService**

**Goal:** Move deterministic input/record assembly from the engine into a
service.

**Requirements:** R1, R2, R6

**Dependencies:** None

**Files:**
- Create: `src/opencortex/context/recomposition_input.py`
- Modify: `src/opencortex/context/recomposition_engine.py`
- Test: `tests/test_recomposition_input.py`
- Test: `tests/test_conversation_merge.py`

**Approach:**
- Move `_select_tail_merged_records`, `_load_immediate_records`,
  `_aggregate_records_metadata`, and `_build_recomposition_entries` to the new
  service.
- Use `RecompositionSegmentationService` from the input service for anchor/time
  extraction.
- Preserve token estimation via the manager's `_estimate_tokens`.
- Preserve CortexFS L2 content hydration and fallback to stored record text.

**Patterns to follow:** Existing concrete service style in
`RecompositionSegmentationService` and `ContextRecompositionTaskService`.

**Test scenarios:**
- Tail selection respects max merged leaves and max message window.
- Immediate records are returned in input URI order.
- Entry building hydrates merged-tail L2 content from CortexFS and falls back to
  stored text when missing.
- Metadata aggregation preserves stable unique entities, time refs, topics, and
  first event date.

**Verification:** New direct service tests and existing conversation merge tests
pass.

- U2. **Wire engine and compatibility wrappers**

**Goal:** Make `SessionRecompositionEngine` call `RecompositionInputService`
while preserving existing manager and benchmark-facing behavior.

**Requirements:** R3, R4, R5, R6

**Dependencies:** U1

**Files:**
- Modify: `src/opencortex/context/recomposition_engine.py`
- Modify: `src/opencortex/context/manager.py` only if wrapper signatures need
  type updates
- Test: `tests/test_context_manager.py`
- Test: `tests/test_benchmark_ingest_service.py`

**Approach:**
- Add `self._input = RecompositionInputService(manager)` in the engine.
- Replace engine method bodies with thin delegation.
- Keep manager wrappers unchanged.
- Keep benchmark ingest calls unchanged.

**Patterns to follow:** Previous segmentation/task-service extraction pattern:
compose service, keep wrappers, test both direct service and existing wrapper
behavior.

**Test scenarios:**
- `ContextManager._build_recomposition_entries` still returns the same entry
  shape and ordering.
- Benchmark ingest metadata aggregation still returns the same merged metadata.
- Online merge still builds segments from immediate plus merged-tail entries.

**Verification:** Focused context, conversation merge, and benchmark ingest
tests pass.

## Verification Plan

- `uv run --group dev pytest tests/test_recomposition_input.py tests/test_conversation_merge.py -q`
- `uv run --group dev pytest tests/test_context_manager.py tests/test_benchmark_ingest_service.py tests/test_benchmark_ingest_lifecycle.py -q`
- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`
