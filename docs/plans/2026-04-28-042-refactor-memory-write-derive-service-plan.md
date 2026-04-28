---
title: "refactor: Extract memory write derive service"
type: refactor
status: completed
date: 2026-04-28
origin: user request
---

# refactor: Extract memory write derive service

## Overview

Extract the derive/fallback-derive portion of `MemoryWriteService.add()` into a
focused `MemoryWriteDeriveService`. `MemoryWriteService.add()` should continue
to orchestrate ingest routing, target resolution, context building, embedding,
dedup, store persistence, and timing logs. The new service should own only the
content-leaf derive decision, `defer_derive` fallback overview/abstract
generation, derive timing, and the result object passed into the context
builder.

## Problem Frame

The store write path has already had record persistence, semantic dedup/merge,
and context assembly extracted. The remaining non-trivial domain branch in
`src/opencortex/services/memory_write_service.py::add()` is derive:

- content leaf writes call `_derive_layers(...)` unless `defer_derive=True`;
- non-deferred derive can fill missing `abstract` and `overview`;
- deferred derive uses deterministic fallback overview and abstract helpers;
- derive timing is measured inline as `derive_layers_ms`;
- `layers` is later passed to `MemoryWriteContextBuilder`.

This behavior is cohesive and can be extracted without moving embed/dedup/store
or changing the public store API.

## Requirements Trace

- R1. Add `MemoryWriteDeriveService`.
- R2. Move content leaf non-deferred `_derive_layers(...)` execution out of
  `MemoryWriteService.add()`.
- R3. Move `defer_derive` fallback overview/abstract logic out of `add()`.
- R4. Move derive timing calculation into the new service.
- R5. Return an explicit derive result containing `abstract`, `overview`,
  `layers`, and `derive_layers_ms`.
- R6. Keep `MemoryWriteService.add()` responsible for ingest, context-builder,
  embed, dedup, store persistence, and timing log behavior.
- R7. Preserve `/api/v1/memory/store` behavior and existing tests.

## Scope Boundaries

- Do not change `_derive_layers(...)` internals or prompt behavior.
- Do not change fallback overview/abstract helper semantics.
- Do not move embedding in this PR.
- Do not change context assembly, dedup, persistence, signals, or TTL behavior.
- Do not alter document ingest routing or deferred document derive behavior.

## Current Code References

- `src/opencortex/services/memory_write_service.py`
  - `add()` currently owns derive/fallback branch and timing.
- `src/opencortex/services/memory_write_context_builder.py`
  - Consumes `abstract`, `overview`, and `layers`.
- `src/opencortex/services/derivation_service.py`
  - Owns `_derive_layers`, fallback overview, and abstract derivation helpers
    behind orchestrator compatibility methods.
- `tests/test_vectorization_expansion.py`
  - Verifies derived keywords affect embed text.
- `tests/test_context_manager.py`
  - Verifies fact-point derivation behavior through `add()`.
- `tests/test_document_async_derive.py`
  - Covers deferred/document derive-adjacent behavior.

## Key Technical Decisions

- Name the service `MemoryWriteDeriveService`, scoped to the write path rather
  than the broader derivation domain.
- Bind it to `MemoryWriteService` to match the composition style of
  `MemoryWriteContextBuilder`, `MemoryWriteDedupService`, and
  `MemoryStoreRecordService`.
- Add `MemoryWriteDeriveResult` with `abstract`, `overview`, `layers`, and
  `derive_layers_ms`.
- Keep `layers` empty for no-content, non-leaf, and deferred derive paths,
  matching the current behavior.
- Keep timing at `0` for deferred fallback, matching the current behavior where
  only `_derive_layers(...)` is timed.

## Implementation Units

### U1. Create MemoryWriteDeriveService

Goal: Move derive/fallback derive behavior into a focused service.

Requirements: R1, R2, R3, R4, R5

Files:

- Create: `src/opencortex/services/memory_write_derive_service.py`
- Modify: `src/opencortex/services/memory_write_service.py`
- Test: `tests/test_memory_write_derive_service.py`

Approach:

- Add `MemoryWriteDeriveResult`.
- Add `derive_for_write(...)` with explicit inputs:
  - `abstract`, `overview`, `content`, `is_leaf`, and `defer_derive`.
- For `content and is_leaf and not defer_derive`:
  - time `_derive_layers(...)`;
  - fill missing `abstract`/`overview` from returned layers.
- For `content and is_leaf and defer_derive`:
  - fill missing `overview` from `_fallback_overview_from_content(...)`;
  - fill missing `abstract` from `_derive_abstract_from_overview(...)`;
  - keep `layers={}` and `derive_layers_ms=0`.
- For non-content or non-leaf paths:
  - return original `abstract`, `overview`, empty `layers`, and timing `0`.

Test scenarios:

- Non-deferred content leaf calls `_derive_layers(...)`, fills missing
  `abstract`/`overview`, returns layers, and records a non-negative timing.
- Non-deferred content leaf preserves caller-provided `abstract`/`overview`
  while still returning layers.
- Deferred derive uses fallback helpers, does not call `_derive_layers(...)`,
  and returns empty layers/timing `0`.
- Non-leaf or empty content does not call derive/fallback helpers.

Verification:

- `uv run --group dev pytest tests/test_memory_write_derive_service.py -q`

### U2. Wire MemoryWriteService.add to MemoryWriteDeriveService

Goal: Keep `add()` as orchestration and delegate derive result construction.

Requirements: R6, R7

Files:

- Modify: `src/opencortex/services/memory_write_service.py`
- Test: `tests/test_vectorization_expansion.py`
- Test: `tests/test_document_async_derive.py`
- Test: `tests/test_context_manager.py`

Approach:

- Add a lazy `_write_derive_service` property.
- Replace inline derive/fallback branch with
  `derive_result = await self._write_derive_service.derive_for_write(...)`.
- Feed `derive_result.abstract`, `derive_result.overview`, and
  `derive_result.layers` into `MemoryWriteContextBuilder`.
- Continue using `derive_result.derive_layers_ms` in existing timing logs.
- Leave embed/dedup/store blocks unchanged.

Test scenarios:

- Derived keyword vectorization remains unchanged.
- Fact-point add behavior remains unchanged.
- Deferred derive and document async derive behavior remains unchanged.
- Public store API behavior remains unchanged.

Verification:

- `uv run --group dev pytest tests/test_memory_write_derive_service.py tests/test_vectorization_expansion.py tests/test_document_async_derive.py -q`
- `uv run --group dev pytest tests/test_context_manager.py tests/test_e2e_phase1.py -q`

## Verification Plan

- `uv run --group dev pytest tests/test_memory_write_derive_service.py tests/test_vectorization_expansion.py tests/test_document_async_derive.py -q`
- `uv run --group dev pytest tests/test_http_server.py tests/test_eval_contract.py tests/test_write_dedup.py -q`
- `uv run --group dev pytest tests/test_context_manager.py tests/test_e2e_phase1.py -q`
- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`
