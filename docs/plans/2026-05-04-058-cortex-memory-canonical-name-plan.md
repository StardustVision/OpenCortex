---
status: completed
created: 2026-05-04
origin: user request
scope: canonical CortexMemory name with MemoryOrchestrator compatibility
---

# CortexMemory Canonical Name

## Problem

`MemoryOrchestrator` is no longer an accurate or desirable name for the central
memory facade. Most domain logic has moved into focused services; the remaining
class is the stable memory subsystem entrypoint that wires storage, retrieval,
derive, session lifecycle, and admin operations.

The new canonical name should be `CortexMemory`. The old
`MemoryOrchestrator` name must continue to work for external users, tests, and
`__new__` bypass compatibility.

## Scope

In scope:

- Introduce `CortexMemory` as the canonical class/import path.
- Preserve `MemoryOrchestrator` as a deprecated compatibility alias or shim.
- Preserve `MemoryOrchestrator.__new__(MemoryOrchestrator)` bypass behavior.
- Update internal code, HTTP/server initialization, and tests to prefer
  `CortexMemory` where this is low-risk.
- Keep `src/opencortex/orchestrator.py` as a compatibility export; do not delete
  it in this pass.
- Keep public API behavior and method names unchanged except for the new class
  name/import path.

Out of scope:

- Removing `MemoryOrchestrator` compatibility import.
- Renaming every local variable named `orch` in service internals.
- Moving service ownership boundaries.
- Changing storage, retrieval, derive, session, or admin behavior.

## Implementation Strategy

### 1. Reference Audit

Use `rg` to identify:

- Imports of `MemoryOrchestrator`.
- Imports of `opencortex.orchestrator`.
- Runtime construction sites.
- Tests that instantiate via `MemoryOrchestrator.__new__(MemoryOrchestrator)`.

### 2. Canonical Class Path

Prefer the smallest compatibility-safe structure:

- Move the canonical class implementation to `src/opencortex/cortex_memory.py`
  as `class CortexMemory`.
- Keep `src/opencortex/orchestrator.py` as a thin compatibility module:
  `MemoryOrchestrator = CortexMemory`.
- Avoid circular imports by updating `TYPE_CHECKING` imports and internal
  runtime imports carefully.

If a full file move creates too much risk, fall back to defining
`CortexMemory` in `orchestrator.py` and aliasing `MemoryOrchestrator =
CortexMemory`; however, the preferred outcome is a new canonical module.

### 3. Internal Import Updates

Update low-risk internal call sites to import and construct `CortexMemory`,
especially:

- HTTP/server startup.
- Test fixtures.
- Type-checking imports in service modules.
- Any direct docs/examples touched by the import path.

Leave external compatibility untouched by keeping `opencortex.orchestrator` and
`MemoryOrchestrator` valid.

### 4. Compatibility Tests

Ensure both paths work:

- `CortexMemory(...)`
- `MemoryOrchestrator(...)`
- `MemoryOrchestrator.__new__(MemoryOrchestrator)` with lazy service access.

## Test Plan

Focused tests:

- `uv run --group dev pytest tests/test_orchestrator_services.py -q`
- `uv run --group dev pytest tests/test_e2e_phase1.py::TestE2EPhase1::test_12_update -q`
- `uv run --group dev pytest tests/test_memory_service.py tests/test_memory_recall_pipeline_service.py -q`
- `uv run --group dev pytest tests/test_http_server.py -q` if present.

Static checks:

- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`

LFG checks:

- `ce-code-review mode:autofix plan:docs/plans/2026-05-04-058-cortex-memory-canonical-name-plan.md`
- `ce-test-browser mode:pipeline`
- Commit, push, and open PR.

## Risks

- Hidden imports may still depend on `opencortex.orchestrator`. Keep the module
  as a compatibility export.
- `__new__` bypass tests depend on legacy cache slot behavior. Alias/subclass
  choices must preserve this path.
- Over-eager variable renames can create noise and merge risk. Keep variable
  cleanup out of this pass unless needed for import clarity.
