---
status: completed
created: 2026-05-04
origin: user request
scope: active code naming cleanup after CortexMemory rename
---

# CortexMemory Active Naming Cleanup

## Problem

`CortexMemory` is now the canonical memory facade, with
`MemoryOrchestrator` preserved as a compatibility alias. Active code still has
comments/docstrings that describe the current primary entrypoint as
`MemoryOrchestrator`, "orchestrator", or "orchestrator-owned" even when the
runtime object is now `CortexMemory`.

Those stale labels make the cleaned architecture harder to read and invite new
code to use the old name.

## Scope

In scope:

- Update active code comments/docstrings in:
  - `src/opencortex/http/`
  - `src/opencortex/context/`
  - `src/opencortex/insights/`
  - `src/opencortex/services/`
  - `src/opencortex/lifecycle/`
  - other active `src/opencortex/` modules when the mention clearly refers to
    the current memory facade.
- Prefer `CortexMemory` or "memory facade" for the current canonical entrypoint.
- Keep behavior unchanged.

Out of scope:

- Renaming `_orchestrator` variables in HTTP/server or active code.
- Renaming the compatibility module `opencortex.orchestrator`.
- Removing `MemoryOrchestrator` or `MemoryOrchestratorServices` aliases.
- Editing historical docs under `docs/plans/`, `docs/refactor/`, archival
  design docs, or residual review docs.
- Mechanical full-repo text replacement.

## Implementation Units

### 1. Active-Code Residual Scan

Use `rg` over `src/opencortex/` only, excluding compatibility modules where the
old name is intentional:

- `src/opencortex/orchestrator.py`
- `src/opencortex/services/orchestrator_services.py`

Scan for:

- `MemoryOrchestrator`
- `orchestrator-owned`
- `orchestrator instance`
- comments/docstrings that describe the current entrypoint as "the
  orchestrator".

### 2. Targeted Text Cleanup

Edit only docstrings and comments. Do not change function names, public
signatures, variables, payload fields, or test behavior.

Examples:

- "orchestrator-owned subsystems" -> "CortexMemory-owned subsystems"
- "MemoryOrchestrator instance" -> "CortexMemory instance"
- "orchestrator statistics" -> "memory facade statistics"

### 3. Compatibility Guard

Confirm the intentional old-name surfaces remain:

- `opencortex.orchestrator`
- `MemoryOrchestrator = CortexMemory`
- `MemoryOrchestratorServices = CortexMemoryServices`
- `_orchestrator` variable names in HTTP/server.

## Test Plan

Because this is comment/docstring-only cleanup, run fast static and compatibility
checks:

- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`
- `uv run --group dev pytest tests/test_orchestrator_services.py -q`
- `uv run --group dev pytest tests/test_http_server.py -q`

LFG checks:

- `ce-code-review mode:autofix plan:docs/plans/2026-05-04-059-cortex-memory-active-naming-cleanup-plan.md`
- `ce-test-browser mode:pipeline`
- Commit, push, and open PR.

## Risks

- Blind replacement can break compatibility language around the intentional old
  alias. Keep compatibility modules explicit.
- Renaming variables would create noisy diffs without improving behavior in this
  pass. Leave them alone.
