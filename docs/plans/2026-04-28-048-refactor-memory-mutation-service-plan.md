---
title: refactor: Extract Memory Mutation Service
type: refactor
status: completed
date: 2026-04-28
---

# refactor: Extract Memory Mutation Service

## Overview

Extract `MemoryWriteService` update/remove mutation logic into a focused
`MemoryMutationService`. `MemoryWriteService` should keep the store/add
orchestration path plus compatibility wrappers, while mutation-specific record
loading, metadata merge, re-derive/re-embed, upsert, derived projection sync,
entity-index sync, and CortexFS cleanup live in the new service.

## Problem Frame

The store chain has already been split into context building, derive, embed,
dedup, store-record persistence, document write, and directory record services.
After those extractions, the biggest unrelated logic still inside
`src/opencortex/services/memory_write_service.py` is existing-record mutation:
`update()` and `remove()`.

That makes `MemoryWriteService` carry two different domains:

- new memory store orchestration (`add`)
- mutation of already persisted records (`update/remove`)

Splitting mutation keeps the store path easier to read without changing public
facades or HTTP behavior.

## Requirements Trace

- R1. Preserve `MemoryService.update/remove` and
  `MemoryOrchestrator.update/remove` behavior and signatures.
- R2. Keep `MemoryWriteService.update/remove` as compatibility wrappers.
- R3. Move `update` internals: record load, metadata normalization/merge,
  derive and fact-point preservation, embed/re-embed, storage update,
  CortexFS `write_context`, anchor/fact projection sync, and entity index sync.
- R4. Move `remove` internals: affected record lookup, vector-store delete,
  entity-index remove, CortexFS `rm`, warning/log behavior, and return count.
- R5. Do not change add/store, dedup, document ingest, batch add, directory
  record creation, scoring, or recall behavior.

## Scope Boundaries

- Do not alter update semantics, including fact-point preservation behavior.
- Do not change `remove_by_uri` matching semantics or recursive filesystem
  deletion behavior.
- Do not remove public or private compatibility wrappers in this pass.
- Do not move `_merge_into`, `_check_duplicate`, document write, or batch add.
- Do not convert storage filters or metadata schemas in this pass.

## Current Code Context

- Store/write facade:
  `src/opencortex/services/memory_write_service.py`
- Public memory facade:
  `src/opencortex/services/memory_service.py`
- Orchestrator compatibility facade:
  `src/opencortex/orchestrator.py`
- Record projection helpers used by update:
  `src/opencortex/services/memory_record_service.py`
- Existing focused write helpers:
  `src/opencortex/services/memory_write_context_builder.py`,
  `src/opencortex/services/memory_write_derive_service.py`,
  `src/opencortex/services/memory_write_embed_service.py`,
  `src/opencortex/services/memory_write_dedup_service.py`,
  `src/opencortex/services/memory_store_record_service.py`,
  `src/opencortex/services/memory_directory_record_service.py`
- Regression tests:
  `tests/test_memory_service.py`,
  `tests/test_e2e_phase1.py`,
  `tests/test_http_server.py`,
  `tests/test_write_dedup.py`

Local patterns are strong and this is a mechanical service extraction, so
external research is intentionally skipped.

## Implementation Units

- U1. **Introduce MemoryMutationService**
  - **Goal:** Add a composed mutation service under
    `src/opencortex/services/memory_mutation_service.py`.
  - **Requirements:** R1, R2
  - **Dependencies:** none
  - **Files:**
    - Add: `src/opencortex/services/memory_mutation_service.py`
    - Modify: `src/opencortex/services/memory_write_service.py`
  - **Approach:** Follow the existing service-composition pattern used by
    `MemoryStoreRecordService`, `MemoryDirectoryRecordService`, and
    `RetrievalObjectQueryService`: bind to `MemoryWriteService`, access the
    orchestrator through the parent service, and lazy-load from
    `MemoryWriteService`.
  - **Test scenarios:**
    - Importing `MemoryWriteService` does not introduce circular imports.
    - `MemoryWriteService.update/remove` delegate to the new service with the
      same arguments and return values.
  - **Verification:** Existing memory service wrapper tests continue to pass.

- U2. **Move Update Mutation Logic**
  - **Goal:** Move the full body of `MemoryWriteService.update` into
    `MemoryMutationService.update`.
  - **Requirements:** R1, R2, R3, R5
  - **Dependencies:** U1
  - **Files:**
    - Modify: `src/opencortex/services/memory_mutation_service.py`
    - Modify: `src/opencortex/services/memory_write_service.py`
  - **Approach:** Preserve the current control flow mechanically: load record
    by URI, normalize JSON/dict metadata, merge explicit metadata, re-derive
    layer/object fields when abstract/content changes, preserve prior
    fact-points when derivation does not run, re-embed when needed, update
    storage, sync anchor/fact projection records, write CortexFS content, and
    refresh entity index for changed leaf records.
  - **Test scenarios:**
    - Updating a missing URI still returns `False`.
    - Updating abstract/content re-embeds and writes storage payload fields.
    - Updating content regenerates fact-points and projections.
    - Updating without derivation preserves existing fact-points.
    - Filesystem write failures/behavior remain unchanged for successful
      updates.
  - **Verification:** E2E update tests and memory service tests pass.

- U3. **Move Remove Mutation Logic**
  - **Goal:** Move the full body of `MemoryWriteService.remove` into
    `MemoryMutationService.remove`.
  - **Requirements:** R1, R2, R4, R5
  - **Dependencies:** U1
  - **Files:**
    - Modify: `src/opencortex/services/memory_mutation_service.py`
    - Modify: `src/opencortex/services/memory_write_service.py`
  - **Approach:** Preserve pre-delete entity-index affected-record lookup,
    vector-store `remove_by_uri`, post-delete entity-index batch removal,
    CortexFS `rm`, warning log behavior, and returned delete count.
  - **Test scenarios:**
    - Removing an existing URI deletes vector records and CortexFS data.
    - Recursive removal still uses the existing `recursive` flag.
    - Entity index cleanup still receives affected record IDs when enabled.
    - Filesystem cleanup errors remain warnings and do not change return count.
  - **Verification:** E2E remove tests and HTTP delete tests pass.

- U4. **Clean Imports and Validate Store Path**
  - **Goal:** Keep `MemoryWriteService` focused on add/store orchestration after
    mutation extraction.
  - **Requirements:** R2, R5
  - **Dependencies:** U2, U3
  - **Files:**
    - Modify: `src/opencortex/services/memory_write_service.py`
    - Modify tests only if a focused wrapper assertion is needed.
  - **Approach:** Remove imports that only supported update/remove from
    `MemoryWriteService`, add a lazy `_mutation_service` property, and leave
    `add`, `_check_duplicate`, `_merge_into`, `_ensure_parent_records`,
    `_add_document`, and `batch_add` behavior untouched.
  - **Test scenarios:**
    - `/api/v1/memory/store` still follows the same add path.
    - Dedup merge still calls the update compatibility wrapper successfully.
    - Public delete HTTP behavior remains unchanged.
  - **Verification:** Targeted store, update/remove, dedup, and HTTP tests pass.

## System-Wide Impact

This is an internal service-boundary refactor. Public API behavior should not
change. The intended impact is making `MemoryWriteService` a clearer store-path
coordinator and making existing-record mutation easier to reason about and test
in isolation.

## Risks and Mitigations

- **Risk:** Update fact-point preservation regresses when moving code.
  **Mitigation:** Move logic mechanically and run the E2E update tests that
  cover update/reprojection behavior.
- **Risk:** Entity index cleanup silently changes when `remove` moves.
  **Mitigation:** Preserve lookup and remove-batch order exactly; add a focused
  assertion only if existing tests do not cover the wrapper boundary.
- **Risk:** Circular imports from the new service back to `MemoryWriteService`.
  **Mitigation:** Use `TYPE_CHECKING` imports and lazy construction.
- **Risk:** Compatibility tests patch `MemoryWriteService.update/remove`.
  **Mitigation:** Keep those methods as wrappers with unchanged signatures.

## Verification Plan

- `uv run --group dev ruff check .`
- `uv run --group dev pytest tests/test_memory_service.py -q`
- `uv run --group dev pytest tests/test_e2e_phase1.py::TestE2EPhase1::test_12_update tests/test_e2e_phase1.py::TestE2EPhase1::test_14_remove -q`
- `uv run --group dev pytest tests/test_write_dedup.py -q`
- `uv run --group dev pytest tests/test_http_server.py::TestHTTPServer::test_02_store tests/test_http_server.py::TestHTTPServer::test_03_search -q`

## Deferred to Implementation

- Whether a small dedicated `tests/test_memory_mutation_service.py` is needed
  after checking current update/remove coverage.
- Whether current HTTP delete tests have stable names in this branch; if not,
  use the current route tests that exercise delete behavior.
