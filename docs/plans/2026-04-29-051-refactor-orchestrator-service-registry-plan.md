---
title: refactor: Extract Orchestrator Service Registry
type: refactor
status: completed
date: 2026-04-29
---

# refactor: Extract Orchestrator Service Registry

## Summary

Extract `MemoryOrchestrator` lazy service construction into a focused
`MemoryOrchestratorServices` registry. `MemoryOrchestrator` keeps the same
public API and compatibility wrappers, but no longer owns the repeated
service-instance slots and lazy-construction boilerplate directly.

## Problem Frame

The store, recall, derive, mutation, projection, session, and status domains
have been split into focused services. `src/opencortex/orchestrator.py` is now
mostly a facade, but it still contains a large service-registry block:
instance cache fields in `__init__` plus many `_x_service` lazy properties.
That keeps the facade physically large and makes it look like the orchestrator
is still the service bus rather than a composition root with delegated APIs.

## Requirements

- R1. Add a `MemoryOrchestratorServices` helper that owns lazy construction and
  caching for orchestrator-owned services.
- R2. Move lazy construction for `_memory_service`, `_derivation_service`,
  `_retrieval_service`, `_session_lifecycle_service`,
  `_memory_record_service`, `_model_runtime_service`,
  `_memory_sharing_service`, `_memory_admin_stats_service`,
  `_knowledge_service`, `_system_status_service`,
  `_background_task_manager`, and `_bootstrapper` into the registry.
- R3. Preserve all `MemoryOrchestrator` property names and wrapper behavior so
  existing code and tests keep calling the same attributes.
- R4. Preserve `MemoryOrchestrator.__new__` bypass behavior used by tests:
  accessing a delegated service on a partially constructed orchestrator should
  still lazily create what it needs instead of raising missing-attribute errors.
- R5. Do not change public memory, recall, derive, session, knowledge, status,
  or lifecycle behavior.
- R6. Keep the change behavior-preserving and scoped to facade structure.

## Scope Boundaries

- Do not remove compatibility wrappers from `MemoryOrchestrator`.
- Do not move store/recall/derive/session business logic in this pass.
- Do not change service constructors or add new runtime configuration.
- Do not refactor `close()`, `init()`, bootstrap internals, or background task
  lifecycle beyond adapting them to the registry.
- Do not change HTTP/API contracts or benchmark behavior.

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/orchestrator.py` currently declares service-instance cache
  fields in `__init__` and implements each lazy service property inline.
- Existing service splits use composition with the orchestrator passed as a
  narrow back-reference while wrappers remain in place.
- `tests/test_perf_fixes.py` and similar fixtures historically bypass
  `MemoryOrchestrator.__init__`, so lazy access must use `getattr` defaults.
- Recent plans through
  `docs/plans/2026-04-29-050-refactor-layer-derivation-service-plan.md`
  reduced store/recall/derive services to focused modules; this plan targets
  facade structure, not domain logic.

### Institutional Learnings

- `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md`
  reinforces keeping memory hot-path responsibilities explicit and avoiding
  drift across orchestration layers.

### External References

- External research is intentionally skipped. This is an internal structural
  cleanup following established local service-extraction patterns.

## Key Technical Decisions

- Create `MemoryOrchestratorServices` in
  `src/opencortex/services/orchestrator_services.py`: this keeps registry code
  near the service layer and avoids adding a new top-level package.
- Store the registry on `MemoryOrchestrator` as `_services`: this makes service
  ownership explicit while keeping compatibility properties on the orchestrator.
- Use a small generic helper inside the registry for lazy construction:
  one place should handle `getattr` cache lookup, import, construction, and
  cache assignment.
- Keep orchestrator properties as thin delegates:
  `return self._services.memory_service`, preserving current attribute names
  used by wrappers and tests.

## Open Questions

### Resolved During Planning

- Should public wrapper methods move into the registry?
  Resolution: no. The registry only owns service construction and caching.
- Should `MemoryOrchestrator.__init__` eagerly construct the registry?
  Resolution: yes, but `_services` access must still lazily create the registry
  when tests bypass `__init__`.

### Deferred to Implementation

- Exact import style for service classes: prefer local imports inside registry
  methods to preserve current import-cycle behavior.

## Output Structure

    src/opencortex/services/
    ├── orchestrator_services.py
    └── ...

## Implementation Units

- U1. **Add MemoryOrchestratorServices Registry**

  **Goal:** Introduce a focused registry that owns lazy service construction.

  **Requirements:** R1, R2, R4

  **Dependencies:** None

  **Files:**
  - Create: `src/opencortex/services/orchestrator_services.py`
  - Modify: `src/opencortex/orchestrator.py`

  **Approach:**
  - Define `MemoryOrchestratorServices` with an orchestrator reference.
  - Add one lazy property per service listed in R2.
  - Use local imports inside each property or a helper closure to match current
    circular-import avoidance.
  - Preserve cache attribute names where practical so external tests that
    inspect or assign `_memory_service_instance` continue to work.

  **Patterns to follow:**
  - Current lazy property implementations in `src/opencortex/orchestrator.py`.
  - Existing composition style in `src/opencortex/services/derivation_service.py`
    and `src/opencortex/services/retrieval_service.py`.

  **Test scenarios:**
  - Happy path: a normal orchestrator returns the same singleton service
    instance across repeated property access.
  - Edge case: an orchestrator created with `MemoryOrchestrator.__new__` can
    still access `_memory_service` and `_system_status_service`.
  - Integration: registry construction does not import service modules at
    orchestrator module import time.

  **Verification:**
  - Focused registry tests and ruff pass.

- U2. **Delegate Orchestrator Lazy Properties to Registry**

  **Goal:** Remove inline service-construction boilerplate from
  `MemoryOrchestrator` while preserving property names.

  **Requirements:** R2, R3, R4, R5

  **Dependencies:** U1

  **Files:**
  - Modify: `src/opencortex/orchestrator.py`

  **Approach:**
  - Add a `_services` property that lazily returns
    `MemoryOrchestratorServices(self)`.
  - Replace each orchestrator `_x_service` property body with a one-line
    registry delegate.
  - Remove service-instance cache initialization from `__init__` after the
    registry owns equivalent cache behavior.
  - Keep public API methods and compatibility wrapper bodies unchanged.

  **Patterns to follow:**
  - Existing orchestrator compatibility wrappers for derive, retrieval, record,
    status, and lifecycle methods.

  **Test scenarios:**
  - Store path still delegates through `MemoryService`.
  - Recall/search path still delegates through `MemoryService` /
    `MemoryQueryService` and retrieval services.
  - Derive wrappers still reach `DerivationService`.
  - Session/status wrappers still reach lifecycle and status services.

  **Verification:**
  - Store, recall, derive, session, and status targeted tests pass.

- U3. **Add Regression Coverage for Registry Semantics**

  **Goal:** Lock in the facade-registry boundary so future cleanup does not
  reintroduce inline service-bus boilerplate or break `__new__` bypass tests.

  **Requirements:** R3, R4, R5

  **Dependencies:** U2

  **Files:**
  - Create or modify: `tests/test_orchestrator_services.py`

  **Approach:**
  - Test singleton caching for at least one core service.
  - Test `MemoryOrchestrator.__new__` lazy registry creation.
  - Keep tests narrow and structural; rely on existing domain tests for behavior.

  **Patterns to follow:**
  - `tests/test_memory_layer_derivation_service.py` for small service-boundary
    tests.
  - Existing orchestrator wrapper tests for behavior coverage.

  **Test scenarios:**
  - Edge case: `_services` property exists even when `__init__` was skipped.
  - Happy path: `_memory_service` returns the same object on repeated access.
  - Compatibility: assigning an existing `_memory_service_instance` is honored
    by the registry.

  **Verification:**
  - New test file passes.

## System-Wide Impact

- **Interaction graph:** Public callers still enter through
  `MemoryOrchestrator`; service construction moves behind a registry.
- **Error propagation:** No behavioral error propagation changes are expected.
- **State lifecycle risks:** The main risk is cache identity drift; preserving
  existing cache attribute names mitigates it.
- **API surface parity:** Public API methods, private compatibility wrapper
  names, and service property names remain unchanged.
- **Integration coverage:** Existing store, recall, derive, session, status,
  and lifecycle tests cover behavior above the registry.
- **Unchanged invariants:** Service constructors still receive the orchestrator
  back-reference and lazy import behavior remains local.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Import cycles appear if registry imports services at module import time. | Keep service imports inside property methods/helper callables. |
| Tests that bypass `__init__` fail because `_services` is missing. | Implement `_services` as a `getattr`-guarded lazy property. |
| Existing tests or users assign `_memory_service_instance` directly. | Preserve cache attribute names in the registry helper. |
| Facade cleanup accidentally changes public wrapper behavior. | Do not edit wrapper bodies except service-property delegation and run targeted regressions. |

## Documentation / Operational Notes

- No README/API docs are expected because this is an internal facade cleanup.
- PR description should call out that public API and compatibility wrappers are
  intentionally unchanged.

## Sources & References

- Related code: `src/opencortex/orchestrator.py`
- Related code: `src/opencortex/services/memory_service.py`
- Related code: `src/opencortex/services/derivation_service.py`
- Related code: `src/opencortex/services/retrieval_service.py`
- Related code: `src/opencortex/services/session_lifecycle_service.py`
- Related tests: `tests/test_memory_layer_derivation_service.py`
- Related tests: `tests/test_memory_write_derive_service.py`
- Related tests: `tests/test_recall_planner.py`
- Related tests: `tests/test_document_async_derive.py`
- Institutional learning: `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md`
