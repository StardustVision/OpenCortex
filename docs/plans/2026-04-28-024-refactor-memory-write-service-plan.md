---
title: Refactor MemoryService Write Mutation Logic Into MemoryWriteService
created: 2026-04-28
status: completed
type: refactor
origin: "$lfg 抽离 MemoryService 的 write/mutation 领域逻辑，保持class尽量在1000行以内"
---

# Refactor MemoryService Write Mutation Logic Into MemoryWriteService

## Problem Frame

After the latest orchestrator extractions, `MemoryService` is now the largest
class in the memory stack at more than 2000 lines. It mixes write/mutation
flows, scoring/lifecycle adjuncts, search, listing, and admin index behavior in
one class.

The next cut should split the write path into a dedicated application service
while preserving the current public facade. HTTP routes, MCP, ContextManager,
benchmarks, and tests should continue to call `MemoryOrchestrator` or
`MemoryService` through the same method names.

## Requirements

- R1: Add `src/opencortex/services/memory_write_service.py` as the owner for
  memory write/mutation behavior.
- R2: Keep `MemoryService` public/private method names as compatibility wrappers
  for `add`, `update`, `remove`, `batch_add`, `_check_duplicate`, `_merge_into`,
  `_ensure_parent_records`, and `_add_document`.
- R3: Move write/mutation implementations out of `MemoryService` without
  changing record shapes, URI behavior, dedup behavior, document ingest,
  deferred derive scheduling, parent record creation, CortexFS writes, entity
  index updates, autophagy bootstrap, or cascade removal.
- R4: Keep `MemoryService` focused on search/listing plus compatibility
  wrappers after extraction.
- R5: Keep each edited service class as close as practical to 1000 lines or
  below. `MemoryService` should drop below 1000 lines after the write path
  extraction; the new `MemoryWriteService` should also remain below 1000 lines.
- R6: Preserve monkeypatch compatibility for tests that patch `MemoryService`
  private helpers, scoring methods, batch constants, or `MemoryOrchestrator.add`.
- R7: Run focused write/mutation, document ingest, batch add, context manager,
  immediate write, and style checks.

## Scope Boundaries

- Do not move search/retrieve logic in this phase.
- Do not move feedback, decay, protect, profile, or listing/admin-index methods
  in this phase unless line-count verification proves absolutely necessary.
- Do not change HTTP route behavior or API response shapes.
- Do not redesign `MemoryOrchestrator` facade methods.
- Do not change storage adapter behavior, Qdrant filter semantics, CortexFS
  path rules, or embedding strategy.
- Do not remove compatibility wrappers from `MemoryService`.

## Current Code Evidence

- `src/opencortex/services/memory_service.py` has `add`, `update`, `remove`,
  `_check_duplicate`, `_merge_into`, `_ensure_parent_records`, `_add_document`,
  and `batch_add` before the scoring/search/listing sections.
- `src/opencortex/orchestrator.py` delegates public write methods to
  `MemoryService`.
- `ContextManager`, document-mode tests, batch-add tests, ingestion tests, and
  e2e tests call write methods through `MemoryOrchestrator`.
- `tests/test_memory_service.py` explicitly asserts `MemoryService` helper
  method names exist, so wrapper compatibility matters.

## Key Technical Decisions

- Add a lazy `MemoryService._memory_write_service` property rather than placing
  the new service on `MemoryOrchestrator`. This keeps the public orchestration
  layer stable and makes `MemoryService` the compatibility facade for memory
  domain methods.
- Use a back-reference from `MemoryWriteService` to `MemoryService`, and access
  the orchestrator through `self._service._orch`. This avoids duplicating
  all bridge methods and preserves helper monkeypatch compatibility through the
  wrapper surface.
- Inside extracted services, call facade wrappers where existing tests may
  monkeypatch the facade.
- Keep constants `_BATCH_ADD_CONCURRENCY` and `_BATCH_ADD_TASK_CHUNK_SIZE` in
  `memory_service.py` for existing test monkeypatch compatibility; the document
  write service reads them dynamically.
- Add `MemoryDocumentWriteService` and `MemoryScoringService` once line-count
  verification proved a single write service would still exceed 1000 lines.
- Update the module docstring of `MemoryService` to describe its new facade plus
  search/listing responsibility.

## Implementation Units

### U1. Add MemoryWriteService shell and MemoryService lazy property

**Goal:** Establish the new write-domain owner without behavior changes.

**Files:**
- Add: `src/opencortex/services/memory_write_service.py`
- Modify: `src/opencortex/services/memory_service.py`

**Approach:**
- Add `MemoryWriteService(memory_service)` with a `_service` back-reference and
  an `_orch` convenience property.
- Add `MemoryService._memory_write_service` lazy property using `getattr` so
  `MemoryService.__new__` fixtures do not fail on missing attributes.
- Do not move behavior in this unit except minimal import-safe scaffolding.

**Test Scenarios:**
- `tests/test_memory_service.py` still sees the expected helper names.
- Importing `MemoryService` and `MemoryWriteService` has no cycle failure.

### U2. Move update/remove/add/private write helpers

**Goal:** Move single-record mutation behavior into `MemoryWriteService`.

**Files:**
- Modify: `src/opencortex/services/memory_write_service.py`
- Modify: `src/opencortex/services/memory_service.py`

**Approach:**
- Move implementations for `update`, `remove`, `add`, `_check_duplicate`,
  `_merge_into`, `_ensure_parent_records`, and `_add_document`.
- Replace the original `MemoryService` methods with thin wrappers.
- Preserve local imports of `_merge_unique_strings` and `_split_keyword_string`
  inside moved methods to avoid import-cycle churn.
- Preserve helper calls through the `MemoryService` wrapper where monkeypatch
  compatibility matters.

**Test Scenarios:**
- E2E add/update/remove behavior remains unchanged.
- Fact-point projection survives update and merge paths.
- Remove still cascades anchor/fact projections.
- Document-mode add still creates parent/chunk records and deferred derive state.

### U3. Move document and batch write flow

**Goal:** Move document and batch write behavior into a focused service.

**Files:**
- Add: `src/opencortex/services/memory_document_write_service.py`
- Modify: `src/opencortex/services/memory_write_service.py`
- Modify: `src/opencortex/services/memory_service.py`

**Approach:**
- Move `_add_document` and `batch_add` implementation to
  `MemoryDocumentWriteService`.
- Keep `MemoryService.batch_add` and `MemoryService._add_document` as wrappers
  through `MemoryWriteService`.
- Keep batch constants in `memory_service.py` and read them dynamically from the
  document write service to preserve monkeypatch behavior.
- Preserve directory creation order, batch chunking, concurrency limits, and
  error propagation.

**Test Scenarios:**
- `tests/test_batch_add_hierarchy.py`
- `tests/test_perf_fixes.py` batch-add cancellation/concurrency tests.

### U4. Move scoring/lifecycle mutation adjuncts

**Goal:** Keep `MemoryService` below 1000 lines after write extraction.

**Files:**
- Add: `src/opencortex/services/memory_scoring_service.py`
- Modify: `src/opencortex/services/memory_service.py`

**Approach:**
- Move `feedback`, `feedback_batch`, `decay`, `cleanup_expired_staging`,
  `protect`, and `get_profile` to `MemoryScoringService`.
- Keep same method names as wrappers on `MemoryService`.
- Route internal scoring calls through the facade for monkeypatch compatibility.

**Test Scenarios:**
- `tests/test_reward_integration.py`
- `tests/test_memory_service.py`

### U5. Validate class size and run LFG gates

**Goal:** Prove the extraction is behavior-preserving and line-count goals were
met as far as practical.

**Files:**
- No planned production files beyond U1-U4.

**Approach:**
- Run `wc -l` on `MemoryService` and `MemoryWriteService`.
- Review diff for behavior drift, wrapper coverage, import cycles, and
  monkeypatch compatibility.
- Run ce-review/autofix, todo-resolve, and browser gate.

**Observed Results:**
- `MemoryService`: 939 lines.
- `MemoryWriteService`: 933 lines.
- `MemoryDocumentWriteService`: 349 lines.
- `MemoryScoringService`: 231 lines.
- `tests/test_memory_service.py -q`: pass.
- Batch compatibility targeted tests: pass.
- Main planned suite excluding the pre-existing autophagy embedder-network
  timing failure: 164 passed, 1 deselected.
- `uv run --group dev ruff check .`: pass.

**Validation Commands:**
- `uv run --group dev pytest tests/test_memory_service.py tests/test_e2e_phase1.py tests/test_context_manager.py tests/test_document_mode.py tests/test_document_async_derive.py tests/test_batch_add_hierarchy.py tests/test_perf_fixes.py tests/test_conversation_immediate.py -q`
- `uv run --group dev pytest tests/test_ingestion_e2e.py tests/test_reward_integration.py -q`
- `uv run --group dev ruff check .`
- `uv run --group dev ruff format --check .`

## Risks

| Risk | Mitigation |
|------|------------|
| Tests monkeypatch `MemoryService` private helpers | Keep wrappers and route internal recursive calls through facade wrappers |
| Import cycle between write service and memory service | Use `TYPE_CHECKING` and local imports where needed |
| Add/update behavior drifts during move | Move code mechanically and run projection-heavy context tests |
| Batch add concurrency/cancellation changes | Move constants and implementation together, run perf/batch tests |
| Line count still exceeds 1000 | Keep wrappers concise; if still above target, report exact remaining cause instead of hiding logic in HTTP routes |
