---
title: "refactor: Extract KnowledgeService from MemoryOrchestrator (Phase 2)"
type: refactor
status: active
date: 2026-04-26
origin: docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md
---

# refactor: Extract KnowledgeService from MemoryOrchestrator (Phase 2)

## Overview

Phase 2 of the 8-phase God Object decomposition. Extracts the knowledge management surface (6 public methods + 1 private helper, ~166 lines) from `MemoryOrchestrator` into a dedicated `KnowledgeService` at `src/opencortex/services/knowledge_service.py`. Same MOVE-not-REWRITE discipline as Phase 1 — method bodies land verbatim, orchestrator keeps thin delegate stubs, all existing tests pass unchanged.

---

## Problem Frame

Plan 010 (origin document) defines the full decomposition roadmap. Phase 1 (MemoryService, plans 010+011, PRs #16+#17) moved ~1480 lines. Phase 2 targets the knowledge/archivist surface: a self-contained cluster of methods that delegate to `KnowledgeStore`, `Archivist`, and `Sandbox`. The orchestrator is 4807 lines post-Phase-1; this extraction removes ~166 lines of method bodies (replaced by one-line delegates), advancing the decomposition goal.

---

## Requirements Trace

- R1. `KnowledgeService` follows the established back-reference pattern: `KnowledgeService(orchestrator)` with `self._orch` access to subsystems. (origin: plan 010 Phase 2 spec)
- R2. Every public method on `MemoryOrchestrator` that is extracted (`knowledge_search`, `knowledge_approve`, `knowledge_reject`, `knowledge_list_candidates`, `archivist_trigger`, `archivist_status`) keeps its exact name + signature + behavior. External callers (HTTP routes, ContextManager, tests) see zero contract change.
- R3. `_run_archivist` private helper moves to `KnowledgeService` and is accessible from `session_end` on the orchestrator. (origin: plan 010, "helper methods coupled to them")
- R4. `KnowledgeService` follows Google Python Style: docstrings with Args/Returns/Raises on every public method, full type hints, `[KnowledgeService]` logger prefixes, UPPER_SNAKE constants.
- R5. All existing tests pass without modification. Only NEW tests for `KnowledgeService` surface are allowed.

---

## Scope Boundaries

- This is a MOVE, not a REWRITE. Method bodies land verbatim with only `self._X` → `self._orch._X` renames.
- No changes to `KnowledgeStore`, `Archivist`, `Sandbox`, or any `alpha/` module.
- No changes to HTTP routes (`server.py`) or `ContextManager` — they call through orchestrator delegators which remain.
- No changes to `session_end`'s archivist gating logic — only the call target changes from `self._run_archivist(...)` to `self._knowledge_service.run_archivist(...)`.
- Attribute ownership: `_knowledge_store`, `_archivist`, `_trace_store` remain on the orchestrator (booted in `init()`). KnowledgeService accesses them via `self._orch._knowledge_store` etc.

### Deferred to Follow-Up Work

- Phase 3: Extract `BackgroundTaskManager` (origin: plan 010)
- Phase 4: Extract `SystemStatusService` (origin: plan 010)
- Phase 5: Extract `SubsystemBootstrapper` (origin: plan 010)
- Phase 6: Style sweep across remaining orchestrator methods (origin: plan 010)

---

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/services/memory_service.py` — the validated extraction pattern: `self._orch` back-reference, `self._orch._X` subsystem access, one-line delegates on orchestrator via lazy `@property`
- `src/opencortex/orchestrator.py` lines 2611-2630 — lazy `_memory_service` property pattern to mirror for `_knowledge_service`
- `src/opencortex/alpha/knowledge_store.py` — `KnowledgeStore` interface (save, search, approve, reject, list_candidates)
- `src/opencortex/alpha/archivist.py` — `Archivist` interface (run, status, should_trigger)
- `src/opencortex/alpha/sandbox.py` — pure functions (`evaluate`, `stat_gate`, `llm_verify`), no stateful class

### Institutional Learnings

- **Back-reference pattern validated** across 3 successful extractions (BenchmarkConversationIngestService, MemoryService PR #16, MemoryService PR #17). Constructor stores `self._orch`, methods access `self._orch._storage` etc. (plan 010, plan 011)
- **Helper classification**: private helpers only called by moved methods relocate; shared helpers stay on orchestrator accessed via `self._orch._helper()`. (plan 011)
- **Lazy property**: use `@property` with `getattr` guard for service instance, not eager init. Handles tests that bypass `__init__` via `__new__`. (plan 011 actual implementation)
- **Static method promotion**: helpers that don't use `self` become `@staticmethod` on the service. (plan 011)

---

## Key Technical Decisions

- **`_run_archivist` becomes public `run_archivist`**: This private helper is called by both `archivist_trigger` (within the service) and `session_end` (on the orchestrator). Making it public on the service avoids the convention violation of calling a private method cross-service. The orchestrator's `session_end` calls `self._knowledge_service.run_archivist(tid, uid)`.
- **No attribute ownership transfer**: `_knowledge_store`, `_archivist`, `_trace_store` stay on the orchestrator. They're booted in `init()` which depends on config-driven conditional logic. Phase 5 (SubsystemBootstrapper) will eventually own boot.
- **Lazy property mirroring MemoryService**: `@property _knowledge_service` on the orchestrator with `getattr` guard, same pattern as `_memory_service`.

---

## Method Inventory

### Methods to extract (all from `orchestrator.py`)

| Method | Lines | Role | `self._X` accessed |
|--------|-------|------|---------------------|
| `knowledge_search` | 4210-4228 | Public API | `_ensure_init`, `_knowledge_store` |
| `knowledge_approve` | 4230-4240 | Public API | `_ensure_init`, `_knowledge_store` |
| `knowledge_reject` | 4242-4252 | Public API | `_ensure_init`, `_knowledge_store` |
| `knowledge_list_candidates` | 4254-4261 | Public API | `_ensure_init`, `_knowledge_store` |
| `archivist_trigger` | 4263-4270 | Public API | `_ensure_init`, `_archivist` (via `_run_archivist`) |
| `archivist_status` | 4272-4276 | Public API | `_archivist` |
| `_run_archivist` | 4111-4204 | Private helper | `_archivist`, `_trace_store`, `_knowledge_store`, `_llm_completion`, `_config.cortex_alpha` |

### Cross-references

- `session_end` (orchestrator line 4091-4094) calls `_run_archivist` — after move, calls `self._knowledge_service.run_archivist(tid, uid)`
- `archivist_trigger` calls `_run_archivist` — after move, becomes same-service call `self._run_archivist(...)`

### Self-referencing calls (within extraction set)

| Caller | Callee | After move |
|--------|--------|------------|
| `archivist_trigger` | `_run_archivist` | `self._run_archivist(...)` (same-service) |

---

## Implementation Units

- [ ] U1. **Create KnowledgeService and move methods**

**Goal:** Create `src/opencortex/services/knowledge_service.py` with all 7 knowledge methods moved from orchestrator. Add delegate stubs on orchestrator. Update `session_end` cross-reference.

**Requirements:** R1, R2, R3, R5

**Dependencies:** None

**Files:**
- Create: `src/opencortex/services/knowledge_service.py`
- Modify: `src/opencortex/orchestrator.py` (delegate stubs + `session_end` update)
- Create: `tests/test_knowledge_service.py`

**Approach:**
1. Create `KnowledgeService` class with `__init__(self, orchestrator)` storing `self._orch`
2. Move all 7 methods verbatim, applying `self._X` → `self._orch._X` renames:
   - `_ensure_init()` → `self._orch._ensure_init()`
   - `_knowledge_store` → `self._orch._knowledge_store`
   - `_archivist` → `self._orch._archivist`
   - `_trace_store` → `self._orch._trace_store`
   - `_llm_completion` → `self._orch._llm_completion`
   - `_config` → `self._orch._config`
   - `get_effective_identity()` stays as-is (imported from context module)
3. Rename `_run_archivist` → `run_archivist` (public) on the service since orchestrator's `session_end` calls it
4. Add lazy `@property _knowledge_service` on orchestrator (mirror `_memory_service` pattern)
5. Replace moved methods on orchestrator with one-line delegates
6. Update `session_end` to call `self._knowledge_service.run_archivist(tid, uid)` instead of `self._run_archivist(tid, uid)`

**Imports needed in KnowledgeService:**
- `asyncio` (for `create_task` in `archivist_trigger`)
- `logging`, `Dict`, `Any`, `List`, `Optional` from typing
- `TYPE_CHECKING` for `MemoryOrchestrator` type hint
- `get_effective_identity`, `get_effective_project_id` from `opencortex.http.request_context`
- `KnowledgeScope`, `KnowledgeStatus` from `opencortex.alpha.types`
- `evaluate` from `opencortex.alpha.sandbox`

**Patterns to follow:**
- `src/opencortex/services/memory_service.py` — back-reference pattern, `self._orch._X` renames
- Orchestrator lazy property at line 2611-2630

**Test scenarios:**
- Happy path: `KnowledgeService.__init__` stores orchestrator reference
- Happy path: `knowledge_search` delegates to `_knowledge_store.search` with correct args
- Happy path: `knowledge_approve` delegates to `_knowledge_store.approve`
- Happy path: `knowledge_reject` delegates to `_knowledge_store.reject`
- Happy path: `knowledge_list_candidates` delegates to `_knowledge_store.list_candidates`
- Happy path: `archivist_trigger` creates asyncio task for `_run_archivist`
- Happy path: `archivist_status` returns `_archivist.status` dict
- Integration: `session_end` routes through `self._knowledge_service.run_archivist`

**Verification:**
- `uv run python3 -m unittest discover -s tests -v` passes with zero new failures
- KnowledgeService has all 7 methods with correct signatures
- Orchestrator delegates are one-line stubs calling `self._knowledge_service.X(...)`

---

- [ ] U2. **Style polish**

**Goal:** Google-style docstrings on all public methods, `[KnowledgeService]` logger prefix, type annotation improvements.

**Requirements:** R4

**Dependencies:** U1

**Files:**
- Modify: `src/opencortex/services/knowledge_service.py`
- Modify: `tests/test_knowledge_service.py` (add docstring presence smoke test)

**Approach:**
1. Add Google-style docstrings (Args/Returns/Raises) to all 6 public methods
2. Add brief docstring to `run_archivist` (public helper)
3. Set `logger = logging.getLogger(__name__)` and prefix any log messages with `[KnowledgeService]`
4. Verify type annotations on all method signatures — replace bare `Any` where actual types are knowable
5. Add `TestDocstringPresence` smoke test class to `tests/test_knowledge_service.py`

**Patterns to follow:**
- `src/opencortex/services/memory_service.py` — docstring style, logger prefix pattern
- `tests/test_memory_service.py` — `TestDocstringPresence` pattern

**Test scenarios:**
- Happy path: all public methods have non-empty docstrings
- Happy path: all private helpers have non-empty docstrings

**Verification:**
- Docstring smoke tests pass
- No `# noqa` or type: ignore needed

---

## System-Wide Impact

- **Interaction graph:** `session_end` (orchestrator) gains a call to `_knowledge_service.run_archivist`. This is the only cross-service call from a non-knowledge orchestrator method.
- **Error propagation:** `_run_archivist` already has try/except blocks — no change.
- **State lifecycle risks:** None. KnowledgeService is stateless (no own attributes beyond `self._orch`).
- **API surface parity:** All 6 public methods keep identical signatures. HTTP routes, ContextManager, and tests require zero changes.
- **Integration coverage:** Existing HTTP route tests (`test_phase2_shrinkage.py`) go through the full route→orchestrator→delegate→service chain unchanged.
- **Unchanged invariants:** `_ensure_init()` still guards knowledge methods. Config gating in HTTP routes (`archivist_enabled`) still works. `init()` boot sequence for `_knowledge_store` and `_archivist` unchanged.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `session_end` cross-reference breaks | `run_archivist` is public on the service; simple call target change |
| Circular import between KnowledgeService and orchestrator | Use `TYPE_CHECKING` for type hints; no module-level orchestrator imports |
| `asyncio.create_task` in `archivist_trigger` needs event loop | Already works in orchestrator — same pattern on service |
| Tests bypass `__init__` via `__new__` | Lazy property with `getattr` guard handles this (validated in Phase 1) |

---

## Sources & References

- **Origin document:** [docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md](docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md)
- Related: [docs/plans/2026-04-26-011-refactor-memory-service-completion-plan.md](docs/plans/2026-04-26-011-refactor-memory-service-completion-plan.md) (Phase 1 completion)
- Related code: `src/opencortex/services/memory_service.py` (validated pattern)
- Repo-wide audit: `docs/refactor/2026-04-25-repo-wide-god-object-audit.md`
