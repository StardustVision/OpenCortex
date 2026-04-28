---
title: "refactor: Extract memory write context builder"
type: refactor
status: completed
date: 2026-04-28
origin: user request
---

# refactor: Extract memory write context builder

## Overview

Extract the context assembly portion of `MemoryWriteService.add()` into a
focused `MemoryWriteContextBuilder`. `MemoryWriteService.add()` should continue
to orchestrate ingest-mode routing, derive/embed execution, dedup, store
persistence, and timing logs. The new builder should own URI and parent
resolution, explicit entity/topic extraction, post-derive metadata merging,
`Context` construction, vectorization text setup, `effective_category`,
`abstract_json`, and memory object payload construction.

## Problem Frame

After the store-record and dedup extractions, `src/opencortex/services/
memory_write_service.py` is smaller, but `add()` still mixes orchestration with
assembly details:

- URI and parent resolution happen inline before derive.
- explicit `entities` and `topics` are normalized inline.
- keyword/topic/anchor metadata merging happens inline after derive.
- `Context`, vectorization text, `abstract_json`, fact-points, and
  `object_payload` are built inline before embed/dedup/store.

Those are data assembly steps, not the main write workflow. A builder service
lets `add()` read as a pipeline while preserving derive/embed/dedup/store
behavior.

## Requirements Trace

- R1. Add `MemoryWriteContextBuilder` for store context assembly.
- R2. Move URI and parent resolution out of `MemoryWriteService.add()`.
- R3. Move explicit entity/topic normalization out of `add()`.
- R4. Move post-derive keyword/topic/entity/anchor metadata merge into the
  builder.
- R5. Move `Context` construction and vectorization text setup into the
  builder.
- R6. Move `effective_category`, `abstract_json`, fact-point injection, and
  `object_payload` construction into the builder.
- R7. Keep `MemoryWriteService.add()` responsible for ingest mode, derive,
  embed, dedup, store persistence, and timing log behavior.
- R8. Preserve `/api/v1/memory/store` behavior and existing tests.

## Scope Boundaries

- Do not change document ingest routing.
- Do not change derive behavior, fallback derive behavior, or embed behavior.
- Do not change dedup, store persistence, record payload fields, signals, or
  TTL logic.
- Do not change URI generation semantics or parent-record creation.
- Do not remove compatibility methods.

## Current Code References

- `src/opencortex/services/memory_write_service.py`
  - `add()` currently owns context assembly around derive/embed orchestration.
- `src/opencortex/services/memory_store_record_service.py`
  - Existing composition pattern for post-`Context` persistence.
- `src/opencortex/services/memory_write_dedup_service.py`
  - Existing composition pattern for dedup/merge result delegation.
- `tests/test_memory_store_record_service.py`
  - Boundary tests for downstream persistence inputs.
- `tests/test_write_dedup.py`
  - Ensures add/dedup behavior remains unchanged.
- `tests/test_vectorization_expansion.py`
  - Covers vectorization text expansion with keywords.
- `tests/test_http_server.py` and `tests/test_eval_contract.py`
  - Public store API contract coverage.

## Key Technical Decisions

- Name the new service `MemoryWriteContextBuilder` because it builds the
  transient write context and associated payloads; it should not persist,
  embed, dedup, or write records.
- Bind it to `MemoryWriteService`, following the existing composed service
  style.
- Use small dataclasses for:
  - pre-derive URI/explicit metadata result;
  - post-derive assembled context result.
- Let `MemoryWriteService.add()` still perform derive and pass the resulting
  `layers`, `abstract`, `overview`, and `keywords` into the builder. This keeps
  derive as orchestration for this PR and avoids mixing generation behavior
  into assembly.
- Preserve `MemoryKind(...)` validation in `add()` after `object_payload` is
  returned.

## Implementation Units

### U1. Create MemoryWriteContextBuilder

Goal: Move assembly behavior out of `MemoryWriteService.add()`.

Requirements: R1, R2, R3, R4, R5, R6

Files:

- Create: `src/opencortex/services/memory_write_context_builder.py`
- Modify: `src/opencortex/services/memory_write_service.py`
- Test: `tests/test_memory_write_context_builder.py`

Approach:

- Add `ResolvedWriteTarget` containing `uri`, `parent_uri`,
  `existing_record`, `meta`, `explicit_entities`, and `explicit_topics`.
- Add `AssembledWriteContext` containing `ctx`, `abstract`, `overview`,
  `keywords`, `keywords_list`, `entities`, `meta`, `effective_category`,
  `abstract_json`, `object_payload`, `merge_signature`, and `mergeable`.
- Add `resolve_target(...)` to:
  - copy `meta`;
  - normalize explicit entities/topics using existing merge helpers;
  - auto-generate and uniquify URI when needed;
  - fetch existing record when explicit URI is provided;
  - derive parent URI when omitted.
- Add `assemble_context(...)` to:
  - merge derived entities with explicit entities;
  - merge derived keywords with explicit topics;
  - update `meta["topics"]` and `meta["anchor_handles"]`;
  - build `Context` with effective user identity;
  - apply vectorization override from `embed_text`/keywords;
  - build `abstract_json` and inject `fact_points` for content leaf records;
  - build `object_payload`.

Test scenarios:

- Auto URI path resolves unique URI, derives parent URI, and leaves
  `existing_record` empty.
- Explicit URI path fetches the existing record and reuses its id in the
  returned `Context`.
- Explicit topics/entities merge with derived keywords/entities without
  duplicates and update `meta["topics"]`.
- Anchor handles from derive merge into `meta["anchor_handles"]`.
- Vectorization text uses `embed_text` first and appends keywords.
- `abstract_json` includes fact points for content leaf writes.

Verification:

- `uv run --group dev pytest tests/test_memory_write_context_builder.py -q`

### U2. Wire MemoryWriteService.add to the builder

Goal: Keep `add()` as the orchestration layer and delegate assembly.

Requirements: R7, R8

Files:

- Modify: `src/opencortex/services/memory_write_service.py`
- Test: `tests/test_write_dedup.py`
- Test: `tests/test_vectorization_expansion.py`
- Test: `tests/test_http_server.py`

Approach:

- Add a lazy `_context_builder` property.
- Replace inline URI/meta explicit normalization with `resolve_target(...)`.
- Keep derive and fallback derive branches in `add()`, using explicit entities
  from the resolved target.
- Replace inline keyword/context/payload assembly with `assemble_context(...)`.
- Continue passing assembled outputs to embed, dedup, parent-record creation,
  and `MemoryStoreRecordService` unchanged.

Test scenarios:

- Existing write-dedup tests continue to pass.
- Vectorization expansion behavior remains unchanged.
- HTTP store contracts remain unchanged.
- Store record service tests continue to receive the same assembled payloads.

Verification:

- `uv run --group dev pytest tests/test_memory_write_context_builder.py tests/test_vectorization_expansion.py tests/test_write_dedup.py -q`
- `uv run --group dev pytest tests/test_http_server.py tests/test_eval_contract.py tests/test_memory_store_record_service.py -q`

## Verification Plan

- `uv run --group dev pytest tests/test_memory_write_context_builder.py tests/test_vectorization_expansion.py tests/test_write_dedup.py -q`
- `uv run --group dev pytest tests/test_http_server.py tests/test_eval_contract.py tests/test_memory_store_record_service.py -q`
- `uv run --group dev pytest tests/test_context_manager.py tests/test_e2e_phase1.py -q`
- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`
