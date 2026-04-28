---
title: "refactor: Extract recomposition segmentation service"
type: refactor
status: active
date: 2026-04-28
origin: user request
---

# refactor: Extract recomposition segmentation service

## Overview

Move the pure conversation recomposition segmentation logic out of
`SessionRecompositionEngine` into a small `RecompositionSegmentationService`.
The engine should continue to own I/O, merge buffering, background task
coordination, full-session recomposition, and LLM-derived parent writes. It
should call the segmentation service when it needs anchor/time extraction or
segment construction.

## Problem Frame

`SessionRecompositionEngine` is still large after the ContextManager and
benchmark-ingest extractions. The next clean boundary is pure algorithmic code:
anchor extraction, time-ref extraction, time overlap, sequential segment
building, anchor-clustered segment building, and segment finalization. These
methods do not need the manager back-reference except for already-computed
tokens supplied by callers, so keeping them inside the engine mixes deterministic
domain logic with storage, task lifecycle, and derivation orchestration.

## Requirements Trace

- R1. Extract anchor/time extraction, coarse time-ref matching, time overlap,
  sequential segment building, anchor-clustered segment building, segment
  finalization, and related pure helpers into a dedicated service.
- R2. `SessionRecompositionEngine` calls the service instead of carrying the
  algorithms directly.
- R3. Keep `ContextManager` and `BenchmarkConversationIngestService` observable
  behavior unchanged, including private compatibility wrappers that existing
  tests still exercise.
- R4. Do not change recomposition caps, Jaccard threshold behavior, source
  segment hard-split behavior, URI contracts, or segment payload shape.
- R5. Add focused direct coverage for the new service where the current tests
  only reach the logic through manager wrappers.

## Scope Boundaries

- Do not extract task lifecycle in this PR. `_spawn_*`, `_wait_*`, pending task
  tracking, merge follow-up handling, and close behavior remain in the engine.
- Do not move storage, CortexFS cleanup, record loading, merge snapshot/restore,
  summary generation, directory derivation, or full recomposition orchestration.
- Do not remove `ContextManager` compatibility wrappers in this PR; the request
  is behavior-preserving extraction.
- Do not introduce strategy hierarchies or abstract base classes. This is a
  plain composed service with deterministic methods.

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/context/recomposition_engine.py` currently defines
  `_merge_unique_strings`, `_split_topic_values`, `_segment_anchor_terms`,
  `_segment_time_refs`, `_is_coarse_time_ref`, `_time_refs_overlap`,
  `_build_recomposition_segments`, `_build_anchor_clustered_segments`, and
  `_finalize_recomposition_segment`.
- `src/opencortex/context/manager.py` delegates
  `_build_recomposition_segments`, `_build_anchor_clustered_segments`,
  `_segment_anchor_terms`, and `_segment_time_refs` to the recomposition engine.
- `src/opencortex/context/benchmark_ingest_service.py` uses manager wrappers to
  build benchmark recomposition entries and offline segments.
- `src/opencortex/context/recomposition_types.py` defines
  `RecompositionEntry`, the stable input shape consumed by the segmentation
  algorithms.
- `tests/test_conversation_merge.py`, `tests/test_context_manager.py`,
  `tests/test_benchmark_ingest_service.py`, and
  `tests/test_recomposition_engine.py` cover the current wrapper-driven
  behavior.

### Institutional Learnings

- Recent OpenCortex refactors favor service composition over growing broad
  lifecycle classes.
- The user explicitly wants main-path classes kept smaller, cleaner, and free of
  unrelated compatibility baggage, while preserving current behavior during
  bounded extractions.

### External References

- Not used. This is an internal Python refactor following existing local
  service-composition patterns.

## Key Technical Decisions

- Add `src/opencortex/context/recomposition_segmentation.py` containing
  `RecompositionSegmentationService` plus module-level pure helper functions.
- Keep caps and regex constants with the segmentation service so the algorithm
  boundary is self-contained.
- Give `SessionRecompositionEngine` a composed segmentation service instance,
  initialized in `__init__`, and route all algorithm calls through it.
- Keep engine private wrapper methods for `_segment_anchor_terms`,
  `_segment_time_refs`, `_build_recomposition_segments`, and
  `_build_anchor_clustered_segments` so `ContextManager` and existing tests do
  not need an API cutover in the same PR.
- Prefer direct service tests for pure behavior and leave existing wrapper tests
  as integration guardrails.

## Open Questions

### Resolved During Planning

- Should task lifecycle be extracted now? No. It is a separate boundary and has
  wider coupling to manager close/end behavior.
- Should benchmark ingest call the new service directly? Not required for this
  PR. It can continue through manager wrappers to keep behavior stable.

### Deferred to Implementation

- Whether `_merge_unique_strings` and `_split_topic_values` should remain
  import-compatible from `recomposition_engine.py` or move fully with aliases:
  decide based on current imports and tests.

## Implementation Units

- U1. **Create pure segmentation service**

**Goal:** Move deterministic recomposition segmentation algorithms into a
standalone service module.

**Requirements:** R1, R4, R5

**Dependencies:** None

**Files:**
- Create: `src/opencortex/context/recomposition_segmentation.py`
- Modify: `src/opencortex/context/recomposition_engine.py`
- Test: `tests/test_recomposition_segmentation.py`
- Test: `tests/test_conversation_merge.py`

**Approach:**
- Move anchor/time extraction helpers, coarse time-ref regexes, time-overlap
  logic, sequential splitting, anchor-clustered splitting, finalization, and
  stable string/topic normalization into the new module.
- Keep the public behavior and return shape identical to the current private
  engine methods.
- Add direct tests for extraction, time-overlap coarse handling, hard split by
  `source_segment_index`, anchorless cap behavior, oversized seed behavior, and
  final segment payload deduplication.

**Patterns to follow:** `RecompositionEntry` typed shape in
`src/opencortex/context/recomposition_types.py`; service composition used by
recent context and memory-service extractions.

**Verification:** Direct segmentation tests pass and existing conversation merge
tests still pass.

- U2. **Wire SessionRecompositionEngine through the service**

**Goal:** Make the engine consume the service without owning the segmentation
algorithm implementation.

**Requirements:** R2, R3, R4

**Dependencies:** U1

**Files:**
- Modify: `src/opencortex/context/recomposition_engine.py`
- Modify: `src/opencortex/context/manager.py`
- Modify: `src/opencortex/context/benchmark_ingest_service.py` only if direct
  imports are needed
- Test: `tests/test_recomposition_engine.py`
- Test: `tests/test_context_manager.py`
- Test: `tests/test_benchmark_ingest_service.py`

**Approach:**
- Add `self._segmentation = RecompositionSegmentationService()` in the engine.
- Replace engine-internal calls with `self._segmentation...`.
- Retain thin engine wrapper methods for current manager/test call sites.
- Keep `ContextManager` wrappers and benchmark behavior unchanged.

**Patterns to follow:** Existing lazy manager-to-engine delegation in
`ContextManager._recomposition_engine`; recent extraction style where the
facade remains compatible while the implementation moves behind a collaborator.

**Verification:** Wrapper tests and benchmark ingest tests continue to pass
without response or segment-shape changes.

## Verification Plan

- `uv run --group dev pytest tests/test_recomposition_segmentation.py tests/test_recomposition_engine.py tests/test_conversation_merge.py -q`
- `uv run --group dev pytest tests/test_context_manager.py tests/test_benchmark_ingest_service.py tests/test_benchmark_ingest_lifecycle.py -q`
- `uv run --group dev ruff format --check src/opencortex/context/recomposition_segmentation.py src/opencortex/context/recomposition_engine.py tests/test_recomposition_segmentation.py`
- `uv run --group dev ruff check src/opencortex/context/recomposition_segmentation.py src/opencortex/context/recomposition_engine.py tests/test_recomposition_segmentation.py`
