---
title: Refactor Orchestrator Remaining Internal Helpers And Retrieval Boundaries
created: 2026-04-28
status: completed
type: refactor
origin: "$compound-engineering:lfg 抽离 MemoryOrchestrator 剩余 internal helper：_generate_abstract_overview 移到 document write/derivation，get_user_memory_stats 移到 admin/stats service，并做 RetrievalService 超 1000 前的边界评估"
---

# Refactor Orchestrator Remaining Internal Helpers And Retrieval Boundaries

## Problem Frame

After the prior orchestrator extractions, `MemoryOrchestrator` is mostly a
compatibility facade. Two real internal helper implementations still live in
the class:

- `_generate_abstract_overview`, a document write/derivation helper that imports
  prompt, JSON parsing, chunking, and truncation utilities.
- `get_user_memory_stats`, an admin/insights statistics query over stored memory
  records.

At the same time, `RetrievalService` is now 999 lines and is close to becoming
the next large service. This phase should remove the remaining orchestrator
domain helpers and produce a grounded boundary assessment before splitting
retrieval.

## Requirements

- R1: Move `_generate_abstract_overview` implementation out of
  `MemoryOrchestrator` into the document write/derivation ownership boundary.
- R2: Preserve the orchestrator `_generate_abstract_overview(...)`
  compatibility method because existing tests and document-write code patch or
  call it.
- R3: Remove document-summary prompt/JSON/chunk imports from
  `MemoryOrchestrator` when they are no longer needed there.
- R4: Move `get_user_memory_stats` implementation out of `MemoryOrchestrator`
  into an admin/stats service boundary.
- R5: Preserve `MemoryOrchestrator.get_user_memory_stats(...)` as a public
  compatibility wrapper.
- R6: Add focused tests for the moved stats behavior and abstract/overview
  wrapper behavior where existing coverage is not enough.
- R7: Add a durable `RetrievalService` boundary assessment that identifies
  whether and how to split it before it grows beyond 1000 lines.
- R8: Do not split `RetrievalService` in this phase unless the assessment
  reveals a tiny no-risk move required by this work.

## Scope Boundaries

- Do not remove existing orchestrator compatibility wrappers.
- Do not change document ingestion response shape, summarization prompts, or
  fallback behavior.
- Do not change memory statistics response keys or filtering semantics.
- Do not redesign retrieval planning, probing, reranking, ACL filtering, or
  aggregation in this phase.
- Do not move retrieval code just to satisfy line-count goals; this phase ends
  with an assessment unless a safe adjacent cleanup is unavoidable.
- Do not touch frontend code.

## Current Code Evidence

- `src/opencortex/orchestrator.py` is 1617 lines.
- `src/opencortex/orchestrator.py` still imports
  `build_doc_summarization_prompt`, `parse_json_from_response`,
  `chunked_llm_derive`, and `smart_truncate` solely for
  `_generate_abstract_overview`.
- `src/opencortex/services/memory_document_write_service.py` calls
  `orch._generate_abstract_overview(...)` during document writes.
- `tests/test_perf_fixes.py` monkeypatches `oc._generate_abstract_overview`, so
  the orchestrator wrapper must stay.
- `src/opencortex/orchestrator.py` implements `get_user_memory_stats(...)`
  directly and no other production file currently calls it.
- `src/opencortex/services/system_status_service.py` already owns health,
  stats, derive status, and re-embedding, but not user memory admin stats.
- `src/opencortex/services/retrieval_service.py` is 999 lines and currently owns
  probe/planner/bind, query embedding, ACL conversion, rerank/aggregate, and
  session-aware search behavior.

## Key Technical Decisions

- Add the document summarization helper to
  `MemoryDocumentWriteService`, because its only current production caller is
  the document-write service and this keeps document write behavior cohesive.
- Keep `MemoryOrchestrator._generate_abstract_overview(...)` as a thin wrapper
  delegating to `self._memory_service._memory_document_write_service`.
- Add a small `MemoryAdminStatsService` for `get_user_memory_stats(...)` rather
  than expanding `SystemStatusService`; this keeps health/system status separate
  from admin record analytics.
- Add a lazy `_memory_admin_stats_service` property on the orchestrator using the
  same back-reference pattern as the other extracted services.
- Write the retrieval boundary assessment as
  `docs/architecture/retrieval-service-boundary-assessment.md`, so the next
  split has current line-count evidence and candidate seams before execution.

## Implementation Units

### U1. Move document abstract/overview generation

**Goal:** Remove the document summarization implementation and imports from
`MemoryOrchestrator`.

**Files:**
- Modify: `src/opencortex/orchestrator.py`
- Modify: `src/opencortex/services/memory_document_write_service.py`

**Approach:**
- Move `_generate_abstract_overview` logic into `MemoryDocumentWriteService`.
- Keep the orchestrator method name and signature as a delegate wrapper.
- Keep document write call sites routed through the orchestrator wrapper where
  tests monkeypatch it.
- Move imports for prompt building, JSON parsing, chunked derivation, and smart
  truncation into `memory_document_write_service.py`.

**Test Scenarios:**
- Existing document write tests still pass.
- Existing tests that monkeypatch `oc._generate_abstract_overview` still pass.
- No document summarization utility imports remain in `orchestrator.py`.

### U2. Move admin user memory stats

**Goal:** Move `get_user_memory_stats` implementation out of the orchestrator.

**Files:**
- Add: `src/opencortex/services/memory_admin_stats_service.py`
- Modify: `src/opencortex/orchestrator.py`
- Add or modify: `tests/test_memory_admin_stats_service.py`

**Approach:**
- Add `MemoryAdminStatsService(orchestrator)` with
  `get_user_memory_stats(tenant_id, user_id)`.
- Preserve the exact filter DSL:
  - exclude `context_type == staging`;
  - require `source_tenant_id == tenant_id`;
  - require `source_user_id == user_id`.
- Preserve limit `10000`, session count grouping, and positive/negative
  feedback totals.
- Add orchestrator lazy property and wrapper.

**Test Scenarios:**
- Stats service builds the same filter and returns the same response keys.
- Empty result returns zero totals and empty `created_in_session`.
- Orchestrator lazy property works for `__new__` bypass fixtures.

### U3. RetrievalService boundary assessment

**Goal:** Decide the next safe retrieval split from current code, not guesswork.

**Files:**
- Add: `docs/architecture/retrieval-service-boundary-assessment.md`

**Approach:**
- Inventory current `RetrievalService` method groups and line count.
- Identify candidate seams:
  - probe/planner/runtime binding;
  - query embedding and typed query execution;
  - record-to-context projection and ACL helpers;
  - aggregate/rerank scoring.
- Recommend the next split only if it improves ownership without breaking
  monkeypatch seams.
- List tests that must guard a future split.

**Test Scenarios:**
- Document references repo-relative files and current method names.
- No production behavior changes come from the assessment.

### U4. Validation, review, browser gate, and PR

**Goal:** Complete the LFG pipeline.

**Validation Commands:**
- `uv run --group dev pytest tests/test_document_mode.py tests/test_perf_fixes.py tests/test_memory_admin_stats_service.py -q`
- `uv run --group dev pytest tests/test_memory_service.py tests/test_e2e_phase1.py tests/test_http_server.py -q`
- `uv run --group dev pytest tests/test_rerank_client_lifecycle.py tests/test_system_status_service.py tests/test_conversation_immediate.py -q`
- `uv run --group dev ruff check .`
- `uv run --group dev ruff format --check .`

## Risks

| Risk | Mitigation |
|------|------------|
| Tests patch `_generate_abstract_overview` on the orchestrator | Keep the wrapper and keep document write calling through it |
| Document fallback behavior drifts during move | Move logic mechanically and run document/perf tests |
| Admin stats filter changes silently | Add focused tests for exact filter and output totals |
| `SystemStatusService` becomes a mixed admin analytics service | Use a dedicated admin stats service |
| Retrieval split becomes speculative | Produce assessment only; defer code split to a follow-up |

## Observed Results

- `MemoryOrchestrator`: 1567 lines, down from 1617.
- `MemoryAdminStatsService`: 55 lines.
- `RetrievalService`: unchanged at 999 lines.
- Added `docs/architecture/retrieval-service-boundary-assessment.md` with the
  recommended next split: candidate projection helpers first.
- `uv run --group dev pytest tests/test_document_mode.py tests/test_perf_fixes.py tests/test_memory_admin_stats_service.py tests/test_memory_document_write_service.py -q`: 19 passed.
- `uv run --group dev pytest tests/test_memory_service.py tests/test_e2e_phase1.py tests/test_http_server.py -q`: 74 passed.
- `uv run --group dev pytest tests/test_rerank_client_lifecycle.py tests/test_system_status_service.py tests/test_conversation_immediate.py -q`: 24 passed.
- `uv run --group dev ruff check .`: pass.
- `uv run --group dev ruff format --check .`: pass.
