---
title: Refactor Memory Record Projection URI Logic Into MemoryRecordService
created: 2026-04-28
status: completed
type: refactor
origin: "$lfg 抽离 MemoryOrchestrator 的 memory record/projection/URI 领域逻辑"
---

# Refactor Memory Record Projection URI Logic Into MemoryRecordService

## Problem Frame

`MemoryOrchestrator` is now mostly a facade, but memory record concerns are
still split across awkward ownership boundaries:

- `MemoryOrchestrator` owns URI and small record helpers such as `_auto_uri`,
  `_resolve_unique_uri`, `_extract_category_from_uri`, `_enrich_abstract`, and
  `_derive_parent_uri`.
- `DerivationService` still owns canonical `abstract_json`, object payload,
  anchor projection, fact-point projection, and stale derived-record cleanup,
  even though these are record/projection concerns rather than LLM derivation.
- `MemoryService`, `DerivationService`, and `SessionLifecycleService` all call
  back through orchestrator wrappers for these helpers.

This phase should create a dedicated `MemoryRecordService` for memory record
shape, derived projection records, and URI utilities. The orchestrator must
keep the same method names as compatibility wrappers.

## Requirements

- R1: Add `src/opencortex/services/memory_record_service.py` as the owner for
  memory record shape, anchor/fact projection, derived cleanup, and URI helper
  behavior.
- R2: Add a lazy `MemoryOrchestrator._memory_record_service` property matching
  the existing service extraction pattern.
- R3: Keep existing `MemoryOrchestrator` wrappers for `_build_abstract_json`,
  `_memory_object_payload`, `_anchor_projection_prefix`, `_fact_point_prefix`,
  `_is_valid_fact_point`, `_fact_point_records`, `_anchor_projection_records`,
  `_delete_derived_stale`, `_sync_anchor_projection_records`, `_ttl_from_hours`,
  `_auto_uri`, `_uri_exists`, `_resolve_unique_uri`, `_extract_category_from_uri`,
  `_enrich_abstract`, and `_derive_parent_uri`.
- R4: Move record/projection implementations out of `DerivationService` into
  `MemoryRecordService`, leaving `DerivationService` focused on LLM-backed
  derivation and deferred derive completion.
- R5: Preserve `MemoryService`, `DerivationService`, and
  `SessionLifecycleService` call behavior by continuing to route through
  orchestrator wrappers where compatibility matters.
- R6: Preserve all current record shapes, URI formats, anchor/fact projection
  digest schemes, stale cleanup semantics, TTL formatting, and abstract
  enrichment behavior.
- R7: Run focused record/projection/URI tests plus style gates.

## Scope Boundaries

- Do not change public API behavior or HTTP routes.
- Do not redesign `abstract_json`, `anchor_hits`, `anchor_projection`, or
  `fact_point` schemas.
- Do not alter the existing write-time anchor/fact-point quality rules.
- Do not change ContextManager recomposition URI formats.
- Do not change storage filter DSL semantics or Qdrant adapter behavior.
- Do not remove orchestrator compatibility wrappers in this phase.
- Do not split `MemoryService.add/update/remove` yet; this phase only moves
  shared record/projection/URI helpers.

## Current Code Evidence

- `src/opencortex/orchestrator.py` implements URI helpers and abstract
  enrichment directly.
- `src/opencortex/orchestrator.py` currently wraps record/projection helpers by
  delegating to `DerivationService`.
- `src/opencortex/services/derivation_service.py` implements
  `_build_abstract_json`, `_memory_object_payload`, `_anchor_projection_records`,
  `_fact_point_records`, `_delete_derived_stale`, and
  `_sync_anchor_projection_records`.
- `src/opencortex/services/memory_service.py` calls these helpers through
  `orch.*` during add/update/merge flows.
- `src/opencortex/services/session_lifecycle_service.py` calls these helpers
  through `orch.*` from immediate message writes.
- Tests in `tests/test_context_manager.py`, `tests/test_e2e_phase1.py`,
  `tests/test_multi_tenant.py`, and `tests/test_recall_planner.py` call or patch
  orchestrator wrappers directly.

## Key Technical Decisions

- Keep `MemoryRecordService` as a back-reference service over orchestrator-owned
  config, storage, embedder, and collection state. Use explicit bridge helpers;
  do not introduce `__getattr__`.
- Move implementation bodies, not call sites, where compatibility is valuable.
  `MemoryService`, `DerivationService`, and `SessionLifecycleService` may keep
  calling `orch._build_abstract_json(...)`, `orch._auto_uri(...)`, etc.
- Leave `_merge_unique_strings` and `_split_keyword_string` exported from
  `derivation_service.py` and imported by `orchestrator.py`, because current
  `MemoryService.add()` imports them from `opencortex.orchestrator` for
  compatibility.
- Preserve static/class-style call compatibility for helpers used externally,
  especially `MemoryOrchestrator._enrich_abstract(...)` and
  `MemoryOrchestrator._fact_point_prefix(...)`.

## Implementation Units

### U1. Add MemoryRecordService shell and lazy property

**Goal:** Establish the new record/projection/URI owner.

**Files:**
- Add: `src/opencortex/services/memory_record_service.py`
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
- Add `_memory_record_service_instance` in orchestrator initialization.
- Add lazy `_memory_record_service` property.
- Add explicit service bridges for storage, embedder, collection name, user
  memory categories, and identity/project helpers where needed.

**Test Scenarios:**
- Importing `MemoryOrchestrator` still works.
- `MemoryOrchestrator.__new__` fixtures can access wrappers without requiring
  eager service construction.

### U2. Move record payload and projection implementations

**Goal:** Move canonical memory record shape and derived projection record logic
out of `DerivationService`.

**Files:**
- Modify: `src/opencortex/services/memory_record_service.py`
- Modify: `src/opencortex/services/derivation_service.py`
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
- Move `_build_abstract_json`, `_memory_object_payload`,
  `_anchor_projection_prefix`, `_fact_point_prefix`, `_is_valid_fact_point`,
  `_fact_point_records`, `_anchor_projection_records`, `_delete_derived_stale`,
  and `_sync_anchor_projection_records` implementations into
  `MemoryRecordService`.
- Replace `DerivationService` methods with thin delegates to orchestrator
  wrappers, or remove only if no internal/external caller needs the name.
- Keep orchestrator wrappers delegating to `MemoryRecordService`.

**Test Scenarios:**
- Anchor projection records preserve URI, metadata, ACL fields, and vector
  fields.
- Fact-point records preserve quality gates, digest URI scheme, ACL inheritance,
  and cap of 8 records.
- `_delete_derived_stale` still avoids sibling-prefix over-delete.

### U3. Move URI, TTL, and abstract enrichment helpers

**Goal:** Move remaining memory record utility logic out of `MemoryOrchestrator`.

**Files:**
- Modify: `src/opencortex/services/memory_record_service.py`
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
- Move `_ttl_from_hours`, `_auto_uri`, `_uri_exists`, `_resolve_unique_uri`,
  `_extract_category_from_uri`, `_enrich_abstract`, and `_derive_parent_uri`
  implementations into `MemoryRecordService`.
- Preserve deterministic semantic-node URI behavior, request identity scoping,
  project scoping for resources, conflict suffix behavior, and invalid-URI
  fallback behavior.
- Preserve static wrapper compatibility for `_extract_category_from_uri`,
  `_enrich_abstract`, and `_derive_parent_uri` where currently used.

**Test Scenarios:**
- `tests/test_e2e_phase1.py` auto URI routing table still passes.
- `tests/test_multi_tenant.py` auto URI identity scoping still passes.
- `src/opencortex/migration/v032_overview_first.py` can still call
  `MemoryOrchestrator._enrich_abstract(...)`.

### U4. Review, todo-resolve, browser gate, and PR

**Goal:** Complete the LFG pipeline.

**Files:**
- No planned files beyond U1-U3.

**Approach:**
- Review diff for schema drift, import cycles, wrapper gaps, and accidental
  behavior changes.
- Run review/autofix and persist any safe fixes.
- Inspect `.context/compound-engineering/todos/` and resolve only ready todos.
- Browser gate is expected to be no-op because this is backend-only.

**Validation Commands:**
- `uv run --group dev pytest tests/test_context_manager.py tests/test_e2e_phase1.py tests/test_multi_tenant.py tests/test_recall_planner.py tests/test_conversation_immediate.py -q`
- `uv run --group dev pytest tests/test_cascade_qdrant_integration.py::TestDeleteDerivedStale -q`
- `uv run --group dev ruff check .`
- `uv run --group dev ruff format --check .`

## Risks

| Risk | Mitigation |
|------|------------|
| Anchor/fact projection schema drift | Move code verbatim and run projection-heavy tests |
| Tests patch orchestrator wrappers | Keep wrappers and route call sites through them |
| Static helper compatibility breaks migration code | Preserve `MemoryOrchestrator._enrich_abstract(...)` wrapper |
| Import cycle between services | Use `TYPE_CHECKING` and local runtime imports where needed |
| Stale cleanup over-deletes sibling prefixes | Preserve literal `startswith` guard and run cascade test |
