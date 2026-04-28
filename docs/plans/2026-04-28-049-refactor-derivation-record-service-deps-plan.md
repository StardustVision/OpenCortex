---
title: refactor: Clean Derivation Record Service Dependencies
type: refactor
status: completed
date: 2026-04-28
---

# refactor: Clean Derivation Record Service Dependencies

## Overview

Clean the dependency direction between `DerivationService` and
`MemoryRecordService`. `DerivationService` should call record/projection
behavior directly through a composed `MemoryRecordService`, not bounce through
`MemoryOrchestrator` compatibility wrappers.

`MemoryOrchestrator` wrappers stay in place for existing tests and callers, but
they should no longer be the internal dependency used by derivation.

## Problem Frame

The write path now has focused services for context assembly, derive, embed,
dedup, persistence, directory records, and mutation. The remaining derive-side
coupling issue is in `src/opencortex/services/derivation_service.py`: deferred
derive completion builds abstract payloads and syncs anchor/fact projections,
but it reaches some record/projection helpers through `self._orch._...`.

That creates an unnecessary loop:

`DerivationService -> MemoryOrchestrator wrapper -> MemoryRecordService`

The derive domain should depend directly on the record/projection domain. The
orchestrator should remain a public/backward-compatible facade, not the service
bus between two extracted services.

## Requirements Trace

- R1. Preserve derive queue and deferred derive behavior.
- R2. Preserve store/update projection behavior and existing orchestrator
  compatibility wrappers.
- R3. Replace `DerivationService` internal calls to orchestrator record wrappers
  for:
  - `_build_abstract_json`
  - `_memory_object_payload`
  - `_anchor_projection_prefix`
  - `_fact_point_prefix`
  - `_is_valid_fact_point`
  - `_fact_point_records`
  - `_anchor_projection_records`
  - `_delete_derived_stale`
  - `_sync_anchor_projection_records`
- R4. Use direct composition with `MemoryRecordService` or a lighter projection
  collaborator instead of routing through `MemoryOrchestrator`.
- R5. Do not remove `MemoryOrchestrator` wrappers in this pass.

## Scope Boundaries

- Do not change LLM derive prompts or parsing.
- Do not change fact-point validation, anchor projection record shape, stale
  derived-record cleanup, or embedding behavior.
- Do not change `MemoryRecordService` public wrapper behavior.
- Do not move store/update/session lifecycle callers off orchestrator wrappers
  in this pass unless required for the derivation dependency cleanup.
- Do not introduce a new abstraction unless it makes the dependency boundary
  clearer than direct `MemoryRecordService` composition.

## Current Code Context

- Derive domain:
  `src/opencortex/services/derivation_service.py`
- Record/projection/URI domain:
  `src/opencortex/services/memory_record_service.py`
- Orchestrator compatibility wrappers:
  `src/opencortex/orchestrator.py`
- Callers that must keep working:
  `src/opencortex/services/memory_write_context_builder.py`,
  `src/opencortex/services/memory_store_record_service.py`,
  `src/opencortex/services/memory_mutation_service.py`,
  `src/opencortex/services/session_lifecycle_service.py`
- Regression tests:
  `tests/test_context_manager.py`,
  `tests/test_document_async_derive.py`,
  `tests/test_memory_store_record_service.py`,
  `tests/test_memory_service.py`,
  `tests/test_e2e_phase1.py`

Local patterns are strong and this is a behavior-preserving dependency cleanup,
so external research is intentionally skipped.

## Implementation Units

- U1. **Add Direct Record Service Access to DerivationService**
  - **Goal:** Give `DerivationService` a direct lazy `_memory_record_service`
    collaborator.
  - **Requirements:** R3, R4
  - **Dependencies:** none
  - **Files:**
    - Modify: `src/opencortex/services/derivation_service.py`
  - **Approach:** Prefer `self._orch._memory_record_service` so there is one
    orchestrator-owned `MemoryRecordService` instance and no duplicate service
    state. Keep the dependency explicit inside `DerivationService` via a small
    property.
  - **Test scenarios:**
    - Importing `DerivationService` does not introduce circular imports.
    - Existing orchestrator-level tests that patch wrappers still import and run.
  - **Verification:** `ruff check` and targeted derive imports pass.

- U2. **Replace Orchestrator Record Wrapper Calls**
  - **Goal:** Change `DerivationService` helper methods to delegate directly to
    `MemoryRecordService`.
  - **Requirements:** R2, R3, R4, R5
  - **Dependencies:** U1
  - **Files:**
    - Modify: `src/opencortex/services/derivation_service.py`
  - **Approach:** Update `_build_abstract_json`, `_fact_point_records`,
    `_anchor_projection_records`, `_delete_derived_stale`, and
    `_sync_anchor_projection_records` to call the direct record service. Keep
    static helpers `_memory_object_payload`, `_anchor_projection_prefix`,
    `_fact_point_prefix`, and `_is_valid_fact_point` as direct
    `MemoryRecordService` delegates or convert them to instance delegates only
    if needed by current call sites.
  - **Test scenarios:**
    - Deferred derive completion builds the same `abstract_json`.
    - Deferred derive still syncs anchor/fact projection records.
    - Orchestrator wrapper methods still work for tests that call them directly.
  - **Verification:** Context manager projection tests and document async derive
    tests pass.

- U3. **Clean Comments and Imports**
  - **Goal:** Make the dependency boundary visible and remove stale wrapper
    wording from `DerivationService`.
  - **Requirements:** R4, R5
  - **Dependencies:** U2
  - **Files:**
    - Modify: `src/opencortex/services/derivation_service.py`
  - **Approach:** Update docstrings from "Delegate to orchestrator
    memory-record wrapper" to "Delegate to MemoryRecordService..." where helper
    wrappers remain. Remove unused imports or local imports made obsolete by the
    direct collaborator.
  - **Test scenarios:**
    - No import cycles or unused import lint errors.
  - **Verification:** `uv run --group dev ruff check .`

- U4. **Run Projection and Derive Regression Coverage**
  - **Goal:** Confirm projection behavior did not drift.
  - **Requirements:** R1, R2
  - **Dependencies:** U3
  - **Files:** no expected code changes beyond validation fixes.
  - **Approach:** Run focused tests around derived projections, async derive,
    store record persistence, and update fact-point preservation.
  - **Test scenarios:**
    - Anchor projection record construction still returns the same records.
    - Fact-point records still embed and clean stale records.
    - Update still preserves fact-points when derive is not run.
    - Store path still syncs projection records through orchestrator wrappers.
  - **Verification:** Targeted tests listed below pass.

## System-Wide Impact

This is an internal dependency cleanup. Public memory APIs, store/update
behavior, derive queue behavior, Qdrant record shapes, CortexFS writes, and
orchestrator compatibility wrappers should not change.

## Risks and Mitigations

- **Risk:** Tests patch an orchestrator wrapper expecting derivation to call it.
  **Mitigation:** Current requirement intentionally removes the internal
  dependency; keep public wrappers intact and rely on behavior tests rather than
  wrapper call assertions.
- **Risk:** Creating a second `MemoryRecordService` instance changes state.
  **Mitigation:** Reuse `self._orch._memory_record_service` instead of
  constructing a separate instance.
- **Risk:** Projection behavior changes accidentally.
  **Mitigation:** Move only delegation targets, not record construction logic,
  and run projection-heavy tests.

## Verification Plan

- `uv run --group dev ruff check .`
- `uv run --group dev pytest tests/test_context_manager.py::TestContextManager::test_anchor_projection_records tests/test_context_manager.py::TestContextManager::test_fact_point_records_written_after_add -q`
- `uv run --group dev pytest tests/test_document_async_derive.py -q`
- `uv run --group dev pytest tests/test_memory_store_record_service.py tests/test_memory_service.py -q`
- `uv run --group dev pytest tests/test_e2e_phase1.py::TestE2EPhase1::test_12_update -q`

## Deferred to Implementation

- Exact current test names for projection-heavy context-manager tests should be
  verified from the live test file before running.
- If direct `MemoryRecordService` composition exposes a circular import, use a
  property returning `self._orch._memory_record_service` and keep imports under
  `TYPE_CHECKING`.
