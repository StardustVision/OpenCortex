---
title: Fix Repository Review Findings
created: 2026-04-27
status: active
type: fix
origin: compound-engineering repository review
---

# Fix Repository Review Findings

## Problem Frame

The repository review found several correctness, performance, and maintainability issues in core storage and retrieval paths. The most urgent issue is that Alpha knowledge and trace searches pass a filter as the third positional argument to `StorageInterface.search`, where the interface expects a sparse vector. That silently bypasses tenant/status/type filtering. Secondary findings cover Qdrant ordered filtering doing full result materialization, filter DSL unknown operators becoming match-all filters, unbounded scan/import fan-out, and current ruff gate failures.

## Requirements

- R1: Knowledge and trace vector searches must pass Qdrant filters through the `filter` keyword, not as positional sparse vectors.
- R2: Tests must prove knowledge and trace searches bind filters by keyword and preserve type/status/scope constraints.
- R3: Ordered Qdrant filtering must avoid unbounded full materialization for paginated listing paths, or enforce a bounded scan ceiling with explicit degraded behavior.
- R4: Unknown filter DSL operators must fail fast instead of producing match-all filters.
- R5: MCP repository scanning and server-side batch import must have bounded fan-out so large repositories cannot create unbounded memory/task pressure.
- R6: The repo's configured ruff check and format check must pass for the scoped Python surface.

## Scope Boundaries

- This plan fixes the review findings directly; it does not redesign the retrieval planner, Qdrant schema, or Cortex Alpha lifecycle.
- Existing public API shapes remain intact.
- Benchmark dataset storage policy is not changed in this pass.
- Browser/UI work is out of scope; the browser-test pipeline may legitimately be a no-op for this backend-focused change.

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/storage/storage_interface.py` defines the canonical `search` parameter order and keyword names.
- `src/opencortex/storage/qdrant/adapter.py` is the concrete search/filter adapter and already centralizes Qdrant filter translation.
- `src/opencortex/alpha/knowledge_store.py` and `src/opencortex/alpha/trace_store.py` follow similar vector-search patterns and should be fixed consistently.
- `src/opencortex/storage/qdrant/filter_translator.py` is the single place to tighten DSL behavior.
- `plugins/opencortex-memory/src/scan.ts` owns local repository scan fan-out before sending data to OpenCortex.
- `src/opencortex/services/memory_service.py` owns server-side batch import fan-out.

### Institutional Learnings

- Prior OpenCortex benchmark work emphasized live verification over assumed behavior and keeping benchmark/server measurements isolated.
- Prior review work in this checkout uses source-bound findings and focused regression tests rather than broad speculative refactors.

### External References

- Not used. The issues are internal API contract, local adapter behavior, and bounded resource usage; local code patterns are sufficient.

## Key Technical Decisions

- Use keyword arguments for every `StorageInterface.search` call outside trivial pass-through wrappers when ambiguity is possible. This removes the positional contract hazard without changing the interface.
- Treat unknown filter DSL operators as programmer errors. Callers that need compatibility should translate old shapes before calling `translate_filter`.
- For ordered Qdrant filter pagination, prefer bounded behavior over hidden full scans. If native Qdrant ordering support is not available or not portable across embedded/server modes, cap scanned points and document the cap in code/tests.
- Add both client-side scan limits and server-side batch task chunking. Defense should exist before JSON emission and before in-process task creation.

## Open Questions

### Resolved During Planning

- Should this be a broad refactor of `MemoryOrchestrator` service boundaries? No. The current request is to fix review findings; service-boundary cleanup remains incremental.
- Should large benchmark datasets be removed from git now? No. That is repository policy and benchmark workflow scope, not necessary for the runtime fixes.

### Deferred to Implementation

- Exact Qdrant native ordering support may vary by client/server version. Implementation can choose bounded fallback if native order is not available in the installed client.

## Implementation Units

- U1. **Fix Alpha search argument binding**

**Goal:** Ensure knowledge and trace searches enforce tenant/status/type/scope filters.

**Requirements:** R1, R2

**Dependencies:** None

**Files:**
- Modify: `src/opencortex/alpha/knowledge_store.py`
- Modify: `src/opencortex/alpha/trace_store.py`
- Test: `tests/test_knowledge_store.py`
- Test: `tests/test_trace_store.py`

**Approach:**
- Replace ambiguous positional search calls with explicit `collection=`, `query_vector=`, `filter=`, and `limit=` keywords.
- Update tests to assert keyword binding so regressions cannot reintroduce positional filter loss.

**Execution note:** Test-first for the failing contract is preferred: adjust or add assertions before changing implementation.

**Patterns to follow:**
- `src/opencortex/skill_engine/adapters/storage_adapter.py` already uses keyword arguments for `query_vector` and `filter`.

**Test scenarios:**
- Happy path: knowledge search with `types=["belief", "sop"]` calls storage with `filter` containing tenant, searchable statuses, type constraint, and scope OR group.
- Happy path: trace search calls storage with `filter` containing tenant constraint and `query_vector` containing the embedder vector.
- Regression: no test should treat the third positional argument as the filter.

**Verification:**
- Knowledge and trace search tests fail before the implementation change and pass after.

---

- U2. **Bound ordered Qdrant filtering**

**Goal:** Prevent list/index endpoints from scanning and sorting entire matching collections for small pages.

**Requirements:** R3

**Dependencies:** None

**Files:**
- Modify: `src/opencortex/storage/qdrant/adapter.py`
- Test: `tests/test_qdrant_adapter.py`

**Approach:**
- Replace hidden unbounded scroll in `filter(order_by=...)` with either native Qdrant ordering or a bounded fallback.
- Keep existing behavior for records missing the order field: they should not disappear solely because a field is absent.
- Add a regression test using a low scan ceiling or mocked scroll pagination to prove the adapter does not drain all pages for a small ordered request.

**Patterns to follow:**
- Existing `test_filter_method_with_order_by_missing_field` documents missing-field behavior.

**Test scenarios:**
- Happy path: ordered filter returns requested page size sorted by the requested payload field.
- Edge case: records missing `updated_at` remain eligible and do not crash sorting.
- Performance regression: requesting `limit=10` over many pages does not scroll through every matching page when enough bounded candidates are available.

**Verification:**
- Qdrant adapter tests cover both correctness and scan bound.

---

- U3. **Make filter translator fail fast**

**Goal:** Stop unknown filter DSL shapes from silently becoming match-all filters.

**Requirements:** R4

**Dependencies:** None

**Files:**
- Modify: `src/opencortex/storage/qdrant/filter_translator.py`
- Test: `tests/test_alpha_knowledge_store.py`
- Test: `tests/test_qdrant_adapter.py`

**Approach:**
- Raise `ValueError` for unknown `op` values.
- Preserve empty-filter behavior only for explicitly empty DSL inputs.
- Update tests that documented old match-all behavior to expect a failure.

**Patterns to follow:**
- Existing filter translator tests in `tests/test_qdrant_adapter.py`.

**Test scenarios:**
- Error path: old `{"op": "and", "conditions": [...]}` shape fails instead of returning an empty filter.
- Happy path: known `and`, `or`, `must`, `range`, `prefix`, `contains`, and `is_null` filters still translate.

**Verification:**
- Filter translator tests prove unknown operators cannot silently widen result sets.

---

- U4. **Add scan and batch-import backpressure**

**Goal:** Avoid unbounded memory and task fan-out when scanning or importing large repositories.

**Requirements:** R5

**Dependencies:** None

**Files:**
- Modify: `plugins/opencortex-memory/src/scan.ts`
- Modify: `plugins/opencortex-memory/tests/server.test.ts`
- Modify: `src/opencortex/services/memory_service.py`
- Test: `tests/test_ingestion_e2e.py` or `tests/test_perf_fixes.py`

**Approach:**
- Add scanner limits for max files and max total bytes with environment-variable or constant defaults.
- Include skipped/limit metadata in `scan_meta` so callers can see truncation.
- Change batch import to process items in chunks instead of creating one task per item for the entire request.

**Patterns to follow:**
- Existing batch concurrency semaphore in `MemoryService.batch_add`.
- Existing scanner skip-dir and file-size checks in `scan.ts`.

**Test scenarios:**
- Edge case: scanner stops after max files and reports skipped count in scan metadata.
- Edge case: scanner stops after max total bytes and does not read further file contents.
- Performance regression: batch import with more than one chunk only has bounded in-flight `_process_one` tasks at a time.

**Verification:**
- Python and MCP package tests cover bounded behavior without requiring large fixture files.

---

- U5. **Restore style gate**

**Goal:** Make the repo's configured Python style gates pass.

**Requirements:** R6

**Dependencies:** U1-U4

**Files:**
- Modify: `src/opencortex/http/server.py`
- Modify: `src/opencortex/http/models.py`

**Approach:**
- Wrap the long docstring in `server.py`.
- Run the formatter on the scoped ruff include set or apply equivalent formatting to `models.py`.

**Patterns to follow:**
- Existing ruff configuration in `pyproject.toml`.

**Test scenarios:**
- Test expectation: none -- this unit is formatting and lint cleanup only.

**Verification:**
- `ruff check` and `ruff format --check` pass.

## System-Wide Impact

- **Interaction graph:** Storage adapter behavior affects HTTP search/list endpoints, Alpha knowledge, trace search, and batch import.
- **Error propagation:** Unknown filter operators become explicit errors. Internal callers must pass valid DSL; external request paths should already build or validate structured filters.
- **State lifecycle risks:** Batch import chunking must preserve current error collection and URI ordering semantics as much as practical.
- **API surface parity:** Python server and MCP scanner both get bounded behavior; public request/response models stay compatible.
- **Integration coverage:** Targeted unit tests are sufficient for U1-U3; U4 needs both scanner and Python batch behavior checks.
- **Unchanged invariants:** Existing collection names, record schemas, and public HTTP route names do not change.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Fail-fast filter translation surfaces a latent invalid filter in an existing path | Run targeted and broad tests; fix invalid callers rather than restoring match-all fallback |
| Bounded ordered filtering changes pagination completeness for very large pages | Keep bounds above normal page sizes and document degraded behavior |
| Scanner limits surprise users importing huge repos | Expose truncation metadata so operators can rerun with higher limits when intended |

## Documentation / Operational Notes

- No README update is required unless scanner limit defaults become user-facing configuration.
- If residual review findings remain after autofix, record them through the LFG residual handoff path.

## Sources & References

- Review findings from the immediately preceding repository review.
- Related code: `src/opencortex/storage/storage_interface.py`
- Related code: `src/opencortex/storage/qdrant/adapter.py`
- Related code: `src/opencortex/alpha/knowledge_store.py`
- Related code: `plugins/opencortex-memory/src/scan.ts`
