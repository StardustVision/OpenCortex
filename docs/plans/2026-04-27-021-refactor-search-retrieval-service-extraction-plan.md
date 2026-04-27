---
title: Refactor Search Retrieval Logic Into RetrievalService
created: 2026-04-27
status: active
type: refactor
origin: "$lfg 抽离 MemoryOrchestrator 的 search/retrieve 领域逻辑"
---

# Refactor Search Retrieval Logic Into RetrievalService

## Problem Frame

`MemoryOrchestrator` has already shed write, derive, knowledge, system-status,
background-lifecycle, and bootstrap responsibilities, but the search/retrieve
domain is still implemented directly on it. `MemoryService.search()` currently
acts as the public `search()` implementation, yet it calls back into
orchestrator private methods for the actual retrieval work:

- probe/planner/runtime binding: `probe_memory`, `plan_memory`,
  `bind_memory_runtime`
- search scope construction: `_build_search_filter`, `_build_probe_filter`
- object retrieval execution: `_execute_object_query`
- dense query embedding and cone rerank: `_embed_retrieval_query`,
  `_apply_cone_rerank`, `_cone_query_entities`
- scoring/assembly helpers: `_score_object_record`, `_record_passes_acl`,
  `_matched_record_anchors`, `_records_to_matched_contexts`, `_aggregate_results`
- session-aware retrieval: `session_search`

This keeps `MemoryOrchestrator` large and makes `MemoryService.search()` depend
on an orchestrator back-reference for most search-domain behavior. This phase is
a behavior-preserving extraction. It should reduce orchestrator ownership
without redesigning retrieval semantics, probe/planner contracts, runtime
binding, filters, cone scoring, or result shapes.

## Requirements

- R1: Create `src/opencortex/services/retrieval_service.py` to own
  search/retrieve domain logic now implemented directly on
  `MemoryOrchestrator`.
- R2: Keep existing `MemoryOrchestrator` public/private methods as thin
  compatibility wrappers so current tests and call sites that patch private
  methods still work.
- R3: Move object-query execution and helper methods into `RetrievalService`
  without changing three-layer search behavior over leaf, anchor projection, and
  fact-point surfaces.
- R4: Move probe/planner/runtime binding helpers into `RetrievalService` while
  preserving the current `SearchResult`, `RetrievalPlan`, and runtime-result
  contracts.
- R5: Move `session_search` into `RetrievalService` behind an orchestrator
  wrapper, preserving LLM requirement behavior and query-plan/result fields.
- R6: Update `MemoryService.search()` to depend on the retrieval service where
  safe, while preserving orchestrator monkeypatch compatibility for methods
  commonly patched by tests.
- R7: Preserve current performance-sensitive behavior: bounded candidate caps,
  three parallel storage searches, URI projection batch load, URI path scoring,
  cone rerank, no awaited recall bookkeeping on the hot path, and no retrieval
  time HyDE callback.
- R8: Targeted retrieval, search, cone/rerank, recall planner, and context
  manager tests must pass along with style gates.

## Scope Boundaries

- Do not change public HTTP/API behavior.
- Do not redesign `MemoryService.search()` result aggregation or skill-search
  merging beyond moving search-domain calls out of orchestrator.
- Do not change the `IntentRouter`, `MemoryProbe`, `RecallPlanner`, or
  `MemoryRuntime` algorithms.
- Do not alter benchmark datasets, baseline reports, or retrieval scoring
  constants.
- Do not remove compatibility wrappers from `MemoryOrchestrator` in this phase.
- Do not fix unrelated full-suite environmental failures such as live server
  auth, local embedder mocks, or Python event-loop ordering in system-status
  tests.

## Current Code Evidence

- `src/opencortex/orchestrator.py` still contains the `Search / Retrieve`
  section from `probe_memory` through `session_search`.
- `src/opencortex/services/memory_service.py` implements `search()` but calls
  back into `orch.probe_memory`, `orch.plan_memory`, `orch.bind_memory_runtime`,
  `orch._build_search_filter`, `orch._execute_object_query`, and
  `orch._aggregate_results`.
- `tests/test_object_rerank.py` and `tests/test_object_cone.py` patch
  `orch._embed_retrieval_query` and call `orch._execute_object_query` directly.
- `tests/test_context_manager.py` patches `orch.probe_memory` and
  `orch.plan_memory` in recall-mode tests, and directly covers
  `_execute_object_query` scope behavior.
- `tests/test_perf_fixes.py` patches `oc._execute_object_query`,
  `oc.bind_memory_runtime`, and runtime finalize behavior to assert hot-path
  performance properties.

## Key Technical Decisions

- Add a lazy `MemoryOrchestrator._retrieval_service` property matching the
  existing `_memory_service`, `_derivation_service`, `_knowledge_service`,
  `_system_status_service`, `_background_task_manager`, and `_bootstrapper`
  service pattern.
- Keep `RetrievalService` as a behavior-preserving back-reference service. Use
  explicit bridge properties/methods for orchestrator-owned subsystems instead
  of a broad `__getattr__` fallback.
- Preserve orchestrator wrappers for all currently visible names:
  `probe_memory`, `memory_probe_mode`, `memory_probe_trace`, `plan_memory`,
  `bind_memory_runtime`, `_build_search_filter`, `_build_probe_filter`,
  `_cone_query_entities`, `_apply_cone_rerank`, `_embed_retrieval_query`,
  `_score_object_record`, `_record_passes_acl`, `_matched_record_anchors`,
  `_records_to_matched_contexts`, `_execute_object_query`, `search`,
  `session_search`, and `_aggregate_results`.
- Inside `RetrievalService`, route calls through orchestrator wrappers where
  existing tests may monkeypatch the orchestrator instance. In particular,
  `MemoryService.search()` should continue to call `orch.probe_memory`,
  `orch.plan_memory`, `orch.bind_memory_runtime`, and `orch._execute_object_query`
  unless a targeted compatibility test proves it is safe to bypass them.
- Treat `_aggregate_results` as retrieval-domain assembly and move its
  implementation with a wrapper, because both `MemoryService.search()` and
  `session_search()` depend on it.

## Implementation Units

### U1. Add RetrievalService shell and lazy property

**Goal:** Establish the new search-domain owner without changing behavior.

**Files:**
- Add: `src/opencortex/services/retrieval_service.py`
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
- Add `RetrievalService(self)` lazy property on `MemoryOrchestrator`.
- Add explicit bridge accessors for config, storage, fs, embedder, probe,
  planner, runtime, analyzer, cone scorer, entity index, collection name, and
  initialization checks.
- Do not move behavior in this unit except the smallest no-op wrappers needed to
  keep imports clean.

**Tests:**
- Existing import/startup tests through `tests/test_recall_planner.py`.
- Ruff import and format checks.

### U2. Move probe, planner, runtime binding, and filter construction

**Goal:** Move Phase 1/2/3 retrieval orchestration helpers out of
`MemoryOrchestrator`.

**Files:**
- Modify: `src/opencortex/services/retrieval_service.py`
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
- Move bodies for `probe_memory`, `memory_probe_mode`, `memory_probe_trace`,
  `plan_memory`, `bind_memory_runtime`, `_build_search_filter`, and
  `_build_probe_filter`.
- Leave orchestrator methods as thin delegates.
- Preserve `get_effective_identity()` and `get_effective_project_id()` behavior
  inside the moved implementations.

**Tests:**
- `tests/test_recall_planner.py`
- `tests/test_context_manager.py::TestRecallMode`
- Any existing tests that inspect `memory_probe_trace` or probe/planner behavior.

### U3. Move object-query execution and ranking helpers

**Goal:** Make `RetrievalService` own object-aware retrieval, path projection,
rerank, scoring, and matched-context assembly.

**Files:**
- Modify: `src/opencortex/services/retrieval_service.py`
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
- Move `_cone_query_entities`, `_apply_cone_rerank`,
  `_embed_retrieval_query`, `_score_object_record`, `_record_passes_acl`,
  `_matched_record_anchors`, `_records_to_matched_contexts`, and
  `_execute_object_query`.
- Keep orchestrator wrappers so tests that patch `_embed_retrieval_query` or call
  `_execute_object_query` keep working.
- In service-owned `_execute_object_query`, call `self._orch._embed_retrieval_query`
  and related wrappers where monkeypatch compatibility matters.
- Preserve log labels only when they identify the caller; new service-only
  warnings/debug logs should use `RetrievalService`.

**Tests:**
- `tests/test_object_rerank.py`
- `tests/test_object_cone.py`
- `tests/test_context_manager.py` object-query scope tests around
  `_execute_object_query`
- `tests/test_perf_fixes.py` search hot-path tests

### U4. Move aggregate and session-aware retrieval

**Goal:** Finish the search/retrieve extraction by moving result aggregation and
LLM-assisted session search.

**Files:**
- Modify: `src/opencortex/services/retrieval_service.py`
- Modify: `src/opencortex/orchestrator.py`
- Modify: `src/opencortex/services/memory_service.py` only if direct dependency
  on `RetrievalService` is cleaner without breaking monkeypatches.

**Approach:**
- Move `_aggregate_results` implementation.
- Move `session_search` implementation behind an orchestrator wrapper.
- Keep `MemoryService.search()` behavior stable. Prefer minimal call-site churn;
  its current orchestration may remain there for now as long as it no longer
  depends on large orchestrator method bodies.

**Tests:**
- `tests/test_e2e_phase1.py` session/search coverage
- `tests/test_recall_planner.py`
- `tests/test_context_manager.py`

### U5. Review, todo-resolve, browser stage, and PR

**Goal:** Complete LFG gates and prove the refactor is behavior-preserving.

**Files:**
- No planned production files beyond the units above.

**Approach:**
- Run a manual review/autofix pass focused on hidden behavior drift, monkeypatch
  compatibility, import cycles, and accidental broad delegation.
- Run todo-resolve; only ready todos should be fixed.
- Browser stage is expected to be no-op unless frontend files are touched.

**Validation Commands:**
- `uv run --group dev pytest tests/test_recall_planner.py tests/test_object_rerank.py tests/test_object_cone.py tests/test_perf_fixes.py tests/test_context_manager.py tests/test_e2e_phase1.py -q`
- `uv run --group dev pytest tests/test_ingestion_e2e.py tests/test_reward_integration.py -q`
- `uv run --group dev ruff check .`
- `uv run --group dev ruff format --check .`

## Risks

| Risk | Mitigation |
|------|------------|
| Existing tests monkeypatch orchestrator private retrieval helpers | Keep wrappers and route service internals through wrappers where needed |
| Import cycle between orchestrator, memory service, and retrieval service | Keep runtime imports localized and use `TYPE_CHECKING` for orchestrator types |
| Search behavior drift from moving aggregation/scoring | Run object rerank, cone, context-manager, recall-planner, and perf tests |
| Hot-path latency regression | Preserve parallel storage search and keep recall bookkeeping fire-and-forget |
| Service boundary still too implicit | Use explicit bridge properties/methods; do not add a broad `__getattr__` |

## Done Criteria

- `RetrievalService` contains the search/retrieve-domain implementation.
- `MemoryOrchestrator` no longer contains large search/retrieve method bodies,
  only thin delegates for compatibility.
- `MemoryService.search()` behavior and monkeypatch compatibility are preserved.
- Targeted retrieval/search tests and ruff gates pass.
- Any full-suite failures are classified as in-scope regressions or pre-existing
  unrelated failures before final reporting.
