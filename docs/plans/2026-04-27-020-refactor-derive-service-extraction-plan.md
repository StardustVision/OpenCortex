---
title: Refactor Derive Domain Logic Into DerivationService
created: 2026-04-27
status: active
type: refactor
origin: "$lfg 将derive的领域逻辑解耦"
---

# Refactor Derive Domain Logic Into DerivationService

## Problem Frame

Document and conversation derive execution is already asynchronous through
`_derive_queue` and `BackgroundTaskManager`, but the derive domain logic still
lives on `MemoryOrchestrator`. `MemoryService`, background workers,
`ContextManager`, recomposition, and benchmark ingest all call orchestrator
private methods such as `_derive_layers`, `_complete_deferred_derive`, and
`_sync_anchor_projection_records`. This keeps `MemoryOrchestrator` large and
forces unrelated services to treat it as the owner of LLM derivation, derived
record projection, embedding refresh, and CortexFS writeback.

This phase is a behavior-preserving extraction. It should reduce orchestrator
ownership without redesigning queue semantics, retrieval semantics, or document
chunk topology.

## Requirements

- R1: Create a `DerivationService` that owns derive-domain algorithms currently
  implemented directly in `MemoryOrchestrator`.
- R2: Keep existing `MemoryOrchestrator` private methods as thin compatibility
  wrappers so current tests and call sites that monkeypatch them still work.
- R3: Move the `_DeriveTask` data shape out of `orchestrator.py` so queue task
  type ownership belongs to the derivation domain, not the top-level facade.
- R4: Update internal imports/call sites where safe so new code can depend on
  `DerivationService` directly, while preserving behavior for existing wrapper
  calls.
- R5: Preserve current derive behavior: LLM fallback, retry behavior, keyword /
  entity / anchor / fact-point extraction, abstract JSON construction,
  projection record sync, stale projection cleanup, deferred derive counters,
  CortexFS writeback, and vector refresh.
- R6: Keep queue scheduling in `BackgroundTaskManager`; this refactor does not
  replace `_derive_queue`, `.derive_pending` recovery, or worker lifecycle.
- R7: Targeted derive, document async derive, benchmark/recomposition deferred
  derive, and style gates must pass.

## Scope Boundaries

- Do not change public HTTP/API behavior.
- Do not redesign document chunking, bottom-up summary ordering, or recovery
  marker format.
- Do not extract retrieval/object-query logic in this phase.
- Do not remove compatibility wrappers from `MemoryOrchestrator`; that can be a
  later cleanup after downstream call sites and tests stop patching them.
- Do not add a new dependency-injection framework. Use the repo's current
  back-reference service pattern.

## Current Code Evidence

- `src/opencortex/orchestrator.py` still defines `_DeriveTask`, owns
  `_derive_queue`, and directly implements `_derive_layers`,
  `_complete_deferred_derive`, `_derive_parent_summary`,
  `_build_abstract_json`, `_memory_object_payload`, and
  `_sync_anchor_projection_records`.
- `src/opencortex/lifecycle/background_tasks.py` consumes `_derive_queue`, but
  calls back into `orch.add`, `orch.update`, and `orch._derive_parent_summary`.
- `src/opencortex/services/memory_service.py` calls
  `orch._derive_layers` and `orch._sync_anchor_projection_records` during add
  and update.
- `src/opencortex/context/benchmark_ingest_service.py` and
  `src/opencortex/context/recomposition_engine.py` call
  `orch._complete_deferred_derive` for deferred conversation derive.
- Tests currently patch or call orchestrator private derive methods directly,
  especially in `tests/test_context_manager.py`,
  `tests/test_document_async_derive.py`,
  `tests/test_benchmark_ingest_lifecycle.py`, and
  `tests/test_recall_planner.py`.

## Key Technical Decisions

- Add `src/opencortex/services/derivation_service.py`.
- Move `DeriveTask` into that module as the domain-owned task dataclass.
- Add `MemoryOrchestrator._derivation_service` lazy property matching existing
  `_memory_service`, `_knowledge_service`, and lifecycle service patterns.
- Implement compatibility wrappers on `MemoryOrchestrator` for the existing
  private names. Each wrapper should delegate to `self._derivation_service`.
- Keep `_derive_queue` on `MemoryOrchestrator` for now because
  `BackgroundTaskManager` owns worker lifecycle and close semantics. Only the
  task type moves.
- Prefer minimal call-site churn. Update imports from `_DeriveTask` to
  `DeriveTask`; leave wrapper-based method calls where tests currently rely on
  monkeypatching the orchestrator method.

## Implementation Units

### U1. Add DerivationService shell and task type

**Goal:** Establish the new domain owner without changing behavior.

**Files:**
- Add: `src/opencortex/services/derivation_service.py`
- Modify: `src/opencortex/orchestrator.py`
- Modify: `src/opencortex/lifecycle/background_tasks.py`
- Modify: `src/opencortex/services/memory_service.py`
- Modify: tests importing `_DeriveTask`

**Approach:**
- Move `_DeriveTask` to `DeriveTask` in the new service module.
- Update queue type annotations and imports.
- Add lazy `_derivation_service` property.

### U2. Move derive-layer and summary logic

**Goal:** Make `DerivationService` own LLM derive and fallback behavior.

**Files:**
- Modify: `src/opencortex/services/derivation_service.py`
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
- Move logic for parent summary, layer derivation, split-field derivation,
  retryable LLM completion, overview fallback, abstract derivation, and coercion.
- Keep wrappers named `_derive_parent_summary`, `_derive_layers`,
  `_derive_layers_split_fields`, `_derive_layers_llm_completion`,
  `_fallback_overview_from_content`, and `_derive_abstract_from_overview`.

### U3. Move abstract JSON and projection sync logic

**Goal:** Move derived record construction and projection synchronization out of
the orchestrator body.

**Files:**
- Modify: `src/opencortex/services/derivation_service.py`
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
- Move `_build_abstract_json`, `_memory_object_payload`,
  `_anchor_projection_prefix`, `_fact_point_prefix`,
  `_is_valid_fact_point`, `_fact_point_records`,
  `_anchor_projection_records`, `_delete_derived_stale`, and
  `_sync_anchor_projection_records`.
- Preserve wrappers for compatibility and test monkeypatching.

### U4. Move deferred derive completion

**Goal:** Move deferred conversation/document derive completion into
`DerivationService`.

**Files:**
- Modify: `src/opencortex/services/derivation_service.py`
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
- Move `_complete_deferred_derive` logic.
- Keep counter state (`_deferred_derive_count`) on orchestrator for
  `SystemStatusService.wait_deferred_derives`, but have the service increment and
  decrement it through the orchestrator back-reference.
- Preserve the wrapper so tests and existing deferred call sites continue to
  intercept `orch._complete_deferred_derive`.

### U5. Verification and review

**Goal:** Prove the extraction is behavior-preserving.

**Commands:**
- `uv run --group dev pytest tests/test_context_manager.py tests/test_document_async_derive.py tests/test_background_task_manager.py tests/test_benchmark_ingest_lifecycle.py tests/test_recall_planner.py tests/test_vectorization_expansion.py -q`
- `uv run --group dev ruff check .`
- `uv run --group dev ruff format --check .`

## Risks

| Risk | Mitigation |
|------|------------|
| Existing tests monkeypatch orchestrator private methods | Keep wrappers and avoid bypassing them in deferred call sites |
| Import cycle between orchestrator and service | Use local imports where necessary and keep service constructor as back-reference only |
| Moving static helpers breaks callers | Preserve wrapper names on orchestrator and update only safe imports |
| Behavior drift in derived records | Run projection and context-manager tests that cover anchors/fact points |

## Done Criteria

- `DerivationService` contains the derive-domain implementation.
- `MemoryOrchestrator` no longer contains large derive method bodies, only thin
  delegates for compatibility.
- `_DeriveTask` no longer lives in `orchestrator.py`.
- Targeted derive-related tests and ruff gates pass.
