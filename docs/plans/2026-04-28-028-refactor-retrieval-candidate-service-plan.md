---
title: Refactor Retrieval Candidate Projection Service
created: 2026-04-28
status: completed
type: refactor
origin: "$compound-engineering:lfg 抽离 RetrievalService 的 candidate projection helper 到 RetrievalCandidateService，保留 RetrievalService 和 MemoryOrchestrator 兼容 wrapper"
---

# Refactor Retrieval Candidate Projection Service

## Problem Frame

`RetrievalService` is now the largest service after the orchestrator cleanup:
999 lines on `master`. The boundary assessment in
`docs/architecture/retrieval-service-boundary-assessment.md` identifies the
lowest-risk next split: candidate projection helpers that convert raw storage
records into scored and access-controlled retrieval candidates.

This phase should extract those helpers into `RetrievalCandidateService` while
keeping `RetrievalService` and `MemoryOrchestrator` compatibility wrappers.

## Requirements

- R1: Add `src/opencortex/services/retrieval_candidate_service.py`.
- R2: Move candidate projection helper implementations out of
  `RetrievalService`:
  - `_score_object_record`
  - `_record_passes_acl`
  - `_matched_record_anchors`
  - `_records_to_matched_contexts`
- R3: Keep `RetrievalService` methods with the same names and signatures as
  compatibility wrappers.
- R4: Keep `MemoryOrchestrator` wrappers unchanged; callers and tests must still
  call or patch the same orchestrator method names.
- R5: Keep `_execute_object_query` orchestration in `RetrievalService`.
- R6: Preserve scoring, ACL, anchor matching, detail-level content loading, and
  `MatchedContext` output shape exactly.
- R7: Add focused tests for `RetrievalCandidateService` where existing object
  retrieval tests do not directly lock the service boundary.
- R8: Run object rerank/cone, perf, recall planner, memory/e2e, and style gates.

## Scope Boundaries

- Do not split probe/planner/runtime binding in this phase.
- Do not split `_execute_object_query`.
- Do not remove compatibility wrappers from `RetrievalService` or
  `MemoryOrchestrator`.
- Do not change retrieval ranking weights, ACL rules, detail-level behavior, or
  explain metadata.
- Do not alter HTTP routes or request/response schemas.
- Do not touch frontend code.

## Current Code Evidence

- `src/opencortex/services/retrieval_service.py` is 999 lines.
- The candidate helpers currently live in `RetrievalService` near the middle of
  the file and are called by `_execute_object_query`.
- `tests/test_perf_fixes.py` patches orchestrator wrappers such as
  `_execute_object_query` and `_aggregate_results`.
- `tests/test_object_rerank.py` and `tests/test_object_cone.py` call
  `_execute_object_query` through `MemoryOrchestrator`.
- `tests/test_context_manager.py` has multiple direct object-query tests.

## Key Technical Decisions

- Add a lazy `_retrieval_candidate_service` property on `RetrievalService`.
- Move implementations into `RetrievalCandidateService`, which holds a
  back-reference to the parent `RetrievalService`.
- Keep `RetrievalService` wrappers and preserve `_execute_object_query`'s
  existing compatibility path through `MemoryOrchestrator` wrapper calls.
- Let `RetrievalCandidateService` reach orchestrator-owned filesystem through
  the parent service (`self._service._fs`) for L2 content fallback.
- Use `TYPE_CHECKING` imports to avoid runtime cycles.

## Implementation Units

### U1. Add RetrievalCandidateService and lazy property

**Goal:** Establish the new owner for candidate scoring/projection helpers.

**Files:**
- Add: `src/opencortex/services/retrieval_candidate_service.py`
- Modify: `src/opencortex/services/retrieval_service.py`

**Approach:**
- Add `RetrievalCandidateService(retrieval_service)` with `_service`
  back-reference.
- Add `RetrievalService._retrieval_candidate_service` lazy property.
- Move imports needed only by candidate helpers from `retrieval_service.py` if
  they become unused there.

**Test Scenarios:**
- Importing `RetrievalService` still works.
- `RetrievalService.__new__` style fixtures can access wrapper methods without
  eager service construction.

### U2. Move scoring, ACL, anchor, and context projection helpers

**Goal:** Move helper bodies while preserving compatibility wrappers.

**Files:**
- Modify: `src/opencortex/services/retrieval_candidate_service.py`
- Modify: `src/opencortex/services/retrieval_service.py`
- Add: `tests/test_retrieval_candidate_service.py`

**Approach:**
- Move `_score_object_record`, `_record_passes_acl`, `_matched_record_anchors`,
  and `_records_to_matched_contexts` implementations into
  `RetrievalCandidateService`.
- Replace `RetrievalService` methods with thin delegates.
- Keep static-like call compatibility for `_record_passes_acl` and
  `_matched_record_anchors` by leaving wrappers callable on the service.

**Test Scenarios:**
- `_record_passes_acl` preserves private user and project visibility rules.
- `_matched_record_anchors` returns normalized intersection values capped to 8.
- `_records_to_matched_contexts` preserves `MatchedContext` fields and L2 content
  fallback behavior.
- Existing object rerank/cone tests still pass.

### U3. Validation, review, browser gate, and PR

**Goal:** Complete the LFG pipeline with focused verification.

**Validation Commands:**
- `uv run --group dev pytest tests/test_retrieval_candidate_service.py -q`
- `uv run --group dev pytest tests/test_object_rerank.py tests/test_object_cone.py -q`
- `uv run --group dev pytest tests/test_perf_fixes.py -q`
- `uv run --group dev pytest tests/test_recall_planner.py -q`
- `uv run --group dev pytest tests/test_memory_service.py tests/test_e2e_phase1.py -q`
- `uv run --group dev ruff check .`
- `uv run --group dev ruff format --check .`

## Risks

| Risk | Mitigation |
|------|------------|
| Retrieval scoring drifts | Move code mechanically and run object rerank/cone tests |
| Tests patch wrappers | Keep both RetrievalService and MemoryOrchestrator wrapper names |
| L2 content fallback loses filesystem access | Service reaches parent retrieval service `_fs` property |
| Import cycle between services | Use `TYPE_CHECKING` and local/lazy service imports |
| Split expands scope into `_execute_object_query` | Keep main method in place and only delegate helper calls |

## Observed Results

- Added `src/opencortex/services/retrieval_candidate_service.py`.
- `RetrievalService` compatibility wrappers remain in place.
- `MemoryOrchestrator` compatibility wrappers remain unchanged.
- `_execute_object_query` remains owned by `RetrievalService`.
- `RetrievalService` reduced from 999 lines to 886 lines.
- `RetrievalCandidateService` is 225 lines.

## Validation Results

- `uv run --group dev pytest tests/test_retrieval_candidate_service.py -q`:
  passed, 3 tests.
- `uv run --group dev pytest tests/test_object_rerank.py tests/test_object_cone.py -q`:
  passed, 2 tests.
- `uv run --group dev pytest tests/test_perf_fixes.py -q`: passed, 13 tests
  with one pre-existing fastembed warning.
- `uv run --group dev pytest tests/test_recall_planner.py -q`: passed,
  25 tests.
- `uv run --group dev pytest tests/test_memory_service.py tests/test_e2e_phase1.py -q`:
  passed, 54 tests.
- `uv run --group dev ruff check .`: passed.
- `uv run --group dev ruff format --check .`: passed.
