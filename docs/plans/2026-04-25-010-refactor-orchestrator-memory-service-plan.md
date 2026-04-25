---
title: "refactor: Decompose MemoryOrchestrator God Object — Phase 1: Extract MemoryService"
type: refactor
status: active
date: 2026-04-25
---

# refactor: Decompose MemoryOrchestrator God Object — Phase 1: Extract MemoryService

## Overview

`src/opencortex/orchestrator.py` is 6474 lines, 132 methods (88 private, 44 public-ish), and 39+ `self._X` subsystem attributes. It is unambiguously a God Object — and the project already pays for it: every test file constructs the orchestrator with bypass tricks (`MemoryOrchestrator.__new__(MemoryOrchestrator)`) because real `init()` triggers expensive subsystem startup, the close() teardown is a 100-line defensive `getattr`-soup, and adding a single new feature touches the orchestrator's centerline by reflex.

This plan is **Phase 1 of a 6-phase decomposition roadmap** that the user explicitly asked be staged across multiple PRs (no mega-refactor). Phase 1 extracts the **memory CRUD + scoring surface** (~15 methods, ~600-1000 lines of behavior) into a dedicated `MemoryService` at `src/opencortex/services/memory_service.py`. The orchestrator keeps its public method names — they become thin pass-through wrappers that delegate to `self._memory_service.X`. External callers (HTTP server, admin routes, MCP plugin) see no contract change.

The remaining 5 phases (KnowledgeService, SystemStatusService, SubsystemBootstrapper, LifecycleManager, style polish sweep) are scoped under `Deferred to Follow-Up Work` so reviewers see the full target architecture without blocking this PR on it.

---

## Problem Frame

The God Object pattern in `MemoryOrchestrator` causes:

1. **Test fragility**: 200+ tests depend on the orchestrator construction/init path. Any subsystem that gets a new dependency forces a test fixture cascade. The `__new__` bypass is widespread (counted in PR #15 work).
2. **Cognitive load**: 6474 lines is past the point where a reviewer can hold the file in their head. PR diffs that touch the orchestrator regularly require reading >500 unaffected surrounding lines to confirm scope.
3. **Coupling**: The 39 `self._X` attributes have implicit ordering dependencies (init step N reads what init step N-1 wrote). The `getattr`-defensive close() is the symptom of these dependencies leaking into shutdown.
4. **Style debt**: Public methods often lack Google-format docstrings, type hints lean on `Any`, module-level constants are mixed in with class attrs. This will only get worse as the file grows.
5. **Diffuse responsibility**: "Memory CRUD", "Knowledge management", "System status reporting", "Lifecycle/sweepers", and "Subsystem boot sequence" all live in the same class, all share `self._storage` / `self._embedder` / etc. directly.

User's explicit constraint: **disciplined, multi-PR decomposition**. Each phase one PR, no mega-refactor, all 200+ existing tests must keep passing, external API surface preserved.

---

## Requirements Trace

- **R1**. `MemoryOrchestrator` reduces by ~600-1000 lines after Phase 1; the moved code lives in `src/opencortex/services/memory_service.py` with clear single-responsibility scope.
- **R2**. Every public method on `MemoryOrchestrator` that was extracted (`add`, `update`, `remove`, `batch_add`, `search`, `list_memories`, `memory_index`, `list_memories_admin`, `feedback`, `feedback_batch`, `decay`, `cleanup_expired_staging`, `protect`, `get_profile`) keeps its exact name + signature + behavior. Internal call sites in admin_routes / http_server / MCP / tests need zero changes.
- **R3**. `MemoryService` follows Google Python Style: PascalCase class, snake_case methods, Google-format docstrings (Args/Returns/Raises) on every public method, full type hints (no bare `Any` where the type is knowable), module constants in UPPER_SNAKE, single responsibility (memory record CRUD + scoring only — no knowledge, no sessions, no benchmark, no lifecycle).
- **R4**. All existing unit/integration/e2e tests pass without modification (zero behavioral regression). The only test changes allowed are NEW tests for the `MemoryService` surface itself.
- **R5**. The phased architecture (Phases 2-6) is documented in this plan so a future implementer can pick up the next phase without re-deriving the target shape.

---

## Scope Boundaries

**This PR (Phase 1) does NOT change**:
- `ContextManager` internals (`src/opencortex/context/manager.py`) — already extracted, untouched here
- `SessionRecordsRepository`, `BenchmarkConversationIngestService` — already extracted in §25 work
- `CortexFS`, `QdrantStorageAdapter`, `RerankClient`, `LLMCompletion` — storage and external clients are not on the extraction path
- `RecallPlanner`, `MemoryExecutor`, `IntentAnalyzer`, `IntentRouter` — recall/intent stack already separated
- `Observer`, `TraceStore`, `Archivist`, `Sandbox`, `KnowledgeStore` — Cortex Alpha pipeline lives in `src/opencortex/alpha/`, untouched here
- `AutophagyKernel`, `CognitiveStateStore`, etc. — cognition module untouched
- HTTP server lifespan, admin routes, MCP plugin — external surfaces preserved
- Method bodies of the extracted methods — Phase 1 is a MOVE, not a REWRITE. Bug fixes and behavior changes are explicitly out of scope and would be separate PRs.

### Deferred to Follow-Up Work

The remaining phases are NOT in this PR but ARE part of the same architectural decomposition. Each becomes its own plan + PR.

- **Phase 2 — Extract `KnowledgeService`** at `src/opencortex/services/knowledge_service.py`. Moves: `knowledge_search`, `knowledge_approve`, `knowledge_reject`, `knowledge_list_candidates`, `archivist_trigger`, `archivist_status`, plus the `_knowledge_store`/`_archivist`/`_sandbox` attribute trio and any helper methods coupled to them. Estimated ~300 lines.
- **Phase 3 — Extract `SystemStatusService`** at `src/opencortex/services/system_status_service.py`. Moves: `system_status`, `derive_status`, `wait_deferred_derives`, `reembed_all`, and the `_system_status` helper machinery. Estimated ~200 lines.
- **Phase 4 — Extract `SubsystemBootstrapper`** at `src/opencortex/lifecycle/bootstrapper.py`. The 11-step `init()` sequence becomes a dedicated object that takes the config and returns a `SubsystemContainer` dataclass holding the wired-up subsystems. The orchestrator's `init()` shrinks to ~30 lines: `self._subsystems = await SubsystemBootstrapper(self._config).build()`. Estimated removes ~400 lines from orchestrator.
- **Phase 5 — Extract `LifecycleManager`** at `src/opencortex/lifecycle/manager.py`. Coordinates the autophagy sweeper, connection sweeper, derive worker, and recall bookkeeping tasks. Owns their startup, supervision (lock + interval), and reverse-order teardown. The orchestrator's `close()` shrinks to ~30 lines: `await self._lifecycle.teardown_in_reverse_order()`. Estimated removes ~300 lines.
- **Phase 6 — Style sweep + facade hardening**. Apply Google-style docstrings, type hint coverage, module constants on the residual facade. Goal: every public method on the post-Phase-5 orchestrator has Args/Returns/Raises and no `Any` where avoidable. Estimated 1-2 days of polish, no behavior change.

After all 6 phases the orchestrator should be ~600-1000 lines of pure facade + initialization + delegation, with the actual subsystem behavior living in their respective service/lifecycle modules.

---

## Context & Research

### Relevant Code and Patterns

The repo already has 4 successful extractions to mirror — pattern is well-grounded:

- `src/opencortex/context/manager.py` (`ContextManager`) — the largest prior extraction. Public-method shim pattern preserved across `MemoryOrchestrator.session_begin/session_message/session_end` already.
- `src/opencortex/context/session_records.py` (`SessionRecordsRepository`) — Phase 5 of §25. Constructor takes storage + collection resolver; methods are async; all paths covered with new test file `tests/test_session_records_repository.py`.
- `src/opencortex/context/benchmark_ingest_service.py` (`BenchmarkConversationIngestService`) — Phase 3 of §25. Service holds a back-reference to `manager` for residual private-attribute access; same pattern is acceptable here for the memory service holding back-refs to orchestrator-owned subsystems (`_storage`, `_embedder`, `_fs`, `_recall_planner`, etc.).
- `src/opencortex/observability/pool_stats.py` — PR #15. Pure-function helpers extracted from a layering inversion. Same intent at smaller scope.

The MemoryService pattern most closely mirrors `BenchmarkConversationIngestService`:
- Service class holds a back-reference to the orchestrator (or to the specific subsystems it needs)
- Service constructor is sync, no expensive init
- Methods are async, share the orchestrator's existing `_storage` / `_embedder` / `_fs` / etc.
- Orchestrator's public method becomes `return await self._memory_service.add(uri, ...)` style

### Institutional Learnings

- `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md` — establishes the "probe / planner / runtime" hot-path discipline. Memory CRUD is on this hot path; the extraction must preserve the runtime behavior bit-for-bit.
- `docs/solutions/best-practices/single-bucket-scoped-probe-2026-04-16.md` — argues scope must be passed as structured input. `MemoryService` methods should accept the same structured args the orchestrator currently accepts; no widening / narrowing.

### External References

- Google Python Style Guide — used as the surface-style contract per user's explicit instruction. Specifically: 3.8 Comments and Docstrings (Google format with Args/Returns/Raises), 3.19 Type Annotations (full coverage, prefer specific types over `Any`), 3.16 Naming (snake_case modules/functions, PascalCase classes, UPPER_SNAKE constants).
- No external library research needed — refactor only, no new dependencies.

---

## Key Technical Decisions

- **Phase 1 extracts MemoryService only.** Decomposing all 5 target services in one PR is exactly the mega-refactor the user prohibited. MemoryService is the largest single coherent slice — its extraction validates the pattern at meaningful scale before committing to the rest. Each subsequent phase becomes its own PR using this PR's pattern as template.

- **Service holds back-reference to orchestrator, not subsystem fields directly.** `MemoryService(orchestrator)` instead of `MemoryService(storage, embedder, fs, recall_planner, ...)`. Mirrors `BenchmarkConversationIngestService` precedent. Two reasons:
  1. Lazy attribute resolution — if the orchestrator hasn't built a subsystem yet (lazy patterns like `_get_or_create_rerank_client`), the service sees the resolved instance, not a stale reference.
  2. Single arg keeps the constructor stable across the remaining phases — Phase 4's `SubsystemBootstrapper` will eventually replace the back-reference with a `SubsystemContainer` parameter; doing both swaps in one PR would be needless churn.

- **Public methods on the orchestrator become one-line delegates.** `async def add(self, ...): return await self._memory_service.add(...)`. The orchestrator's public surface is preserved exactly — call signature, return type, exception contract. This is the only change to `orchestrator.py` for the moved methods (besides removing the original implementation).

- **Private helpers move with their primary public method when they're called from only one place.** Helpers shared across multiple extracted methods stay on the orchestrator (since other phases will need them too); helpers ONLY used by memory CRUD move into `MemoryService`. Boundary call: when in doubt, keep on orchestrator — false splits are easier to fix in Phase 6 than premature merges.

- **Tests stay on `tests/test_e2e_phase1.py`, `tests/test_write_dedup.py`, etc.** They test through the orchestrator surface (which is unchanged). Adding `tests/test_memory_service.py` for the new module focuses on the service-level contract: constructor, single-method exercises with a fake orchestrator, error propagation. Service tests do NOT duplicate the integration-grade coverage the existing tests provide — they cover the new boundary, not the moved behavior.

- **No `__init__.py` re-export shenanigans.** `MemoryService` is imported via `from opencortex.services.memory_service import MemoryService` only. No re-export through `services/__init__.py`. Reduces import-cycle risk and keeps the module surface explicit.

- **Module-level docstring on `memory_service.py` explains WHY, not WHAT.** Per project convention (PR #15 observability/pool_stats.py docstring is the model). Single source of truth for "why this module exists, what its boundary is, what it does NOT do."

---

## Open Questions

### Resolved During Planning

- **Q: Where should `MemoryService` live — `src/opencortex/services/` or `src/opencortex/memory/`?** → `services/`. The `memory/` directory already exists for the lower-level memory data structures (per directory listing). Putting the service there would conflate data-tier with service-tier. Following the §25 precedent (`context/benchmark_ingest_service.py`), services live next to the layer they serve when scoped (e.g., `context/`); when truly orchestrator-level, they live under a new `services/` namespace. This is the first such service so we create the namespace.
- **Q: How does `MemoryService` get access to the orchestrator's lazy `_get_or_create_rerank_client()` style attributes?** → Through the back-reference. `self._orch._get_or_create_rerank_client()` resolves at call time, not at service construction. No issue.
- **Q: Should orchestrator hold `self._memory_service: Optional[MemoryService] = None` and lazy-init, or eager-init in `__init__`?** → Eager. Construction is sync and cheap (no I/O, no model load). Eager init means the delegate methods can blindly call `self._memory_service.X` without `if None` guards. Eager init also keeps the `__new__` bypass tests working — they'd need to set `self._memory_service` explicitly, which is no worse than the current state.

### Deferred to Implementation

- **Exact method-by-method audit of which private helpers move to `MemoryService` vs stay on the orchestrator.** This needs reading method bodies to see what they call. The implementer should default to "keep on orchestrator unless purely internal to memory CRUD." A few minutes of grep per moved method.
- **Whether `batch_add` (which has interesting fan-out + concurrency) should move in this phase or wait for Phase 5 LifecycleManager.** Lean toward moving — it's part of the memory CRUD surface. If it turns out to share lifecycle machinery with the autophagy sweeper or derive worker, the implementer makes the call at execution time.
- **Whether `cleanup_expired_staging` and `decay` should sit on `MemoryService` (they're scoring/lifecycle adjacent to memory records) or wait for Phase 5 LifecycleManager.** Lean toward MemoryService for now — they ARE memory operations. Phase 5 will own the periodic-task scheduling around them, but the operation itself is memory-domain.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
                 BEFORE                                          AFTER (Phase 1)

   src/opencortex/orchestrator.py                  src/opencortex/orchestrator.py
   ┌─────────────────────────────┐                 ┌─────────────────────────────┐
   │ class MemoryOrchestrator    │                 │ class MemoryOrchestrator    │
   │   __init__: 39 self._X      │                 │   __init__: 39 self._X      │
   │   init() — 11 steps         │                 │           + self._memory_service = MemoryService(self)
   │   close() — defensive       │                 │   init() — 11 steps         │
   │                             │                 │   close() — defensive       │
   │   add(...)        ── 80 ln  │                 │                             │
   │   update(...)     ── 50 ln  │                 │   # Phase 1 delegates       │
   │   remove(...)     ── 60 ln  │                 │   add → memory_service.add  │
   │   batch_add(...)  ── 120 ln │   ───►          │   update → ...              │
   │   search(...)     ── 70 ln  │                 │   remove → ...              │
   │   list_memories(...)        │                 │   batch_add → ...           │
   │   memory_index(...)         │                 │   search → ...              │
   │   feedback(...)             │                 │   ... (~14 one-liners)      │
   │   decay(...)                │                 │                             │
   │   ... etc, 132 methods total│                 │   knowledge_*  (Phase 2)    │
   └─────────────────────────────┘                 │   system_status (Phase 3)   │
   6474 lines                                      │   session_*  (already shim)  │
                                                   │   ... (88 private + facade) │
                                                   └─────────────────────────────┘
                                                   ~5500 lines (target ~600 after all 6 phases)

                                                    src/opencortex/services/memory_service.py
                                                   ┌─────────────────────────────┐
                                                   │ class MemoryService         │
                                                   │   __init__(orchestrator)    │
                                                   │                             │
                                                   │   add(...)                  │
                                                   │   update(...)               │
                                                   │   remove(...)               │
                                                   │   batch_add(...)            │
                                                   │   search(...)               │
                                                   │   list_memories(...)        │
                                                   │   memory_index(...)         │
                                                   │   list_memories_admin(...)  │
                                                   │   feedback(...)             │
                                                   │   feedback_batch(...)       │
                                                   │   decay(...)                │
                                                   │   cleanup_expired_staging() │
                                                   │   protect(...)              │
                                                   │   get_profile(...)          │
                                                   │                             │
                                                   │   # Google-style docstrings │
                                                   │   # Full type hints         │
                                                   └─────────────────────────────┘
                                                   ~600-1000 lines (the moved bodies)
```

The shim pattern on the orchestrator is deliberately minimal:

```
async def add(self, *args, **kwargs):
    return await self._memory_service.add(*args, **kwargs)
```

Reviewers can scan-confirm that no behavior changed by reading the diff: each public method on the orchestrator becomes a 2-line wrapper, and the body that was deleted is the body now living in `memory_service.py`.

---

## Implementation Units

- [ ] U1. **Scaffolding: create `services/` namespace + empty `MemoryService` shell**

**Goal:** Land the new module path with a smoke test before moving any real code. Lets later units move methods individually without scaffolding noise.

**Requirements:** R1, R3.

**Dependencies:** None.

**Files:**
- Create: `src/opencortex/services/__init__.py` (empty / module docstring only)
- Create: `src/opencortex/services/memory_service.py` (class skeleton + module docstring)
- Create: `tests/test_memory_service.py` (smoke test: instantiate with a `MagicMock(spec=[])` orchestrator, assert no crash)

**Approach:**
- `services/__init__.py` empty body (per Key Technical Decision: no re-exports).
- `MemoryService` class with `__init__(self, orchestrator)` storing `self._orch = orchestrator`. No methods yet.
- Module docstring on `memory_service.py` modeled on `src/opencortex/observability/pool_stats.py`: explains WHY (decompose God Object, Phase 1 of N), what the boundary is (memory record CRUD + scoring), what it does NOT do (knowledge, sessions, benchmark, lifecycle).
- Smoke test verifies the class can be constructed without touching any orchestrator subsystems.

**Patterns to follow:**
- `src/opencortex/observability/pool_stats.py` module docstring shape (PR #15).
- `src/opencortex/context/benchmark_ingest_service.py` constructor shape (`BenchmarkConversationIngestService(manager)`).

**Test scenarios:**
- *Happy path*: `MemoryService(MagicMock())` constructs without raising. `service._orch is mock` is true.
- *Edge case*: `MemoryService(None)` constructs without raising — back-reference is just stored, not validated. (If we ever want validation, that's a separate decision.)

**Verification:**
- `tests/test_memory_service.py` collects and passes under `uv run --group dev pytest tests/test_memory_service.py`.
- `from opencortex.services.memory_service import MemoryService` works in a Python REPL.

---

- [ ] U2. **Move CRUD methods (add, update, remove, batch_add)**

**Goal:** Move the 4 write-side memory methods into `MemoryService`, replace the orchestrator originals with one-line delegates.

**Requirements:** R1, R2, R4.

**Dependencies:** U1.

**Files:**
- Modify: `src/opencortex/services/memory_service.py` (add the 4 methods)
- Modify: `src/opencortex/orchestrator.py` (replace original bodies with `return await self._memory_service.X(...)` delegates; eager-init `self._memory_service = MemoryService(self)` in `__init__`)
- Modify: `tests/test_memory_service.py` (add per-method assertions on a fake orchestrator)

**Approach:**
- For each of `add`, `update`, `remove`, `batch_add`: cut the body from `orchestrator.py`, paste verbatim into `MemoryService` (rename `self.X` → `self._orch.X` for orchestrator-level attribute access; private orchestrator helpers stay accessed via `self._orch._helper(...)`).
- Add Google-style docstring to each moved method (Args / Returns / Raises). Existing docstrings (when present) get reformatted to Google style; missing docstrings get added.
- Replace the orchestrator's original definition with the delegate one-liner. Preserve method signature, decorators (if any), type hints exactly.
- Add `self._memory_service = MemoryService(self)` to `MemoryOrchestrator.__init__` (eager — see Key Technical Decision).
- For tests using the `__new__` bypass pattern: those tests typically don't call CRUD methods (they test init/close). But sanity-check by running the existing suite — if a `__new__` test accidentally calls a moved method, the AttributeError is the signal to add `orch._memory_service = ...` to that test fixture. Document the pattern in test file comments.

**Patterns to follow:**
- `src/opencortex/context/benchmark_ingest_service.py` — see how the service accesses `manager._orchestrator._something`. Same back-reference indirection pattern.
- Existing one-line delegate examples in `orchestrator.py` for session_* methods (those already shim to `ContextManager`).

**Test scenarios:**
- *Integration*: `tests/test_e2e_phase1.py` (writes via `orchestrator.add`, asserts retrieval) passes unchanged.
- *Integration*: `tests/test_write_dedup.py` passes unchanged.
- *Integration*: `tests/test_ingestion_e2e.py` passes unchanged.
- *Service-level*: in `tests/test_memory_service.py`, build a stub orchestrator with mock `_storage` / `_embedder`, call `service.add(...)`, assert the underlying storage `upsert` was awaited with the expected payload. One test per moved method.

**Verification:**
- 4 methods physically live in `memory_service.py`, not `orchestrator.py`.
- Orchestrator's original `add`/`update`/`remove`/`batch_add` are 1-2 lines each (`return await self._memory_service.X(...)`).
- Wider regression sweep (`uv run python3 -m unittest discover -s tests`) shows the same pass/fail count as on master (modulo the documented pre-existing flake `test_update_regenerates_fact_points_after_content_change`).

---

- [ ] U3. **Move query methods (search, list_memories, memory_index, list_memories_admin)**

**Goal:** Move the 4 read-side memory methods using the same pattern as U2.

**Requirements:** R1, R2, R4.

**Dependencies:** U2 (so the delegate pattern + service init are already in place).

**Files:**
- Modify: `src/opencortex/services/memory_service.py` (add the 4 methods + Google docstrings)
- Modify: `src/opencortex/orchestrator.py` (replace original bodies with delegates)
- Modify: `tests/test_memory_service.py` (add service-level tests)

**Approach:**
- Same recipe as U2: cut, paste, rename `self.X → self._orch.X` for orchestrator-level state, add Google docstrings, replace original with one-line delegate.
- `list_memories_admin` has admin auth dependency — preserve the delegation; admin gating happens at the HTTP route layer, not in the service.
- `memory_index` returns aggregated counts. Preserve return shape exactly.

**Patterns to follow:**
- Same as U2.

**Test scenarios:**
- *Integration*: `tests/test_e2e_phase1.py` search-related tests pass.
- *Integration*: HTTP-layer tests in `tests/test_http_server.py` that exercise `/api/v1/memory/search` and `/api/v1/admin/memories` pass unchanged.
- *Service-level*: in `tests/test_memory_service.py`, mock the `_storage.search` interaction and assert `service.search(...)` returns the expected shape.

**Verification:**
- 4 methods live in `memory_service.py`.
- Orchestrator's `search`/`list_memories`/`memory_index`/`list_memories_admin` are one-line delegates.
- HTTP regression sweep green.

---

- [ ] U4. **Move scoring + lifecycle-adjacent methods (feedback, feedback_batch, decay, cleanup_expired_staging, protect, get_profile)**

**Goal:** Move the 6 reward-scoring / decay / staging methods. These are the "memory record auxiliary" surface — operations that act on memory records but aren't direct CRUD.

**Requirements:** R1, R2, R4.

**Dependencies:** U2 (delegate pattern in place).

**Files:**
- Modify: `src/opencortex/services/memory_service.py` (add the 6 methods)
- Modify: `src/opencortex/orchestrator.py` (replace original bodies with delegates)
- Modify: `tests/test_memory_service.py` (add service-level tests)

**Approach:**
- Same recipe as U2/U3.
- `cleanup_expired_staging` and `decay` have periodic-task callers in the autophagy/connection-sweeper paths — that wiring stays where it is; only the operation moves. The sweepers will continue to call `self._orch.cleanup_expired_staging()` (the orchestrator's delegate), no need to re-wire the sweepers to call the service directly. Phase 5 (LifecycleManager) will reconsider; Phase 1 doesn't.
- `feedback_batch` has fan-out — preserve the implementation, including any internal concurrency.
- `protect` and `get_profile` interact with the reward scoring fields on records; preserve exactly.

**Patterns to follow:**
- Same as U2.

**Test scenarios:**
- *Integration*: existing tests for `feedback`, `decay`, `protect` (search `tests/` for usages — primarily `tests/test_e2e_phase1.py`) pass unchanged.
- *Integration*: the autophagy sweeper test (if it exercises `cleanup_expired_staging` indirectly) passes — confirms the orchestrator-level delegate is wired correctly.
- *Service-level*: in `tests/test_memory_service.py`, add a single test per moved method that exercises the underlying storage interaction with a mock.

**Verification:**
- 6 methods live in `memory_service.py`.
- Orchestrator's originals are one-line delegates.
- Wider regression sweep green.

---

- [ ] U5. **Style polish on `MemoryService`: Google-format docstrings, type hint coverage, module constants**

**Goal:** Apply Google Python Style consistently across the new module. This is the "service-quality" pass — once content is in place (U2-U4), make sure the module reads as a clean, well-documented public-API surface.

**Requirements:** R3.

**Dependencies:** U4 (all methods landed).

**Files:**
- Modify: `src/opencortex/services/memory_service.py` (docstrings, type hints, constants)
- Modify: `tests/test_memory_service.py` (add a doctring presence smoke test if natural)

**Approach:**
- Audit every public method for Google-format docstring presence. Format: one-line summary, blank line, optional extended description, `Args:` / `Returns:` / `Raises:` blocks. Mirror the convention from `src/opencortex/observability/pool_stats.py`.
- Replace `Any` in method signatures with the actual type when the type is knowable from existing call sites (e.g., `Dict[str, Any]` for storage payload, `List[Dict[str, Any]]` for search results, etc.). Where the contract truly is "anything", `Any` stays — but document why in the docstring.
- Lift any inline magic numbers (e.g., timeout values, batch sizes) to module-level UPPER_SNAKE constants at the top of the file. Each constant gets a one-line comment explaining what it does.
- Verify no imports are unused or relative; absolute `from opencortex.X import Y` only.

**Test scenarios:**
- *Smoke*: a test that `dir(MemoryService)` exposes all expected method names (catches accidental rename or deletion).
- *Smoke* (optional): assert every public method has a non-empty `__doc__`.

**Verification:**
- `MemoryService` reads as a clean, single-purpose module with consistent docstrings and type hints.
- Code review of the diff passes on Google Style — no `Any` where avoidable, no missing docstrings, no relative imports.

---

## System-Wide Impact

- **Interaction graph:** External callers of `MemoryOrchestrator.add/update/remove/search/...` see no change. Internal callers (other orchestrator methods that call `self.add(...)` directly — if any) need to either keep using the public method (works because the delegate preserves behavior) or switch to `self._memory_service.add(...)` (slightly faster, one less hop). Default to the former unless the implementer sees a clear hot-path benefit.
- **Error propagation:** `MemoryService` methods raise the same exceptions the originals raised (the bodies are moved verbatim). Delegates don't catch — they let exceptions bubble. No new error layer.
- **State lifecycle risks:** None new. The service holds zero state; it's a method bag with a back-reference. All real state stays on the orchestrator.
- **API surface parity:** HTTP routes (`src/opencortex/http/admin_routes.py`, `src/opencortex/http/server.py`) and MCP plugin (`plugins/opencortex-memory/`) call orchestrator methods by name. None of their imports or call sites change.
- **Integration coverage:** The 200+ existing tests cover every public surface that's being delegated. The new `tests/test_memory_service.py` adds boundary tests but does not duplicate them.
- **Unchanged invariants:** `ContextManager`, `SessionRecordsRepository`, `BenchmarkConversationIngestService`, `RecallPlanner`, `MemoryExecutor`, `IntentRouter`, all alpha components, all cognition components, all storage adapters, FastAPI lifespan, MCP plugin — all explicitly out of scope and untouched.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| A moved method silently changes behavior because of an attribute access typo (`self.X` → `self._orch.X`) | Each unit's verification step requires the existing test suite to pass at the same count. The integration tests are the regression net — they exercise the moved methods through the public surface. |
| `__new__`-bypass tests crash because they don't set `self._memory_service` | The bypass tests almost universally exercise lifecycle methods (init/close), not memory CRUD. If one accidentally calls a moved method, the AttributeError is loud and the fix is one line: set `orch._memory_service = MemoryService(orch)` in the test setup. Document this in `tests/test_memory_service.py` header comment. |
| Phase 1's pattern doesn't generalize to Phase 2-6 | Phase 1 was chosen specifically because it's the largest single coherent slice — extracting it validates the pattern at meaningful scale. If issues surface, the next plan revises the pattern before committing more PRs to it. |
| `batch_add`'s concurrency primitives (asyncio.Semaphore, gather) interact with orchestrator state in non-obvious ways | `batch_add` is moved BUT preserves all its internal structure verbatim — semaphores, concurrent loops, error aggregation. The implementer reads the body before moving, not after. If implementation reveals real coupling, defer `batch_add` to a follow-up unit and document the reason. |
| The "back-reference to orchestrator" pattern creates a circular-ish coupling | True, but acceptable for the same reason it was acceptable in `BenchmarkConversationIngestService`: services depend on orchestrator-level subsystems (storage, embedder, fs) that genuinely live there. Phase 4's `SubsystemBootstrapper` will eventually replace the back-reference with a `SubsystemContainer` parameter, which removes the circular shape. Phase 1 inherits the precedent's tradeoff. |
| Reviewers see a 600-1000 line diff and lose context | The diff is structurally trivial — every changed method either becomes a one-liner or moves verbatim. The PR description should call out this pattern explicitly so reviewers know to scan, not deep-read. The plan itself (this document) gets linked from the PR for the architecture context. |

---

## Documentation / Operational Notes

- After all 6 phases land, write a `docs/solutions/best-practices/orchestrator-decomposition-2026-04.md` capturing the pattern (back-reference services, eager init, one-line delegates, phased rollout). The §25 work has set the precedent that this kind of post-mortem write-up is valuable.
- No CHANGELOG entry needed for Phase 1 — internal refactor, no user-facing API change.
- No new env vars, no new endpoints, no operational change.

---

## Sources & References

- `src/opencortex/orchestrator.py` (target of the refactor)
- `src/opencortex/context/manager.py` (ContextManager — pattern reference)
- `src/opencortex/context/session_records.py` (SessionRecordsRepository — pattern reference)
- `src/opencortex/context/benchmark_ingest_service.py` (BenchmarkConversationIngestService — closest pattern reference)
- `src/opencortex/observability/pool_stats.py` (recent extraction — module docstring shape)
- `docs/plans/2026-04-25-005-refactor-benchmark-ingest-server-side-design-patterns-plan.md` (§25 plan — reference for phased decomposition discipline)
- Google Python Style Guide (style contract per user instruction)
