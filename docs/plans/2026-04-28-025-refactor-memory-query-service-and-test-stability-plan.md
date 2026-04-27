---
title: Refactor MemoryService Query Logic And Stabilize Memory Tests
created: 2026-04-28
status: completed
type: refactor
origin: "$compound-engineering:lfg 先抽 search/list/index，再专门修测试稳定性"
---

# Refactor MemoryService Query Logic And Stabilize Memory Tests

## Problem Frame

`MemoryService` is now below 1000 lines after the write/mutation extraction,
but it still owns the remaining memory query domain logic:

- `search`
- `_build_typed_queries`
- query helper methods
- `list_memories`
- `memory_index`
- `list_memories_admin`

The service should continue moving toward a compatibility facade. The query and
listing logic should live behind a dedicated service while keeping
`MemoryOrchestrator` and existing call sites unchanged.

The previous validation also exposed two unstable tests that should be fixed
after the extraction:

- `tests/test_perf_fixes.py::TestAutophagySweeperLifecycle::test_init_starts_autophagy_sweeper_fire_and_forget`
  can be dominated by real local embedder model loading instead of the
  autophagy scheduling behavior it intends to assert.
- `tests/test_ingestion_e2e.py::TestIngestionE2E::test_document_mode_large_markdown`
  searches immediately after async document derive enqueue and can observe zero
  records or readonly database warnings before derive completion is stable.

## Requirements

- R1: Add a focused service for memory query/list/index behavior.
- R2: Keep `MemoryService.search`, `list_memories`, `memory_index`,
  `list_memories_admin`, and `_build_typed_queries` as compatibility wrappers
  where tests or call sites may patch or assert method existence.
- R3: Preserve search behavior, planner/runtime binding, scope filters, explain
  summaries, skill search merging, and timing metadata.
- R4: Preserve list/index/admin filtering behavior, project isolation, scope
  isolation, payload inclusion, ordering, and output shapes.
- R5: Keep `MemoryService` meaningfully smaller and focused on facade
  delegation after extraction.
- R6: Stabilize the autophagy init test so it verifies fire-and-forget
  scheduling without depending on network/model-loading latency.
- R7: Stabilize large document ingestion E2E so it waits for the async derive
  contract or asserts the correct pending behavior deterministically.
- R8: Keep API routes, orchestrator public methods, storage adapter behavior,
  Qdrant filters, CortexFS paths, and embedding strategy unchanged.

## Scope Boundaries

- Do not move write/mutation, document/batch, scoring, record projection, or
  session/trace lifecycle logic in this phase.
- Do not redesign the retrieval pipeline or planner/runtime contracts.
- Do not change HTTP response schemas.
- Do not remove compatibility wrappers from `MemoryService`.
- Do not hide test instability by loosening assertions without preserving the
  intended behavior under test.

## Current Code Evidence

- `src/opencortex/services/memory_service.py` is 939 lines.
- `MemoryService.search` starts at line 290 and contains the full
  probe-plan-bind-retrieve-aggregate flow.
- `MemoryService.list_memories`, `memory_index`, and `list_memories_admin`
  own filter construction and output shaping.
- `src/opencortex/orchestrator.py` delegates query/list/index methods to
  `MemoryService`, so public call sites can stay unchanged.
- Existing extracted services already use lazy properties from `MemoryService`
  with back-references to the facade.

## Key Technical Decisions

- Add `src/opencortex/services/memory_query_service.py` and make it the owner of
  search/list/index implementations.
- Keep `MemoryService` as the compatibility facade. Its query/list/index
  methods should delegate to `self._memory_query_service`.
- Move helper methods used only by query logic into `MemoryQueryService`, but
  keep `MemoryService._build_typed_queries` as a wrapper to preserve tests and
  monkeypatch compatibility.
- When moved query logic needs helper methods, call through the facade only
  where compatibility matters; otherwise keep local static helpers on
  `MemoryQueryService` for cohesion.
- Stabilize tests by eliminating unrelated real local embedder initialization
  from the autophagy test and by waiting for document derive completion before
  immediate search assertions.

## Implementation Units

### U1. Extract MemoryQueryService

**Goal:** Move query/list/index implementations out of `MemoryService`.

**Files:**
- Add: `src/opencortex/services/memory_query_service.py`
- Modify: `src/opencortex/services/memory_service.py`

**Approach:**
- Add `MemoryQueryService(memory_service)` with `_service` back-reference and
  `_orch` convenience property.
- Move implementations for `search`, `_build_typed_queries`,
  `_context_type_from_value`, `_detail_level_from_retrieval_depth`,
  `_summarize_retrieve_breakdown`, `_infer_context_type`, `list_memories`,
  `memory_index`, and `list_memories_admin`.
- Add a lazy `MemoryService._memory_query_service` property.
- Replace the original `MemoryService` query/list/index methods with wrappers.
- Update `MemoryService` module/class docstrings to describe the facade role.

**Test Scenarios:**
- Existing search tests still pass.
- `tests/test_memory_service.py` still sees expected helper methods.
- Query return shapes keep `FindResult`, explain summary, runtime result, and
  skill merge behavior.
- Listing/index methods preserve field names and filter behavior.

### U2. Stabilize autophagy fire-and-forget test

**Goal:** Ensure the test isolates autophagy scheduling from unrelated embedder
creation latency.

**Files:**
- Modify: `tests/test_perf_fixes.py`

**Approach:**
- Patch the bootstrapper path actually used by `MemoryOrchestrator.init()` so
  the test does not instantiate `LocalEmbedder`.
- Keep the assertion focused on init returning before the patched slow
  autophagy coroutines complete.
- Avoid changing production bootstrap behavior unless the test reveals a real
  production bug in fire-and-forget scheduling.

**Test Scenarios:**
- `tests/test_perf_fixes.py::TestAutophagySweeperLifecycle::test_init_starts_autophagy_sweeper_fire_and_forget`
  passes quickly and still fails if autophagy startup is awaited.

### U3. Stabilize large document ingestion E2E

**Goal:** Make the E2E test deterministic against the async document derive
contract.

**Files:**
- Modify: `tests/test_ingestion_e2e.py`
- Modify production only if current code fails to expose a stable completion
  hook or has a real derive lifecycle bug.

**Approach:**
- After document-mode `orch.add(...)`, wait for the derive queue to drain using
  the existing orchestrator/background-task completion hook before searching.
- If queue drain still leaves zero searchable records, investigate whether the
  test removes its temporary data root before background work finishes or
  whether derive writes are racing a storage shutdown.
- Preserve the assertion that large markdown eventually becomes searchable.

**Test Scenarios:**
- `tests/test_ingestion_e2e.py::TestIngestionE2E::test_document_mode_large_markdown`
  passes in isolation.
- Full `tests/test_ingestion_e2e.py tests/test_reward_integration.py -q`
  remains stable.

### U4. Validation, review, and pipeline gates

**Goal:** Prove the extraction is behavior-preserving and the stability fixes
address the observed failures.

**Files:**
- No planned production files beyond U1 unless U3 exposes a production derive
  lifecycle defect.

**Validation Commands:**
- `uv run --group dev pytest tests/test_memory_service.py -q`
- `uv run --group dev pytest tests/test_perf_fixes.py::TestAutophagySweeperLifecycle::test_init_starts_autophagy_sweeper_fire_and_forget -q`
- `uv run --group dev pytest tests/test_ingestion_e2e.py::TestIngestionE2E::test_document_mode_large_markdown -q`
- `uv run --group dev pytest tests/test_memory_service.py tests/test_e2e_phase1.py tests/test_context_manager.py tests/test_document_mode.py tests/test_document_async_derive.py tests/test_batch_add_hierarchy.py tests/test_perf_fixes.py tests/test_conversation_immediate.py -q`
- `uv run --group dev pytest tests/test_ingestion_e2e.py tests/test_reward_integration.py -q`
- `uv run --group dev ruff check .`
- `uv run --group dev ruff format --check .`

## Risks

| Risk | Mitigation |
|------|------------|
| Search behavior drifts during move | Move mechanically, preserve imports and helper semantics, run search/context/document tests |
| Tests monkeypatch `MemoryService._build_typed_queries` | Keep wrapper and route through query service |
| Query service creates import cycle | Use `TYPE_CHECKING` and local imports matching existing service pattern |
| Autophagy test patch misses real bootstrap path | Patch `SubsystemBootstrapper` methods if init delegates there |
| Document E2E becomes slower | Use queue-drain hook instead of arbitrary long sleeps |
| Fixing tests hides production issue | Investigate failures first; only adjust tests when production behavior matches async contract |

## Observed Results

- `MemoryService`: 405 lines.
- `MemoryQueryService`: 697 lines.
- `uv run --group dev pytest tests/test_memory_service.py -q`: pass.
- `uv run --group dev pytest tests/test_perf_fixes.py::TestAutophagySweeperLifecycle::test_init_starts_autophagy_sweeper_fire_and_forget -q`: pass.
- `uv run --group dev pytest tests/test_ingestion_e2e.py::TestIngestionE2E::test_document_mode_large_markdown -q`: pass.
- `uv run --group dev pytest tests/test_memory_service.py tests/test_e2e_phase1.py tests/test_context_manager.py tests/test_document_mode.py tests/test_document_async_derive.py tests/test_batch_add_hierarchy.py tests/test_perf_fixes.py tests/test_conversation_immediate.py -q`: 165 passed.
- `uv run --group dev pytest tests/test_ingestion_e2e.py tests/test_reward_integration.py -q`: 6 passed, 8 skipped.
- `uv run --group dev ruff check .`: pass.
- `uv run --group dev ruff format --check .`: pass.
