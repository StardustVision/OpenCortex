---
title: refactor: Extract Memory Layer Derivation Service
type: refactor
status: completed
date: 2026-04-29
---

# refactor: Extract Memory Layer Derivation Service

## Summary

Extract the pure LLM-backed layer derivation algorithm from
`DerivationService` into a focused `MemoryLayerDerivationService`.
`DerivationService` remains the queue-facing deferred derive coordinator and
keeps compatibility wrappers for current orchestrator callers.

## Problem Frame

Recent store/recall cleanup has reduced the main write and recall services to
small orchestration layers, but `src/opencortex/services/derivation_service.py`
still mixes two responsibilities: deriving L0/L1/keywords/entities/anchors/fact
points from L2 content, and persisting deferred derive results back into
storage, CortexFS, and projection records. This makes the write-side derive
boundary harder to reason about than the rest of the store pipeline.

## Requirements

- R1. Move pure LLM layer derivation behavior out of `DerivationService` into a
  dedicated composed service.
- R2. Preserve `DerivationService` compatibility methods for all existing
  orchestrator wrappers and tests.
- R3. Preserve `MemoryOrchestrator` wrapper behavior and names.
- R4. Keep deferred derive completion, record update, embedding, CortexFS write,
  and projection sync orchestration in `DerivationService`.
- R5. Preserve current behavior for direct derive, chunked derive, no-LLM
  fallback, retryable LLM failures, parent summary derivation, memory write
  derive, update mutation derive, document async derive, and context-manager
  derive tests.
- R6. Do not change prompt text, parse contracts, derived payload shapes, or
  public memory API behavior.

## Scope Boundaries

- Do not change store, update, recall, or HTTP contracts.
- Do not remove existing compatibility wrappers in `MemoryOrchestrator` or
  `DerivationService`.
- Do not change derive queue lifecycle or background worker scheduling.
- Do not alter anchor/fact projection persistence; that remains delegated to
  `MemoryRecordService` through existing `DerivationService` helpers.
- Do not introduce plugin/config behavior in this pass.
- Do not refactor recomposition or benchmark ingest classes in this pass.

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/services/derivation_service.py` currently owns both pure
  LLM derivation helpers and deferred derive completion.
- `src/opencortex/orchestrator.py` exposes thin compatibility wrappers around
  derivation methods and should continue to do so.
- `src/opencortex/services/memory_write_derive_service.py` and
  `src/opencortex/services/memory_mutation_service.py` call
  `orch._derive_layers`, so wrapper behavior must remain stable.
- `src/opencortex/lifecycle/background_tasks.py`,
  `src/opencortex/context/recomposition_engine.py`, and
  `src/opencortex/context/benchmark_ingest_service.py` call parent-summary or
  deferred-derive wrappers through the orchestrator.
- Existing extracted services use composition while leaving orchestrator
  wrappers in place, for example recent memory write, recall, retrieval, and
  record/projection service splits.

### Institutional Learnings

- `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md`
  reinforces keeping memory hot-path phases explicit and avoiding legacy drift
  across orchestration layers.

### External References

- External research is intentionally skipped. This is an internal
  behavior-preserving service extraction following existing project patterns.

## Key Technical Decisions

- Create `MemoryLayerDerivationService` under `src/opencortex/services/`:
  this keeps LLM derive algorithm code beside the other memory services without
  creating a new package boundary.
- Compose the new service inside `DerivationService`: this keeps
  `DerivationService` as the deferred derive coordinator and avoids making the
  orchestrator a service bus for internal derive algorithm calls.
- Keep compatibility wrappers in `DerivationService`: existing tests and
  orchestrator wrappers can continue to call private derive helpers while the
  actual logic lives in the new service.
- Preserve orchestrator-level LLM completion override behavior: tests currently
  patch `orch._derive_layers_llm_completion`, so the new service must continue
  honoring that override path.

## Open Questions

### Resolved During Planning

- Should `MemoryLayerDerivationService` own deferred derive completion?
  Resolution: no. Deferred completion performs storage mutation, embedding,
  CortexFS writes, and projection sync, so it remains in `DerivationService`.
- Should orchestrator wrappers be removed now?
  Resolution: no. The task explicitly keeps wrappers stable.

### Deferred to Implementation

- Exact constructor shape for `MemoryLayerDerivationService`: prefer passing the
  orchestrator if it keeps override behavior simple; pass narrower callables
  only if implementation stays readable.

## Output Structure

    src/opencortex/services/
    ├── derivation_service.py
    └── memory_layer_derivation_service.py

## Implementation Units

- U1. **Create MemoryLayerDerivationService**

  **Goal:** Add a focused service for pure LLM-backed layer derivation.

  **Requirements:** R1, R5, R6

  **Dependencies:** None

  **Files:**
  - Create: `src/opencortex/services/memory_layer_derivation_service.py`
  - Modify: `src/opencortex/services/derivation_service.py`

  **Approach:**
  - Move `_derive_parent_summary`, `_derive_layers`,
    `_derive_layers_split_fields`, `_coerce_derived_string`,
    `_coerce_derived_list`, `_fallback_overview_from_content`,
    `_is_retryable_layer_derivation_error`, `_derive_layers_llm_completion`,
    and `_derive_abstract_from_overview` into the new service.
  - Preserve imports for prompt builders, JSON parsing, chunked derive,
    smart truncation, retry handling, and logging in the new module.
  - Keep output shapes exactly the same, including comma-separated keyword
    strings and list limits for entities, anchor handles, and fact points.

  **Patterns to follow:**
  - `src/opencortex/services/memory_write_derive_service.py` for focused write
    sub-service shape.
  - `src/opencortex/services/derivation_service.py` for existing derive
    behavior and fallback semantics.

  **Test scenarios:**
  - Happy path: direct derive returns abstract, overview, keywords, entities,
    anchor handles, and fact points unchanged.
  - Edge case: chunked derive still falls back to content-derived abstract when
    LLM output has a blank abstract.
  - Error path: retryable HTTP/transport failures are retried with the same
    bounded budget.
  - Edge case: no-LLM fallback still derives overview and abstract from content.

  **Verification:**
  - Targeted derive tests pass with unchanged expected values.

- U2. **Keep DerivationService as Coordinator and Wrapper**

  **Goal:** Make `DerivationService` delegate pure derive calls to
  `MemoryLayerDerivationService` while retaining deferred derive persistence.

  **Requirements:** R2, R3, R4, R5

  **Dependencies:** U1

  **Files:**
  - Modify: `src/opencortex/services/derivation_service.py`
  - Modify: `src/opencortex/orchestrator.py` only if type imports or comments
    need adjustment.

  **Approach:**
  - Add a lazy `_layer_derivation_service` property or initialize the service in
    `DerivationService.__init__`.
  - Replace the moved methods in `DerivationService` with thin delegates.
  - Keep `_complete_deferred_derive` calling `self._derive_layers(...)` so
    existing override and wrapper semantics remain centered in
    `DerivationService`.
  - Leave record/projection helper delegates untouched.

  **Patterns to follow:**
  - `src/opencortex/orchestrator.py` compatibility wrapper style.
  - `src/opencortex/services/retrieval_service.py` delegating to extracted
    focused services.

  **Test scenarios:**
  - Integration: `MemoryOrchestrator._derive_layers` still delegates through
    `DerivationService` and returns the same payload.
  - Integration: `_complete_deferred_derive` still writes updated abstract,
    overview, keywords, vectors, CortexFS content, and projection records.
  - Compatibility: tests that patch `orch._derive_layers_llm_completion` still
    influence derive calls.

  **Verification:**
  - Document async derive and context-manager derive tests pass.

- U3. **Update Imports, Docstrings, and Focused Tests**

  **Goal:** Remove stale wording and add focused coverage for the new service
  boundary if existing tests do not directly exercise it.

  **Requirements:** R1, R2, R5, R6

  **Dependencies:** U2

  **Files:**
  - Modify: `src/opencortex/services/derivation_service.py`
  - Create or modify: `tests/test_memory_layer_derivation_service.py`
  - Modify existing tests only when imports or direct service names require it.

  **Approach:**
  - Keep public behavior tests primarily on the orchestrator wrapper path.
  - Add a narrow test for `MemoryLayerDerivationService` only if it clarifies
    the new boundary without duplicating broad context-manager tests.
  - Remove unused imports from `derivation_service.py`.

  **Patterns to follow:**
  - `tests/test_memory_write_derive_service.py` for small service-level tests.
  - `tests/test_recall_planner.py` and `tests/test_context_manager.py` for
    current derive behavior assertions.

  **Test scenarios:**
  - Import path: both services import without circular dependency.
  - Wrapper path: orchestrator and `DerivationService` wrappers remain callable.
  - Static helpers: fallback overview, abstract derivation, and coercion helpers
    remain accessible via compatibility wrappers.

  **Verification:**
  - Ruff and targeted tests pass.

## System-Wide Impact

- **Interaction graph:** Memory write derive, mutation update derive, document
  async derive, recomposition parent summaries, and benchmark deferred derive
  continue to enter through existing orchestrator wrappers.
- **Error propagation:** LLM derive failures and retryable transient errors
  should follow the same catch/log/fallback behavior.
- **State lifecycle risks:** Deferred derive counter, storage update, projection
  sync, and CortexFS writes remain in `DerivationService`.
- **API surface parity:** `/api/v1/memory/store`, `/api/v1/memory/search`, and
  existing Python compatibility wrappers should not change.
- **Integration coverage:** Context-manager derive retry tests, document async
  derive tests, and update fact-point regression tests cover the cross-layer
  behavior.
- **Unchanged invariants:** Prompt text, result schema, keyword string format,
  entity lowercasing, anchor/fact limits, and fallback truncation stay the same.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Moving methods breaks tests that patch `orch._derive_layers_llm_completion`. | Preserve override lookup in the new service or delegate through a wrapper that still checks the orchestrator instance dictionary. |
| Static helper compatibility drifts. | Keep static/class wrappers in `DerivationService` and `MemoryOrchestrator` delegating to the new service. |
| Deferred derive accidentally bypasses new behavior or double-wraps fallback logic. | Keep `_complete_deferred_derive` calling `self._derive_layers` and validate with document async derive tests. |
| Import cycles appear between orchestrator, derivation, and the new service. | Use `TYPE_CHECKING` imports and pass the existing orchestrator reference lazily. |

## Documentation / Operational Notes

- No README or API documentation updates are expected because this is an
  internal service-boundary refactor.
- PR description should call out that public wrappers are intentionally
  preserved.

## Sources & References

- Related plan: `docs/plans/2026-04-28-049-refactor-derivation-record-service-deps-plan.md`
- Related code: `src/opencortex/services/derivation_service.py`
- Related code: `src/opencortex/orchestrator.py`
- Related code: `src/opencortex/services/memory_write_derive_service.py`
- Related code: `src/opencortex/services/memory_mutation_service.py`
- Related tests: `tests/test_recall_planner.py`
- Related tests: `tests/test_context_manager.py`
- Related tests: `tests/test_document_async_derive.py`
- Institutional learning: `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md`
