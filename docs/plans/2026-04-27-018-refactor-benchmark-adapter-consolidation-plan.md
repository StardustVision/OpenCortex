---
title: "refactor: Consolidate Benchmark Adapter shared logic"
type: refactor
status: active
date: 2026-04-27
origin: docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md (Phase 8)
---

# refactor: Consolidate Benchmark Adapter shared logic

## Overview

Phase 8 of the God Object decomposition. Six benchmark adapters (conversation, locomo, beam, document, hotpotqa, memory) and one knowledge adapter share ~300 lines of duplicated retrieve dispatch, JSON loading, latency timing, and concurrency boilerplate. This plan extracts the common patterns into the existing `EvalAdapter` base class without changing external behavior or dataset contracts.

---

## Problem Frame

The `benchmarks/adapters/` directory contains 7 concrete adapters inheriting from a 124-line `EvalAdapter` ABC. The ABC defines four abstract methods but provides almost no shared implementation. Each adapter independently re-implements:

1. **retrieve() dispatch** — 6 adapters copy the same recall/search branching, latency timing, and `_set_last_retrieval_meta()` call (~30 lines each, ~180 total)
2. **load_dataset() JSON boilerplate** — open/parse/store pattern repeated 6 times
3. **Concurrency scaffold** — conversation + locomo share ~60 lines of semaphore + gather + error accumulation
4. **Instance attributes** — `_retrieve_method`, `_ingest_method` set on 7 instances but never declared in the base

The duplication means bug fixes (e.g., retrieval contract changes) must be applied to 6 places independently.

---

## Requirements Trace

- R1. All 200+ existing tests continue passing without modification
- R2. Each adapter's unique behavior (dataset validation, session ID construction, QA extraction) is preserved
- R3. The `EvalAdapter` base class gains concrete methods for retrieve dispatch, load_dataset, and concurrent ingest
- R4. No changes to external API surface (adapter constructors, IngestResult/QAItem dataclasses, runner integration)
- R5. Adapter file sizes reduce by removing duplicated boilerplate

---

## Scope Boundaries

- This is a MOVE not REWRITE — method bodies land verbatim with minimal adaptation
- No new adapters or adapter interfaces
- No changes to the unified_eval runner
- No changes to conversation_mapping.py (already well-factored)
- No changes to scoring helpers or eval metrics
- No changes to the knowledge adapter (its retrieve path is fundamentally different — calls Archivist directly)

### Deferred to Follow-Up Work

- Shared test helper for `_OCStub` (test dedup) — orthogonal to adapter consolidation
- Adding missing test files for HotPotQAAdapter and MemoryAdapter — separate concern

---

## Context & Research

### Relevant Code and Patterns

- `benchmarks/adapters/base.py` — 124-line ABC with `EvalAdapter`, `QAItem`, `IngestResult`
- `benchmarks/adapters/conversation_mapping.py` — 256 lines of shared pure-function helpers (already extracted)
- `benchmarks/adapters/conversation.py` — 516 lines, session-scoped retrieve, concurrency ingest
- `benchmarks/adapters/locomo.py` — 625 lines, session-scoped retrieve, concurrency ingest
- `benchmarks/adapters/beam.py` — 308 lines, session-scoped retrieve, serial ingest
- `benchmarks/adapters/document.py` — 300 lines, search-only retrieve
- `benchmarks/adapters/hotpotqa.py` — 219 lines, non-scoped retrieve
- `benchmarks/adapters/memory.py` — 171 lines, non-scoped retrieve
- Prior phases 1-7 used back-reference + delegate pattern; this phase uses inheritance (already in place)

### Institutional Learnings

- Benchmark adapters must consume the `memory_pipeline` envelope (probe/planner/runtime) rather than legacy fields — ensure `_set_last_retrieval_meta` contract is preserved exactly
- `conversation_mapping.py` was already extracted as shared helpers during a prior refactor — follow the same extraction pattern

---

## Key Technical Decisions

- **Template method for retrieve()**: Base class provides a concrete `retrieve()` with hook methods for session ID, metadata filter, and result key extraction. Subclasses override hooks instead of the full method. Rationale: the recall/search branch + latency timing + meta capture is identical across 6 adapters; only the filter construction and result key differ.
- **Keep knowledge adapter unchanged**: Its retrieve path is fundamentally different (Archivist / server endpoints). Forcing it into the template method would add abstraction complexity for no reuse benefit.
- **`_run_concurrent_ingest` helper on base**: Only conversation and locomo use it, but placing it on the base avoids a standalone utility module for 2 consumers and keeps it available for future adapters.
- **load_dataset remains overridable**: Base class provides a concrete implementation (open/parse/store) that calls an optional `_validate_dataset()` hook. Adapters with unique loading (knowledge) can still override.

---

## Open Questions

### Resolved During Planning

- Q: Should document adapter's search-only path use the same template method? A: Yes — it's a degenerate case (no recall branch, no session filter) that the template handles cleanly with hook defaults returning None/empty.

### Deferred to Implementation

- Exact hook method names and signatures — deferred to execution time as the implementer discovers the minimal set needed
- Whether `_OCStub` test dedup should happen in the same PR — likely separate, but implementer decides

---

## Implementation Units

- U1. **Strengthen EvalAdapter base class with shared retrieve dispatch, load_dataset, and concurrent ingest**

**Goal:** Add concrete methods to `EvalAdapter` that encapsulate the duplicated patterns from 6 adapters.

**Requirements:** R3, R5

**Dependencies:** None

**Files:**
- Modify: `benchmarks/adapters/base.py`
- Test: `tests/test_benchmark_runner.py`

**Approach:**

1. Add `_retrieve_method: str = "search"` and `_ingest_method: str = ""` to base `__init__`
2. Add concrete `retrieve()` as a template method:
   - Timing with `time.perf_counter()`
   - Branch on `self._retrieve_method`: "recall" → `oc.context_recall()`, else → `oc.search_payload()`
   - Call `_set_last_retrieval_meta()` with endpoint and session_scope
   - Extract results via hook `_get_retrieval_results(payload)` → default returns `payload.get("results", [])`
   - Hook `_get_retrieval_session_id(qa_item)` → default returns `None`
   - Hook `_get_retrieval_metadata_filter(session_id)` → default returns `None`
3. Add concrete `load_dataset()`:
   - Open/parse JSON, store in `self._dataset`
   - Call optional `self._validate_dataset(raw)` hook (default: no-op)
4. Add `_run_concurrent_ingest()` helper:
   - Takes items, concurrency, and a per-item async callable
   - Semaphore + gather + error accumulation pattern
   - Returns `(successes, errors)` tuple

**Patterns to follow:**
- `benchmarks/adapters/conversation.py:469-516` — canonical retrieve implementation
- `benchmarks/adapters/locomo.py:569-625` — session-scoped retrieve with metadata_filter
- Prior phases' back-reference pattern (base class provides, subclass delegates)

**Test scenarios:**
- Happy path: base retrieve dispatches to recall when `_retrieve_method="recall"`
- Happy path: base retrieve dispatches to search when `_retrieve_method="search"`
- Edge case: hook returning None for session_id produces no metadata_filter
- Edge case: hook returning None for metadata_filter produces unfiltered search
- Integration: load_dataset reads JSON and stores in `_dataset`
- Integration: `_validate_dataset` hook is called when provided
- Error path: invalid JSON path raises FileNotFoundError
- Error path: `_run_concurrent_ingest` accumulates errors from failed items

**Verification:**
- `tests/test_benchmark_runner.py` passes
- New tests exercise the base class retrieve dispatch and load_dataset directly

---

- U2. **Migrate conversation, locomo, beam, document, hotpotqa, memory adapters to use base class methods**

**Goal:** Replace duplicated retrieve/load_dataset/concurrency code in 6 adapters with calls to base class methods and hooks.

**Requirements:** R1, R2, R5

**Dependencies:** U1

**Files:**
- Modify: `benchmarks/adapters/conversation.py`
- Modify: `benchmarks/adapters/locomo.py`
- Modify: `benchmarks/adapters/beam.py`
- Modify: `benchmarks/adapters/document.py`
- Modify: `benchmarks/adapters/hotpotqa.py`
- Modify: `benchmarks/adapters/memory.py`
- Test: `tests/test_locomo_bench.py`
- Test: `tests/test_beam_bench.py`

**Approach:**

For each of the 6 adapters:
1. Remove the duplicated `retrieve()` method body; keep only if the adapter needs to override the base template (most won't)
2. Override hook methods for adapter-specific behavior:
   - Session-scoped adapters (conversation, locomo, beam): override `_get_retrieval_session_id()` and `_get_retrieval_metadata_filter()`
   - Document adapter: override `_get_retrieval_results()` to extract from search-only path, set `context_type` hook
   - Non-scoped adapters (hotpotqa, memory): no hooks needed — base defaults work
3. Remove duplicated `load_dataset()` body; add `_validate_dataset()` hook if the adapter has validation logic
4. Replace concurrency scaffold in conversation/locomo with `_run_concurrent_ingest()`
5. Remove `_retrieve_method` / `_ingest_method` init lines — now in base `__init__`

**Execution note:** Migrate adapters one at a time, running tests after each to catch regressions early.

**Patterns to follow:**
- `benchmarks/adapters/conversation_mapping.py` — prior extraction pattern (pure functions, minimal coupling)

**Test scenarios:**
- Happy path: each adapter's existing tests pass unchanged
- Integration: conversation adapter retrieve includes session_id filter
- Integration: locomo adapter retrieve includes session_id filter with URI dedup
- Integration: document adapter retrieve uses search-only path
- Integration: beam adapter retrieve uses session-scoped search
- Edge case: adapters with `_retrieve_method` override (runner sets it) still work

**Verification:**
- All tests in `tests/test_locomo_bench.py`, `tests/test_beam_bench.py`, `tests/test_benchmark_runner.py` pass
- Total line count across adapter files decreases by ~200+ lines
- No adapter's public method signatures change

---

- U3. **Add base class unit tests and verify runner integration**

**Goal:** Ensure the new base class methods have dedicated test coverage and the unified_eval runner still routes correctly.

**Requirements:** R1, R4

**Dependencies:** U2

**Files:**
- Modify: `tests/test_benchmark_runner.py`
- Test: `tests/test_eval_knowledge.py` (unchanged, but verify it still passes)

**Approach:**
1. Add tests for `EvalAdapter.retrieve()` dispatch logic (recall vs search branching)
2. Add tests for `EvalAdapter.load_dataset()` and `_validate_dataset` hook
3. Add tests for `_run_concurrent_ingest()` error accumulation
4. Verify that `knowledge.py` (which keeps its own retrieve) still passes its tests
5. Run full test suite to confirm no regressions

**Test scenarios:**
- Happy path: base retrieve returns results and latency_ms
- Happy path: base load_dataset parses JSON file
- Edge case: `_validate_dataset` returning None is accepted (no-op)
- Error path: concurrent ingest with one failure returns partial results + errors
- Error path: concurrent ingest with CancelledError is handled gracefully
- Integration: runner can instantiate and call all 7 adapters without errors

**Verification:**
- `uv run python3 -m unittest discover -s tests -v` passes with 0 unexpected failures
- Knowledge adapter tests pass unchanged (confirms non-interference)

---

## System-Wide Impact

- **Interaction graph:** unified_eval runner (`benchmarks/unified_eval.py`) instantiates adapters and calls `retrieve()`, `ingest()`, `build_qa_items()`. No changes to runner needed — adapter contracts are preserved.
- **Error propagation:** Error accumulation pattern unchanged — `IngestResult.errors` still a `List[str]`
- **State lifecycle risks:** None — `_dataset` and `_last_retrieval_meta` lifecycle unchanged
- **API surface parity:** No changes to adapter constructors, QAItem, IngestResult, or runner interface
- **Unchanged invariants:** Knowledge adapter completely untouched; runner's `hasattr` checks for `_retrieve_method` still work since the attribute moves to base `__init__`

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Template method hooks miss adapter-specific edge cases | Migrate one adapter at a time, run tests after each |
| Runner relies on `hasattr(adapter, "_retrieve_method")` | Attribute now on base class — `hasattr` still returns True |
| Knowledge adapter's unique retrieve path accidentally constrained | Knowledge adapter doesn't inherit retrieve from base — its own override stays |
| Document adapter's search-only path doesn't fit template | Degenerate case: no recall branch (hooks return None), template falls through to search |

---

## Sources & References

- **Origin document:** [docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md](docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md) (Phase 8)
- Related code: `benchmarks/adapters/base.py` (EvalAdapter ABC)
- Related code: `benchmarks/adapters/conversation_mapping.py` (prior extraction)
