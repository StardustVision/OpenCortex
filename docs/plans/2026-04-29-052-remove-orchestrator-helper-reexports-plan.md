---
title: refactor: Remove Orchestrator Helper Re-exports
type: refactor
status: active
date: 2026-04-29
---

# refactor: Remove Orchestrator Helper Re-exports

## Summary

Remove the remaining helper-function re-export dependency from
`MemoryOrchestrator`. Callers that need `_merge_unique_strings` or
`_split_keyword_string` should import them from their current implementation
module instead of forcing `opencortex.orchestrator` to act as a utility export
surface.

## Problem Frame

After extracting the orchestrator service registry, `orchestrator.py` is closer
to a facade, but it still imports `_merge_unique_strings` and
`_split_keyword_string` from `derivation_service` only to satisfy downstream
imports from `opencortex.orchestrator`. The live code scan found
`memory_write_context_builder.py` still importing those helpers from the
orchestrator. That keeps a stale service-bus dependency alive and makes the
facade look like the canonical owner of generic helper functions.

## Requirements

- R1. Replace remaining imports of `_merge_unique_strings` /
  `_split_keyword_string` from `opencortex.orchestrator` with direct imports
  from the helpers' actual implementation module or an equivalent local
  utility.
- R2. Remove the helper re-export import from `src/opencortex/orchestrator.py`.
- R3. Preserve store, document async derive, and update behavior.
- R4. Keep public `MemoryOrchestrator` API and compatibility wrappers
  unchanged.
- R5. Do not move or rewrite helper implementations unless required for the
  import cleanup.

## Scope Boundaries

- Do not remove `MemoryOrchestrator` itself or its compatibility wrapper
  methods.
- Do not change helper semantics, ordering, normalization, or keyword splitting.
- Do not refactor unrelated direct imports of `MemoryOrchestrator` used for
  type checking or public API tests.
- Do not change store/derive/update payload shapes.
- Do not touch recomposition or benchmark ingest helper duplication in this
  pass.

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/orchestrator.py` currently imports
  `_merge_unique_strings` and `_split_keyword_string` from
  `src/opencortex/services/derivation_service.py` for re-export compatibility.
- `src/opencortex/services/memory_write_context_builder.py` imports those
  helpers from `opencortex.orchestrator` inside `resolve_target` and
  `build_derived_fields`.
- Other current usages already import from `derivation_service` directly or
  use local helper methods.
- `tests/test_document_async_derive.py` and
  `tests/test_e2e_phase1.py::TestE2EPhase1::test_12_update` previously caught
  this exact import compatibility issue when the re-export was removed too
  early.

### Institutional Learnings

- Current-state store-chain guidance says to trace live code paths rather than
  assuming docs or prior PR state. This plan is based on a fresh `rg` scan of
  helper imports.

### External References

- External research is intentionally skipped. This is a small internal import
  boundary cleanup.

## Key Technical Decisions

- Import directly from `opencortex.services.derivation_service` in
  `memory_write_context_builder.py`: this is the current implementation module
  already used by other services, and avoids a broader utility move.
- Keep helper implementation in `derivation_service.py` for this pass:
  extracting a new generic utility module would touch many consumers and is not
  required to remove the orchestrator re-export.
- Add or rely on targeted regressions that exercise store, document async
  derive, and update paths where the stale import previously failed.

## Open Questions

### Resolved During Planning

- Are there multiple active imports from `opencortex.orchestrator` for these
  helpers?
  Resolution: the live scan found the stale helper imports in
  `memory_write_context_builder.py`; other helper usages already point at
  `derivation_service` or local methods.

### Deferred to Implementation

- If lint reveals another helper re-export usage missed by the initial scan,
  update that caller in the same pattern.

## Implementation Units

- U1. **Update Helper Imports in Write Context Builder**

  **Goal:** Stop importing helper functions through `opencortex.orchestrator`.

  **Requirements:** R1, R3, R5

  **Dependencies:** None

  **Files:**
  - Modify: `src/opencortex/services/memory_write_context_builder.py`

  **Approach:**
  - Replace local imports from `opencortex.orchestrator` with direct imports
    from `opencortex.services.derivation_service`.
  - Keep local import style if it best avoids cycles, or move to module-level
    imports if lint and import behavior stay clean.

  **Patterns to follow:**
  - `src/opencortex/services/memory_mutation_service.py` imports these helpers
    directly from `derivation_service`.
  - `src/opencortex/services/session_lifecycle_service.py` uses direct helper
    imports from `derivation_service`.

  **Test scenarios:**
  - Happy path: store with explicit entities/topics still merges metadata in
    the same order.
  - Integration: document async derive no longer needs orchestrator helper
    re-exports.
  - Integration: update path still preserves derived keywords/entities.

  **Verification:**
  - Targeted store/document/update tests pass.

- U2. **Remove Orchestrator Re-export Imports**

  **Goal:** Make `orchestrator.py` stop exporting derivation helper functions.

  **Requirements:** R2, R4

  **Dependencies:** U1

  **Files:**
  - Modify: `src/opencortex/orchestrator.py`

  **Approach:**
  - Remove `_merge_unique_strings` and `_split_keyword_string` imports from
    `orchestrator.py`.
  - Run `rg` to confirm no code imports those helpers from
    `opencortex.orchestrator`.
  - Preserve normal `MemoryOrchestrator` imports and public API exports.

  **Patterns to follow:**
  - Previous facade cleanup in
    `docs/plans/2026-04-29-051-refactor-orchestrator-service-registry-plan.md`.

  **Test scenarios:**
  - Importing `opencortex.orchestrator` still works.
  - No source file imports helper functions from `opencortex.orchestrator`.

  **Verification:**
  - Ruff and import scans pass.

- U3. **Run Focused Regressions**

  **Goal:** Confirm behavior did not drift after import-boundary cleanup.

  **Requirements:** R3, R4

  **Dependencies:** U1, U2

  **Files:** no expected source changes beyond fixes.

  **Approach:**
  - Run the tests that previously exposed the stale re-export dependency.
  - Run memory write context builder or adjacent service tests if available.

  **Test scenarios:**
  - Store path creates a memory without ImportError.
  - Document async derive drains and completes.
  - Update path still re-embeds and preserves derived fields.

  **Verification:**
  - `tests/test_document_async_derive.py`
  - `tests/test_e2e_phase1.py::TestE2EPhase1::test_12_update`
  - Ruff checks

## System-Wide Impact

- **Interaction graph:** Store/write context building depends directly on the
  helper implementation module instead of the top-level orchestrator facade.
- **Error propagation:** No error behavior changes are expected.
- **State lifecycle risks:** None expected; no stateful logic moves.
- **API surface parity:** `MemoryOrchestrator` public API remains unchanged.
- **Integration coverage:** Store/document/update tests cover the changed import
  path through real write flows.
- **Unchanged invariants:** Helper behavior, metadata merge order, keyword
  splitting, and derived payload shape remain unchanged.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| A hidden caller still imports helpers from `opencortex.orchestrator`. | Run `rg` over `src` and `tests`; targeted document/update tests catch runtime import failures. |
| Direct import from `derivation_service` introduces an import cycle. | Keep imports local inside methods if needed, matching the existing lazy import style. |
| Helper ownership remains imperfect because helpers are generic. | Defer broader utility extraction; this pass only removes orchestrator as a false owner. |

## Documentation / Operational Notes

- No README/API documentation updates are expected. This is an internal import
  boundary cleanup.

## Sources & References

- Related code: `src/opencortex/orchestrator.py`
- Related code: `src/opencortex/services/memory_write_context_builder.py`
- Related code: `src/opencortex/services/derivation_service.py`
- Related code: `src/opencortex/services/memory_mutation_service.py`
- Related tests: `tests/test_document_async_derive.py`
- Related tests: `tests/test_e2e_phase1.py`
- Related plan: `docs/plans/2026-04-29-051-refactor-orchestrator-service-registry-plan.md`
