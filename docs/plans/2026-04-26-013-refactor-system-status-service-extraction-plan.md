---
title: "refactor: Extract SystemStatusService from MemoryOrchestrator (Phase 4)"
type: refactor
status: active
date: 2026-04-26
origin: docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md
---

# refactor: Extract SystemStatusService from MemoryOrchestrator (Phase 4)

## Overview

Phase 4 of the 8-phase God Object decomposition. Extracts the system status and derive-pipeline surface (~123 lines of method bodies) from `MemoryOrchestrator` into a dedicated `SystemStatusService` at `src/opencortex/services/system_status_service.py`. Same MOVE-not-REWRITE discipline as Phases 1-2 — method bodies land verbatim, orchestrator keeps thin delegate stubs, all existing tests pass unchanged.

---

## Problem Frame

Plan 010 defines the full decomposition roadmap. Phases 1-2 (MemoryService + KnowledgeService, PRs #16-#18) moved ~1650 lines. Phase 4 targets the status and derive-pipeline surface: a self-contained cluster of methods that report health, aggregate stats, check async derive state, and trigger re-embedding. The orchestrator is ~4600 lines post-Phase-2; this extraction removes ~123 lines of method bodies (replaced by one-line delegates).

---

## Requirements Trace

- R1. `SystemStatusService` follows the established back-reference pattern: `SystemStatusService(orchestrator)` with `self._orch` access to subsystems. (origin: plan 010 Phase 4 spec)
- R2. Every public method extracted (`system_status`, `health_check`, `stats`, `derive_status`, `wait_deferred_derives`, `reembed_all`) keeps its exact name + signature + behavior. External callers (HTTP routes, admin routes, tests) see zero contract change.
- R3. `SystemStatusService` follows Google Python Style: docstrings with Args/Returns/Raises on every public method, full type hints, `[SystemStatusService]` logger prefixes.
- R4. All existing tests pass without modification. Only NEW tests for `SystemStatusService` surface are added.

---

## Scope Boundaries

- This is a MOVE, not a REWRITE. Method bodies land verbatim with only `self._X` → `self._orch._X` renames.
- No changes to HTTP routes (`server.py`), admin routes (`admin_routes.py`), or any test file.
- `_maybe_warn_pool` is **NOT** moved — it is exclusively called from `_run_connection_sweep_once` (connection sweeper infrastructure that belongs to Phase 6). Moving it would create an awkward cross-service call from the sweep loop back into `SystemStatusService`.
- Attribute ownership: `_storage`, `_embedder`, `_llm_completion`, `_config`, `_fs`, `_inflight_derive_uris`, `_deferred_derive_count` all remain on the orchestrator. `SystemStatusService` accesses them via `self._orch._X`.
- `_build_rerank_config()` stays on the orchestrator. `stats()` uses it via `self._orch._build_rerank_config()`.
- `_get_collection()` stays on the orchestrator. `derive_status()` and `reembed_all()` use it via `self._orch._get_collection()`.

### Deferred to Follow-Up Work

- Phase 5: Extract `SubsystemBootstrapper` (origin: plan 010)
- Phase 6: Extract background sweep loops (`_connection_sweep_loop`, `_run_connection_sweep_once`, `_maybe_warn_pool`) into a `BackgroundTaskManager`

---

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/services/knowledge_service.py` — the most recent validated extraction pattern: `self._orch` back-reference, `self._orch._X` subsystem access, one-line delegates via lazy `@property`
- `src/opencortex/orchestrator.py` lines 2633-2647 — lazy `_knowledge_service` property pattern to mirror for `_system_status_service`
- `src/opencortex/http/server.py` — calls `_orchestrator.health_check()`, `_orchestrator.stats()`, `_orchestrator.system_status()`, `_orchestrator.derive_status()`, `_orchestrator.wait_deferred_derives()` — all must remain on the orchestrator public surface as delegates
- `src/opencortex/http/admin_routes.py` — calls `_orchestrator.reembed_all()`

### Method Inventory

| Method | Approx. Lines | Role | `self._X` accessed |
|--------|---------------|------|---------------------|
| `system_status` | 3905-3927 | Public API — aggregates health + stats | `health_check`, `stats` (within service) |
| `health_check` | 4361-4376 | Public API — component health | `_initialized`, `_storage`, `_embedder`, `_llm_completion` |
| `stats` | 4378-4410 | Public API — storage + rerank stats | `_storage`, `_embedder`, `_llm_completion`, `_build_rerank_config()`, `_get_collection()` (via `_ensure_init()`) |
| `derive_status` | 1488-1511 | Public API — async derive state | `_inflight_derive_uris`, `_fs`, `_storage`, `_get_collection()` |
| `wait_deferred_derives` | 2097-2104 | Public API — wait for in-flight derives | `_deferred_derive_count` |
| `reembed_all` | 1513-1531 | Public API — re-embed all records | `_storage`, `_get_collection()`, `_embedder`, `_config` |

### Self-referencing calls within extraction set

| Caller | Callee | After move |
|--------|--------|------------|
| `system_status` | `health_check` | `self.health_check()` (same-service) |
| `system_status` | `stats` | `self.stats()` (same-service) |

### Institutional Learnings

- **Back-reference pattern validated** across 3 extractions (BenchmarkConversationIngestService, MemoryService, KnowledgeService). Constructor stores `self._orch`, methods access `self._orch._X`. (plans 010, 011, 012)
- **Lazy property with `getattr` guard** handles tests that bypass `__init__` via `__new__`. (plan 011 actual implementation)
- **`_build_rerank_config()` and `_get_collection()` are shared helpers** — they stay on orchestrator, accessed via `self._orch._build_rerank_config()` etc. (established in plan 011 helper classification)
- **`_ensure_init()` stays on orchestrator** — `stats()` calls it; the guard remains `self._orch._ensure_init()`.

---

## Key Technical Decisions

- **`health_check` and `stats` move with `system_status`**: `system_status` delegates to both. Moving all three keeps the service cohesive and avoids orchestrator-to-service cross-calls in the status aggregation path.
- **`_maybe_warn_pool` stays on orchestrator**: It is exclusively called by `_run_connection_sweep_once` (connection sweeper), not by any of the moved methods. Moving it would create an awkward `self._orch._system_status_service._maybe_warn_pool(...)` call from the sweep loop. Phase 6 will co-locate it with the sweeper.
- **No attribute ownership transfer**: Status-related state (`_initialized`, `_storage`, etc.) remains on the orchestrator. Phase 5 (SubsystemBootstrapper) will eventually own boot state.
- **Lazy property mirrors KnowledgeService**: `@property _system_status_service` on the orchestrator with `getattr` guard, same pattern as `_knowledge_service`.

---

## Implementation Units

- [ ] U1. **Create SystemStatusService and move methods**

**Goal:** Create `src/opencortex/services/system_status_service.py` with all 6 status/derive methods moved from orchestrator. Add delegate stubs on orchestrator. Wire lazy property.

**Requirements:** R1, R2, R4

**Dependencies:** None

**Files:**
- Create: `src/opencortex/services/system_status_service.py`
- Modify: `src/opencortex/orchestrator.py` (delegate stubs + `_system_status_service` lazy property + `_system_status_service_instance` init)
- Create: `tests/test_system_status_service.py`

**Approach:**
1. Create `SystemStatusService` class with `__init__(self, orchestrator)` storing `self._orch`
2. Move all 6 methods verbatim, applying `self._X` → `self._orch._X` renames:
   - `self._initialized` → `self._orch._initialized`
   - `self._storage` → `self._orch._storage`
   - `self._embedder` → `self._orch._embedder`
   - `self._llm_completion` → `self._orch._llm_completion`
   - `self._config` → `self._orch._config`
   - `self._fs` → `self._orch._fs`
   - `self._inflight_derive_uris` → `self._orch._inflight_derive_uris`
   - `self._deferred_derive_count` → `self._orch._deferred_derive_count`
   - `self._build_rerank_config()` → `self._orch._build_rerank_config()`
   - `self._get_collection()` → `self._orch._get_collection()`
   - `self._ensure_init()` → `self._orch._ensure_init()`
   - `self.health_check()` (in `system_status`) → `self.health_check()` (same-service, no rename)
   - `self.stats()` (in `system_status`) → `self.stats()` (same-service, no rename)
3. Add `self._system_status_service_instance: Optional[Any] = None` to `__init__` in orchestrator (alongside existing `_memory_service_instance` and `_knowledge_service_instance`)
4. Add lazy `@property _system_status_service` on orchestrator (mirror `_knowledge_service` pattern)
5. Replace 6 moved methods on orchestrator with one-line delegates
6. Update `src/opencortex/services/__init__.py` docstring to mention `SystemStatusService` (Phase 4 addition)

**Imports needed in SystemStatusService:**
- `asyncio`, `logging`
- `typing`: `TYPE_CHECKING`, `Any`, `Dict`, `Optional`
- `TYPE_CHECKING` guard: `from opencortex.orchestrator import MemoryOrchestrator`
- `get_effective_identity` from `opencortex.http.request_context`

**Patterns to follow:**
- `src/opencortex/services/knowledge_service.py` — back-reference pattern, `self._orch._X` renames, module docstring structure
- Orchestrator lazy property at lines 2633-2647

**Test scenarios:**
- Happy path: `SystemStatusService.__init__` stores orchestrator reference
- Happy path: `SystemStatusService(None)` stores None without validation (mirrors knowledge_service test)
- Happy path: lazy `_system_status_service` property on orchestrator via `__new__` bypass succeeds
- Happy path: lazy property caches service instance (two reads return same object)
- Happy path: `health_check` returns dict with `initialized`, `storage`, `embedder`, `llm` keys
- Happy path: `stats` returns dict with `tenant_id`, `user_id`, `storage`, `embedder`, `has_llm`, `rerank` keys
- Happy path: `system_status("health")` routes to `health_check`
- Happy path: `system_status("stats")` routes to `stats`
- Happy path: `system_status("doctor")` merges health + stats + issues list
- Happy path: `derive_status` returns `{"status": "pending"}` when URI in `_inflight_derive_uris`
- Happy path: `wait_deferred_derives` returns immediately when `_deferred_derive_count == 0`
- Happy path: all public methods have non-empty docstrings

**Verification:**
- `uv run python3 -m unittest discover -s tests -v` passes with zero new failures
- `SystemStatusService` has all 6 methods with correct signatures
- Orchestrator delegates are one-line stubs calling `self._system_status_service.X(...)`

---

- [ ] U2. **Style polish**

**Goal:** Google-style docstrings on all public methods, `[SystemStatusService]` logger prefix, type annotation check.

**Requirements:** R3

**Dependencies:** U1

**Files:**
- Modify: `src/opencortex/services/system_status_service.py`
- Modify: `tests/test_system_status_service.py` (docstring presence smoke test already in U1 tests)

**Approach:**
1. Verify all 6 public methods have Google-style docstrings (Args/Returns/Raises)
2. Add `logger = logging.getLogger(__name__)` if any logging is needed; use `[SystemStatusService]` prefix
3. Confirm type annotations on all method signatures are complete

**Patterns to follow:**
- `src/opencortex/services/knowledge_service.py` — docstring style, module-level docstring boundary description

**Test scenarios:**
- Happy path: all public methods have non-empty docstrings (smoke test from U1)

**Verification:**
- Docstring smoke tests pass
- No `# noqa` or `type: ignore` needed

---

## System-Wide Impact

- **No API surface change**: All 6 methods keep identical signatures on the orchestrator (as one-line delegates). HTTP routes, admin routes, and all tests require zero changes.
- **State lifecycle risks**: None. `SystemStatusService` is stateless (no own attributes beyond `self._orch`).
- **`system_status` → `health_check`/`stats` intra-service calls**: These were previously `self.method()` on orchestrator; after extraction they become `self.method()` within the service. No behavior change.
- **`_deferred_derive_count` race**: `wait_deferred_derives` polls `self._orch._deferred_derive_count`. The polling logic is verbatim-moved; no new concurrency surface is introduced.
- **`stats()` calls `_build_rerank_config()`**: This shared helper stays on orchestrator; the service calls it via `self._orch._build_rerank_config()`. Same pattern used for other shared helpers in Phases 1-2.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Circular import between `SystemStatusService` and orchestrator | Use `TYPE_CHECKING` guard for type hints; no module-level orchestrator import |
| `_build_rerank_config()` / `_get_collection()` not accessible | Both stay on orchestrator; access via `self._orch._build_rerank_config()` (same pattern as all prior extractions) |
| Tests bypass `__init__` via `__new__` | Lazy property with `getattr` guard handles this (validated in Phases 1-2) |
| `health_check` / `stats` called directly by HTTP routes | Delegates remain on orchestrator; HTTP routes are unaffected |

---

## Sources & References

- **Origin document:** [docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md](docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md)
- Related: [docs/plans/2026-04-26-012-refactor-knowledge-service-extraction-plan.md](docs/plans/2026-04-26-012-refactor-knowledge-service-extraction-plan.md) (Phase 2, validated pattern)
- Related code: `src/opencortex/services/knowledge_service.py` (validated pattern)
