---
title: "refactor: Complete MemoryService extraction — add, batch_add, search, scoring"
type: refactor
status: active
date: 2026-04-26
---

# refactor: Complete MemoryService extraction — add, batch_add, search, scoring

## Overview

Plan 010 (PR #16) scaffolded `MemoryService` and extracted `update` + `remove`, validating the back-reference + delegate pattern. This plan completes the extraction by moving the remaining 12 public methods and their target-only helpers (~1440 lines) from `MemoryOrchestrator` into the existing `MemoryService` at `src/opencortex/services/memory_service.py`.

After this plan, `MemoryService` is the complete home for all memory-record CRUD, queries, and scoring. The orchestrator retains one-line delegate stubs for every moved method. No external API surface changes.

---

## Problem Frame

The orchestrator (`orchestrator.py`, 6286 lines) remains a God Object. Plan 010 extracted 2 of ~14 memory methods. The remaining 12 methods — including `add` (380L, the largest single move) and `search` (267L) — still live inline. Each method accessed through the orchestrator centerline increases cognitive load, diff noise, and test fragility. Completing the extraction is the next step in the 6-phase decomposition roadmap documented in plan 010's "Deferred to Follow-Up Work" section.

---

## Requirements Trace

- **R1.** `MemoryService` grows from 347 lines to ~1790 lines. All 14 memory methods (12 new + 2 existing) live there with clear single-responsibility scope. The orchestrator shrinks by ~1440 lines of implementation (replaced by delegates).
- **R2.** Every moved public method (`add`, `batch_add`, `search`, `list_memories`, `memory_index`, `list_memories_admin`, `feedback`, `feedback_batch`, `decay`, `cleanup_expired_staging`, `protect`, `get_profile`) keeps its exact name + signature + behavior on the orchestrator. Internal callers (HTTP routes, admin routes, MCP plugin, tests) need zero changes.
- **R3.** `MemoryService` follows Google Python Style: Google-format docstrings on every moved method, full type hints, module constants in UPPER_SNAKE. Shared helpers stay on the orchestrator and are accessed via `self._orch._helper()`.
- **R4.** All existing tests pass without modification. The only test changes are NEW boundary tests in `tests/test_memory_service.py` for the moved methods.
- **R5.** Target-only private helpers move with their parent method; shared helpers stay on the orchestrator. Boundary classification is documented in this plan.

---

## Scope Boundaries

- This plan does NOT change `ContextManager`, `SessionRecordsRepository`, `BenchmarkConversationIngestService`, or any already-extracted modules.
- This plan does NOT extract `KnowledgeService`, `BackgroundTaskManager`, `SystemStatusService`, `SubsystemBootstrapper`, or `ContextManager` — those are plans 012-017.
- This plan does NOT change HTTP routes, admin routes, MCP plugin, or FastAPI lifespan — external surfaces are preserved.
- This plan is a MOVE, not a REWRITE. Method bodies land verbatim. Bug fixes, behavior changes, and style cleanups on the moved methods belong in U5 (style polish) or separate PRs.

### Deferred to Follow-Up Work

- **Plan 012**: Extract `KnowledgeService` (knowledge_search, archivist_trigger, etc.)
- **Plan 013**: Extract `BackgroundTaskManager` (sweepers, derive worker, bookkeeping)
- **Plan 014**: Extract `ObservabilityReporter` + `SystemStatusService`
- **Plan 015**: Extract `SubsystemBootstrapper` (init sequence)
- **Plan 016**: Repo-wide style sweep
- **Plan 017**: `ContextManager` decomposition (second God Object)

---

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/services/memory_service.py` — current state: scaffolding + `update` (213L) + `remove` (50L), 346 lines total
- `src/opencortex/orchestrator.py` — 6286 lines; lazy property `_memory_service` at line 2774; delegate stubs for `update`/`remove` at lines 3290-3310
- `src/opencortex/context/benchmark_ingest_service.py` — pattern reference for `Service(parent)` back-reference
- `docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md` — the master decomposition plan

### Institutional Learnings

- **Hot-path discipline** (`docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md`): `search` sits on the recall hot path. The extraction must preserve the probe/planner/runtime phase boundaries bit-for-bit. The service must not introduce new semantic decisions.
- **Single-bucket scoped probe** (`docs/solutions/best-practices/single-bucket-scoped-probe-2026-04-16.md`): `search` builds structured scope input before calling into the probe. Scope must remain structured, not flattened.
- **Lazy-property pattern** (from Plan 010 implementation): The orchestrator uses a lazy `@property _memory_service` instead of eager init. New delegate stubs must call `self._memory_service.X(...)` (the property), not `self._memory_service_instance`.

### Helper Classification

**Target-only helpers (move with their parent):**

| Helper | Lines | Parent method | Orchestrator line |
|--------|-------|---------------|-------------------|
| `_add_document` | 160L | `add` | L1737 |
| `_check_duplicate` | 71L | `add` | L3191 |
| `_merge_into` | 26L | `add` | L3263 |
| `_ensure_parent_records` | 78L | `add` | L6117 |
| `_build_typed_queries` | 37L | `search` | L3403 |
| `_context_type_from_value` | 5L | `search` (static) | L3442 |
| `_detail_level_from_retrieval_depth` | 4L | `search` (static) | L3449 |
| `_infer_context_type` | 12L | `search` (can become `@staticmethod` — only parses URI string, no `self` access) | L6103 |
| `_summarize_retrieve_breakdown` | 16L | `search` (static) | L3456 |
| `_ttl_from_hours` | 6L | `add` (used by `add` + `_write_immediate`; `_write_immediate` stays on orchestrator, but `add` is the primary caller — keep on orchestrator, access via `self._orch._ttl_from_hours()`) | L3180 |

**Shared helpers (stay on orchestrator, accessed via `self._orch._helper()`):**

`_ensure_init`, `_get_collection`, `_derive_layers`, `_build_abstract_json`, `_memory_object_payload`, `_sync_anchor_projection_records`, `_auto_uri`, `_resolve_unique_uri`, `_derive_parent_uri`, `_extract_category_from_uri`, `_get_record_by_uri`, `_initialize_autophagy_owner_state`, `_aggregate_results`, `_build_search_filter`, `_execute_object_query`, `_generate_abstract_overview`, `_fallback_overview_from_content`, `_derive_abstract_from_overview`.

### Self-Referencing Calls After Move

Methods that call other target methods (these become same-service calls after move):

- `feedback_batch` → calls `self.feedback()` → becomes `self.feedback()` on service
- `decay` → calls `self.cleanup_expired_staging()` → becomes `self.cleanup_expired_staging()` on service
- `batch_add` → calls `self.add()` → becomes `self.add()` on service
- `_merge_into` → calls `self.update()`, `self.feedback()` → `self.update()` is a same-service call (already on MemoryService from Plan 010); `self._orch.feedback()` needed temporarily until U4 moves `feedback`

---

## Key Technical Decisions

- **`_ttl_from_hours` stays on orchestrator.** Although `add` is a primary caller, `_write_immediate` (which stays on orchestrator) also calls it. Moving it would create a circular call (`_write_immediate` → `self._memory_service._ttl_from_hours()`). Keep on orchestrator; `add` accesses via `self._orch._ttl_from_hours()`.

- **`_merge_into` calls `self.update()` and `self.feedback()` on the service directly.** After U1 moves `add` and U2 moves `batch_add`, both `update` and `feedback` are on MemoryService. The `_merge_into` helper (called only by `add`) can call `self.update()` and `self.feedback()` directly instead of routing through the orchestrator. This avoids double-hopping through delegates.

- **Module constant `_BATCH_ADD_CONCURRENCY` moves to `memory_service.py`.** It's only used by `batch_add`. The orchestrator's line 137 definition is removed when `batch_add` moves.

- **Imports are added at module top of `memory_service.py`.** Local imports inside methods (like the `from opencortex.orchestrator import _merge_unique_strings` in `update`) are acceptable for private helpers that would create import cycles. New local imports follow the same pattern.

- **Delegate stubs use `self._memory_service` (lazy property).** Matches the pattern from Plan 010. No eager-init changes.

- **Static methods (`_context_type_from_value`, `_detail_level_from_retrieval_depth`, `_summarize_retrieve_breakdown`, `_infer_context_type`) become `@staticmethod` on MemoryService.** None use `self` — they only parse strings or convert enum values. `_infer_context_type` is currently an instance method but only accesses its `uri` parameter.

---

## Open Questions

### Resolved During Planning

- **Q: Should `_add_document` (160L) move with `add` or stay on orchestrator?** → Move. It's only called by `add`. Its own calls to shared helpers (`_auto_uri`, `_resolve_unique_uri`, etc.) become `self._orch._auto_uri()` — clean and consistent with the pattern.
- **Q: Should `_build_typed_queries` and its three sub-helpers move with `search`?** → Yes. All four are only called by `search`. `_infer_context_type` (12L) is non-obviously located at line 6103 but is target-only.
- **Q: Move order — should U1 (`add`) or U3 (`search`) come first?** → `add` first. It's the largest single move (380L + 335L helpers = 715L total) and highest risk. Getting it done first means the biggest diff is validated early.

### Deferred to Implementation

- **Exact `self._X` → `self._orch._X` renames for each method.** The implementer must read each method body and exhaustively replace orchestrator-level attribute access. A missed rename surfaces at runtime. Mechanical but requires care.
- **Whether `batch_add`'s concurrency primitives interact with orchestrator state non-obviously.** The plan leans toward moving it (same guidance as plan 010). If reading the body reveals coupling to sweepers or derive workers, the implementer defers and documents the reason.

---

## Implementation Units

- [ ] U1. **Move `add` + target-only helpers**

**Goal:** Move the largest single method (`add`, 380L) and its three target-only helpers (`_add_document` 160L, `_check_duplicate` 71L, `_merge_into` 26L, `_ensure_parent_records` 78L) from orchestrator to MemoryService. Replace orchestrator originals with delegate stubs.

**Requirements:** R1, R2, R4, R5.

**Dependencies:** None (builds on Plan 010 scaffolding).

**Files:**
- Modify: `src/opencortex/services/memory_service.py` (add `add`, `_add_document`, `_check_duplicate`, `_merge_into`, `_ensure_parent_records`)
- Modify: `src/opencortex/orchestrator.py` (replace `add` body with delegate; remove `_add_document`, `_check_duplicate`, `_merge_into`, `_ensure_parent_records`)
- Modify: `tests/test_memory_service.py` (add boundary test for `add`)

**Approach:**
- Cut `add` body (L2800-L3179) verbatim into MemoryService. Rename `self.X` → `self._orch.X` for all orchestrator-level attributes (`self._embedder`, `self._storage`, `self._fs`, `self._config`, `self._entity_index`, `self._skill_manager`, `self._llm_completion`, etc.).
- Cut `_add_document` (L1738-L1897), `_check_duplicate` (L3192-L3262), `_merge_into` (L3264-L3289), `_ensure_parent_records` (L6118-L6195) into MemoryService. Same rename pattern.
- `_merge_into` currently calls `self.update()` and `self.feedback()`. After move, these become `self.update()` and `self.feedback()` on the service itself (both are on MemoryService after this plan completes). For U1, `feedback` hasn't moved yet — so `_merge_into` must call `self._orch.feedback()` temporarily. When U4 moves `feedback`, this call can be updated to `self.feedback()`.
- `_ensure_parent_records` calls shared helpers (`_derive_parent_uri`, `_extract_category_from_uri`, `_get_record_by_uri`) — these become `self._orch._derive_parent_uri()` etc.
- `_add_document` calls `_auto_uri`, `_resolve_unique_uri`, and `_generate_abstract_overview` — shared, accessed via `self._orch._auto_uri()` etc.
- Access `_ttl_from_hours` via `self._orch._ttl_from_hours()` (shared with `_write_immediate`).
- Add Google-format docstring to `add` (the orchestrator original likely has one — reformat to Google style if needed).
- Replace orchestrator's `add` with one-line delegate: `return await self._memory_service.add(...)`.
- Add imports to `memory_service.py`: `datetime`, `timezone`, `timedelta` (for TTL handling), `get_effective_identity`, `get_effective_project_id` (from request context), `IngestModeResolver` (local import inside `add` to avoid cycle), `MemoryKind`, `CortexURI`, other types as needed.

**Patterns to follow:**
- `src/opencortex/services/memory_service.py` `update` method (lines 83-295) — same rename pattern `self.X → self._orch.X`, same `orch = self._orch` local alias at method start.

**Test scenarios:**
- Happy path: `tests/test_e2e_phase1.py` — all `add`-related tests pass unchanged (tests write via `orchestrator.add()`, delegates correctly route to service).
- Happy path: `tests/test_write_dedup.py` — 11 dedup-specific tests pass (exercises `_check_duplicate` + `_merge_into` through `add`).
- Happy path: `tests/test_ingestion_e2e.py` — ingestion mode tests pass (exercises `IngestModeResolver` routing inside `add`).
- Boundary: `tests/test_memory_service.py` — mock orchestrator with fake `_storage` / `_embedder`, call `service.add(...)`, assert `upsert` was awaited with expected payload.

**Verification:**
- `add` physically lives in `memory_service.py`, not `orchestrator.py`.
- Orchestrator's `add` is a 2-line delegate.
- `_add_document`, `_check_duplicate`, `_merge_into`, `_ensure_parent_records` physically live in `memory_service.py`.
- Regression sweep (`uv run python3 -m unittest discover -s tests`) same pass/fail count as before.

---

- [ ] U2. **Move `batch_add`**

**Goal:** Move `batch_add` (123L) and its concurrency constant into MemoryService. After U1, `batch_add`'s internal `self.add()` call becomes a same-service call.

**Requirements:** R1, R2, R4, R5.

**Dependencies:** U1 (`add` must be on MemoryService first so the internal `self.add()` call works).

**Files:**
- Modify: `src/opencortex/services/memory_service.py` (add `batch_add` + `_BATCH_ADD_CONCURRENCY` constant)
- Modify: `src/opencortex/orchestrator.py` (replace `batch_add` body with delegate; remove `_BATCH_ADD_CONCURRENCY` at line 137)
- Modify: `tests/test_memory_service.py` (add boundary test for `batch_add`)

**Approach:**
- Move module constant `_BATCH_ADD_CONCURRENCY = 8` from orchestrator line 137 to top of `memory_service.py`.
- Cut `batch_add` body (L5556-L5678) verbatim into MemoryService.
- `self.add()` calls inside `batch_add` become `self.add()` on the service — same-service call, no hop through orchestrator.
- Other `self._X` accesses become `self._orch._X`.
- Replace orchestrator's `batch_add` with one-line delegate.
- Add Google-format docstring.

**Patterns to follow:**
- Same as U1.

**Test scenarios:**
- Happy path: `tests/test_batch_add_hierarchy.py` — directory hierarchy tests pass (exercises `batch_add` with `scan_meta`).
- Happy path: `tests/test_ingestion_e2e.py` — batch ingestion tests pass.
- Boundary: `tests/test_memory_service.py` — mock orchestrator, call `service.batch_add(...)` with 3 items, assert underlying `add` called 3 times.

**Verification:**
- `batch_add` lives in `memory_service.py`.
- Orchestrator's `batch_add` is a one-line delegate.
- `_BATCH_ADD_CONCURRENCY` moved to `memory_service.py`, removed from orchestrator.
- Regression sweep green.

---

- [ ] U3. **Move query methods (`search` + helpers, `list_memories`, `memory_index`, `list_memories_admin`)**

**Goal:** Move the 4 read-side methods and `search`'s target-only helpers into MemoryService.

**Requirements:** R1, R2, R4, R5.

**Dependencies:** None (queries are independent of CRUD methods).

**Files:**
- Modify: `src/opencortex/services/memory_service.py` (add `search`, `_build_typed_queries`, `_context_type_from_value`, `_detail_level_from_retrieval_depth`, `_infer_context_type`, `_summarize_retrieve_breakdown`, `list_memories`, `memory_index`, `list_memories_admin`)
- Modify: `src/opencortex/orchestrator.py` (replace all 4 public methods + 5 helpers with delegates or removal)
- Modify: `tests/test_memory_service.py` (add boundary tests for `search`, `list_memories`)

**Approach:**
- Cut `search` body (L4178-L4444) into MemoryService. This is the second-largest move (267L). Deep integration with probe/planner/runtime stack — `self.probe_memory()`, `self.plan_memory()`, `self.bind_memory_runtime()`, `self._memory_runtime` all become `self._orch.probe_memory()` etc.
- Cut target-only helpers: `_build_typed_queries` (L3403), `_context_type_from_value` (L3442, static), `_detail_level_from_retrieval_depth` (L3449, static), `_summarize_retrieve_breakdown` (L3456, static), `_infer_context_type` (L6103).
- Static methods (`_context_type_from_value`, `_detail_level_from_retrieval_depth`, `_summarize_retrieve_breakdown`, `_infer_context_type`) become `@staticmethod` on MemoryService.
- `_build_typed_queries` calls `self._infer_context_type()` — after move, this is `MemoryService._infer_context_type()` (static call, no `self` needed).
- `_build_typed_queries` calls `self._context_type_from_value()` and `self._detail_level_from_retrieval_depth()` — these become `MemoryService._context_type_from_value()` and `MemoryService._detail_level_from_retrieval_depth()` (static calls).
- Cut `list_memories` (L4722-L4824), `memory_index` (L4826-L4906), `list_memories_admin` (L4908-L4966). Standard rename pattern.
- Replace orchestrator's 4 public methods with one-line delegates.
- Remove the 5 helpers from orchestrator entirely (no delegates needed for private methods).
- Add imports: `ContextType`, `TypedQuery`, `DetailLevel`, `RetrievalPlan`, `RetrievalDepth`, `QueryResult`, `FindResult` and other retrieve types.

**Patterns to follow:**
- Same as U1. Additional attention to the probe/planner/runtime call chain in `search` — preserve the hot-path discipline exactly.

**Test scenarios:**
- Happy path: `tests/test_e2e_phase1.py` — search-related tests pass unchanged.
- Boundary: `tests/test_memory_service.py` — mock `self._orch._storage.search`, call `service.search(query="test")`, assert correct filter construction and result aggregation.
- Boundary: `tests/test_memory_service.py` — mock storage, call `service.list_memories(...)`, assert pagination parameters forwarded correctly.

**Verification:**
- `search`, `list_memories`, `memory_index`, `list_memories_admin` + 5 helpers live in `memory_service.py`.
- Orchestrator's 4 public methods are one-line delegates.
- 5 helpers removed from orchestrator.
- Regression sweep green.

---

- [ ] U4. **Move scoring + lifecycle-adjacent methods (`feedback`, `feedback_batch`, `decay`, `cleanup_expired_staging`, `protect`, `get_profile`)**

**Goal:** Move the 6 reward-scoring / decay / staging methods into MemoryService. After this, all internal cross-calls (`feedback_batch` → `feedback`, `decay` → `cleanup_expired_staging`) become same-service calls.

**Requirements:** R1, R2, R4, R5.

**Dependencies:** None (scoring methods are independent).

**Files:**
- Modify: `src/opencortex/services/memory_service.py` (add 6 methods)
- Modify: `src/opencortex/orchestrator.py` (replace 6 methods with delegate stubs)
- Modify: `tests/test_memory_service.py` (add boundary tests for scoring methods)

**Approach:**
- Cut all 6 method bodies verbatim. Standard rename pattern.
- `feedback_batch` (L5017) calls `self.feedback()` → becomes same-service call `self.feedback()`.
- `decay` (L5029) calls `self.cleanup_expired_staging()` → becomes same-service call `self.cleanup_expired_staging()`.
- **Required cleanup from U1:** update `_merge_into`'s `self._orch.feedback()` call to `self.feedback()` — the service now owns `feedback`. This is a U4 responsibility, not optional.
- `cleanup_expired_staging` accesses timestamp logic — add `datetime`/`timezone` import if not already present.
- `feedback`, `protect`, `get_profile` interact with reward scoring fields on records via `self._orch._storage` — preserve exactly.
- Replace orchestrator's 6 methods with one-line delegates.

**Patterns to follow:**
- Same as U1.

**Test scenarios:**
- Happy path: `tests/test_e2e_phase1.py` — feedback, decay, protect tests pass unchanged.
- Boundary: `tests/test_memory_service.py` — mock storage, call `service.feedback(uri, 1.0)`, assert `update_reward` called with correct parameters.
- Boundary: `tests/test_memory_service.py` — call `service.feedback_batch([...])`, assert `feedback()` called for each item.
- Boundary: `tests/test_memory_service.py` — call `service.decay()`, assert returns decay stats.

**Verification:**
- 6 methods live in `memory_service.py`.
- Orchestrator's 6 methods are one-line delegates.
- `_merge_into`'s `feedback` call updated to same-service.
- Regression sweep green.

---

- [ ] U5. **Style polish on `MemoryService`**

**Goal:** Apply Google Python Style consistently across all moved methods. This is the quality pass after all content is in place (U1-U4).

**Requirements:** R3.

**Dependencies:** U1, U2, U3, U4 (all methods landed).

**Files:**
- Modify: `src/opencortex/services/memory_service.py` (docstrings, type hints, constants)
- Modify: `tests/test_memory_service.py` (add docstring presence smoke test)

**Approach:**
- Audit every public method for Google-format docstring (Args/Returns/Raises). Add missing docstrings; reformat existing ones.
- Replace bare `Any` with specific types where knowable. Document any remaining `Any` with a comment.
- Verify module constants (`_BATCH_ADD_CONCURRENCY`) are UPPER_SNAKE with one-line comments.
- Verify imports: absolute `from opencortex.X import Y` only, no unused imports.
- Verify no relative imports.
- Update the module-level docstring if needed to reflect the completed state.

**Test scenarios:**
- Smoke: assert every public method on `MemoryService` has a non-empty `__doc__`.
- Smoke: `dir(MemoryService)` exposes all expected method names (catches accidental rename or deletion).

**Verification:**
- `MemoryService` reads as a clean, single-purpose module with consistent docstrings and type hints.
- No bare `Any` where avoidable, no missing docstrings on public methods.

---

## System-Wide Impact

- **Interaction graph:** External callers (HTTP routes in `src/opencortex/http/server.py`, admin routes in `src/opencortex/http/admin_routes.py`, MCP plugin in `plugins/opencortex-memory/`) call orchestrator methods by name. No call sites change — delegates preserve the surface.
- **Error propagation:** Moved methods raise the same exceptions. Delegates don't catch — exceptions bubble. No new error layer.
- **State lifecycle risks:** None new. MemoryService holds zero state — it's a method bag with a back-reference. All real state stays on the orchestrator.
- **API surface parity:** HTTP REST API, MCP tools, and Python API all go through the orchestrator surface, which is unchanged.
- **Integration coverage:** 140+ existing tests cover every public surface being delegated. New `test_memory_service.py` boundary tests cover the new module boundary.
- **Unchanged invariants:** `ContextManager`, all alpha components, cognition module, storage adapters, embedders, recall planner, intent router — all untouched.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Missed `self._X` → `self._orch._X` rename in `add` (380L) causes runtime AttributeError | Existing integration tests (140+ Python tests) exercise every code path through `add`. A missed rename surfaces immediately. The `add` method is the highest-risk move — U1 isolates it for focused verification. |
| `_merge_into` cross-service call routing breaks after partial move | U1 uses `self._orch.feedback()` temporarily; U4 updates to `self.feedback()`. The temporary double-hop works because the orchestrator delegate routes to the service. |
| `_add_document` at line 1738 is physically distant from `add` at line 2799 — easy to miss during move | The plan explicitly lists all helpers with their line ranges. The implementer checks off each one. |
| `batch_add` concurrency primitives interact with orchestrator state non-obviously | `batch_add`'s `asyncio.Semaphore` is self-contained. The only state access is through `self.add()` (which moves to the service) and `self._generate_abstract_overview()` (shared, accessed via `self._orch`). Low risk. |
| Import cycle when `memory_service.py` imports from `orchestrator.py` for private helpers | Follow the `update` method precedent: local imports inside the method (`from opencortex.orchestrator import _merge_unique_strings`). Module-level imports use absolute paths from other packages. |

---

## Sources & References

- **Origin plan:** `docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md`
- **Repo-wide audit:** `docs/refactor/2026-04-25-repo-wide-god-object-audit.md`
- **Pattern reference:** `src/opencortex/context/benchmark_ingest_service.py`
- **Current module:** `src/opencortex/services/memory_service.py`
- **Hot-path learning:** `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md`
- **Scoped probe learning:** `docs/solutions/best-practices/single-bucket-scoped-probe-2026-04-16.md`
