---
title: "refactor: Extract shared conversation adapter helper (§25 Phase 7)"
type: refactor
status: active
date: 2026-04-25
---

# refactor: Extract shared conversation adapter helper (§25 Phase 7)

## Overview

Extract `benchmarks/adapters/conversation_mapping.py` to deduplicate ~169 lines (10.83% jscpd) shared between `benchmarks/adapters/conversation.py` (660 lines, `LongMemEvalBench`) and `benchmarks/adapters/locomo.py` (763 lines, `LoCoMoBench`). Closes REVIEW closure tracker entries **R2-24**, **R4-P2-8**, **R4-P2-9**.

The duplicated code is pure mapping/transformation logic with no instance state — eight helpers total: five small free helpers, one byte-identical `_memory_record_snapshot` staticmethod, one structurally-identical `_map_session_uris` that diverges on its last 5 lines (single-best vs full-sorted return), and one newly-abstracted `extract_records_by_uri` (replaces an inline `{uri: dict(record)}` comprehension that appears in every store-path and mainstream-path response handler). The new module is a flat collection of public functions sibling to `benchmarks/adapters/base.py`, matching the existing flat-package convention. No mixin, no class hierarchy.

Behavior must be preserved byte-identically — both adapters must continue passing `tests.test_locomo_bench` (which exercises both adapters via `_OCStub`) without any test modification beyond the import sites that change. A new `tests/test_conversation_mapping.py` adds direct unit coverage for the helpers.

---

## Problem Frame

The `LongMemEvalBench` and `LoCoMoBench` adapters were grown independently and accumulated near-identical mapping helpers as the benchmark surface stabilized. The closure tracker (`docs/residual-review-findings/2026-04-24-review-closure-tracker.md`) lists this as a high-leverage cleanup that closes four findings in one PR:

- **R2-24** — 169 lines duplicated (10.83% jscpd) between `conversation.py` and `locomo.py`; shared helper missing
- **R4-P2-8** — adapter store-branch duplication; future timeout/summary/batch params will drift between the two
- **R4-P2-9** — adapter mapping helper duplication

Without extraction, every behavior tweak — for instance the `include_session_summary=False` perf optimization or the `asyncio.CancelledError` cascade fix from REVIEW REL-04 — has to be applied twice and risks drifting. Three of the five duplicated helpers (`_message_span`, `_overlap_width`, `_record_time_refs`) carry subtle msg_range/time_refs semantics that are easy to silently break by editing only one copy.

---

## Requirements Trace

- **R1.** Eliminate the 5 byte-identical free helpers from both `conversation.py` and `locomo.py`; both adapters import them from `benchmarks/adapters/conversation_mapping.py` instead.
- **R2.** Eliminate the byte-identical `_memory_record_snapshot` staticmethod from both adapters; replaced by a free async function in the new module.
- **R3.** Replace the divergent `_map_session_uris` in both adapters with a single `map_session_uris(..., return_all: bool = False)` that defaults to conversation.py's single-best behavior and exposes locomo.py's full-sorted-list behavior via `return_all=True`.
- **R4.** Both adapters use a shared `extract_records_by_uri(payload)` helper for the canonical `{uri: dict(record)}` shape used by the store-path and mainstream-path response handlers in both adapters. (Note: `beam.py` carries the same inline pattern but its adoption is deferred — see Scope Boundaries.)
- **R5.** Behavior parity: `tests.test_locomo_bench` and `tests.test_beam_bench` pass with no behavioral assertion changes (only import-site changes if any).
- **R6.** New `tests/test_conversation_mapping.py` unit-tests each public helper in isolation — at minimum: `normalize_text_set`, `message_span`, `ranges_overlap`, `overlap_width`, `record_time_refs`, `map_session_uris` (both `return_all=False` and `return_all=True`), `extract_records_by_uri`, `memory_record_snapshot`.
- **R7.** New regression lock `tests/test_conversation_mapping.py::test_store_and_mcp_paths_produce_equivalent_uri_mapping` (TG-3 from the closure tracker) — asserts both store-path and mcp-path produce the same URI mapping for the same fixture, locking the equivalence the refactor preserves.

---

## Scope Boundaries

- **`benchmarks/adapters/beam.py` (308 lines) is out of scope.** It only shares the call shape of `oc.benchmark_conversation_ingest(...)` and the `metadata_filter` dict. It has none of the 5 free helpers, no `_memory_record_snapshot`, no `_map_session_uris`. A follow-up PR can adopt `extract_records_by_uri` for `beam.py` once the helper proves stable; not in this PR.
- **`_lme_session_to_uri` mapping in `LongMemEvalBench` stays adapter-side.** The cross-haystack non-determinism (REVIEW F3 / ADV-003, still open P3) is LongMemEval-specific. Pulling it into a shared helper would falsely advertise it as a pattern both adapters use.
- **R2-33 (turn_id naming + 0/1-based offset divergence) is out of scope.** The closure tracker bundles it with this work as an action-queue line, but it's an orthogonal naming concern that risks scope creep. Track separately.
- **No identity/scope kwargs added to helpers.** The duplicated code uses the OC client's JWT-based identity (no `tid`/`uid` arguments). Extracted helpers stay identity-agnostic; tenant scoping is a server-side concern and was already addressed in PR #7 (U2 of plan 005).
- **No change to `EvalAdapter` ABC in `benchmarks/adapters/base.py`.** This refactor adds a sibling module; it does not modify the adapter contract.

### Deferred to Follow-Up Work

- **Beam adapter dedup** — apply `extract_records_by_uri` to `beam.py` once stable: separate small PR.
- **R2-33 turn_id/offset normalization** — separate small PR; scope is convention pinning, not code dedup.
- **Migration of `_lme_session_to_uri` non-determinism (F3 / ADV-003)** — separate fix; depends on a deterministic ordering decision that's still open.

---

## Context & Research

### Relevant Code and Patterns

- **`benchmarks/adapters/conversation.py`** (660 lines) — `LongMemEvalBench`. Free helpers at lines 29–81; `_memory_record_snapshot` at 226–247; `_map_session_uris` at 249–306; store-branch ingest at 417–451.
- **`benchmarks/adapters/locomo.py`** (763 lines) — `LoCoMoBench`. Free helpers at lines 170–284; `_memory_record_snapshot` at 343–364; `_map_session_uris` at 366–421; store-branch ingest at 538–583.
- **`benchmarks/adapters/base.py`** — `EvalAdapter` ABC + dataclasses (`IngestResult`, `QAItem`). Public symbols (no leading underscore). Sets the naming convention for the new module.
- **`benchmarks/adapters/__init__.py`** — flat package, no nested directories. Sibling-module placement matches convention; no `benchmarks/common/` precedent exists.
- **`benchmarks/unified_eval.py`** — sole consumer of the three adapter modules. Not affected by this refactor (helpers are internal to the adapters).
- **`tests/test_locomo_bench.py`** — exercises both `LoCoMoBench` (7 async + 1 sync) and `LongMemEvalBench` (6 async) via `_OCStub`. Critical test `test_ingest_prefers_tightest_overlapping_merged_record` (lines 197–235) locks the `return_all=True` semantic for locomo.
- **`tests/test_beam_bench.py`** — does not exercise the duplicated helpers; will not require changes.

### Institutional Learnings

- **Closure tracker** (`docs/residual-review-findings/2026-04-24-review-closure-tracker.md`) — R2-24 / R4-P2-8 / R4-P2-9 are listed as the same item under §25 Phase 7. Action queue ranks this #2 highest-leverage. Recommends pairing with **TG-3** (store-path vs mcp-path URI mapping equivalence test) as the regression lock — adopted as R7 above.
- **PR #4 / REL-04** (`docs/residual-review-findings/feat-benchmark-offline-conv-ingest.md`) — fixed `asyncio.gather(_process_one, ..., return_exceptions=True)` cancellation cascade in **both** `locomo.py:586` and `conversation.py:471`. The shared helper extraction must not regress this; the cascade itself stays in the adapters since it wraps adapter-specific orchestration, but the per-item handler comments must be preserved verbatim if any code around them moves.
- **F3 / ADV-003** (open P3) — `_lme_session_to_uri` non-determinism under U13 concurrency is LME-specific. Confirms the boundary call: do not pull this mapping into the shared module.
- **`docs/solutions/`** — no prior entries on benchmark adapter patterns or shared-helper extraction. After this lands, this PR is a strong `/ce-compound` capture candidate.

### External References

None — this is pure refactor; no external library guidance needed.

---

## Key Technical Decisions

- **Module shape: flat collection of public functions, not a class.** Reasons: (1) all duplicated logic is already pure or `@staticmethod`/`@classmethod` with no instance state; (2) the two adapters' `ingest()` orchestration is too divergent to share a base class — `LoCoMoBench` records `_session_candidates_by_key`, `LongMemEvalBench` records `_lme_session_to_uri`; (3) flat-functions matches the `base.py` precedent.
- **Public symbol naming: drop the leading underscore.** `_message_span` becomes `message_span` etc. The new module is a public API for its sibling modules; underscore prefixes signal module-private and would be misleading.
- **`map_session_uris` exposes the divergence as a `return_all: bool = False` kwarg.** `False` matches conversation.py's "single tightest URI" return; `True` matches locomo.py's "full sorted list, defer tie-break to caller". Default chosen to match the simpler (conversation) caller. The locomo call site sets `return_all=True` explicitly.
- **`memory_record_snapshot` becomes `async def memory_record_snapshot(oc) -> Dict[str, Dict]`** — was `@staticmethod async def _memory_record_snapshot(oc)` on both adapter classes. Free function with the `oc` argument is a smaller surface than a class with no state.
- **`extract_records_by_uri(payload)` is purely about record extraction** — does not bake in any `ingest_shape` assumption. Both store-path (no `ingest_shape` kwarg) and mainstream-path (`ingest_shape="direct_evidence"`) callers feed their already-built payload through it.
- **Wiring strategy: import + delete in one PR, no compatibility shim.** Both adapter classes are internal to `benchmarks/`; nothing imports `_message_span` or the other helpers from outside. Compat shim would be dead weight.
- **Module file conventions** — `# SPDX-License-Identifier: Apache-2.0` header, `from __future__ import annotations` per recent autofix (KP-02 in plan 005).

---

## Open Questions

### Resolved During Planning

- **Should beam.py be included?** No — it shares only the call shape, not the helpers. Out of scope; follow-up PR.
- **Class hierarchy or flat functions?** Flat functions — see Key Technical Decisions.
- **How to handle the `_map_session_uris` divergence?** `return_all` kwarg, default `False` — see Key Technical Decisions.
- **Drop underscore prefix on symbols?** Yes — public helper module, matches `base.py` convention.
- **Should we add unit tests for the helpers?** Yes — `tests/test_conversation_mapping.py` (R6). Existing integration tests in `test_locomo_bench.py` provide behavior-parity coverage; unit tests give the helper module independent regression protection.
- **Should we bundle R2-33 (turn_id naming)?** No — out of scope, separate PR.
- **Should we add the TG-3 store/mcp equivalence test?** Yes — bundled as R7. The closure tracker explicitly recommends this pairing.

### Deferred to Implementation

- **Final import grouping in the two adapters** — whether to import individually (`from benchmarks.adapters.conversation_mapping import message_span, normalize_text_set, ...`) or as a module (`from benchmarks.adapters import conversation_mapping as cm`). Decide during implementation based on call-site readability.
- **Whether the integration tests need any import-site changes** — the helpers are referenced by name inside the adapter classes, so test files that import only `LoCoMoBench` / `LongMemEvalBench` should not need changes. Confirm during U4.

---

## Implementation Units

- [x] U1. **Create `benchmarks/adapters/conversation_mapping.py` with five pure free helpers**

**Goal:** Stand up the new module with the five byte-identical free helpers (`normalize_text_set`, `message_span`, `ranges_overlap`, `overlap_width`, `record_time_refs`) so subsequent units have somewhere to land.

**Requirements:** R1

**Dependencies:** None

**Files:**
- Create: `benchmarks/adapters/conversation_mapping.py`
- Test: `tests/test_conversation_mapping.py`

**Approach:**
- Copy bodies verbatim from `conversation.py:29–81` (which is byte-identical to `locomo.py:170–284`). Drop the leading underscore on each public symbol.
- Module starts with `# SPDX-License-Identifier: Apache-2.0` and `from __future__ import annotations`.
- Module docstring cites §25 Phase 7 + R2-24 closure context.
- No adapter changes in this unit — adapters keep their local copies until U4.

**Patterns to follow:**
- `benchmarks/adapters/base.py` (public symbols, module-level docstring, free functions)
- Recent autofix conventions from plan 005 (KP-02 future annotations, SPDX header)

**Test scenarios:**
- Happy path: `normalize_text_set(["A", "a", "  b  ", ""])` → `{"a", "b"}` (lowercased, stripped, empties removed)
- Happy path: `message_span([{"msg_index": 3}, {"msg_index": 7}])` → `(3, 7)`
- Edge case: `message_span([])` → `None`
- Edge case: `message_span([{"msg_index": None}])` → `None`
- Happy path: `ranges_overlap((0, 5), (3, 8))` → `True`; `((0, 5), (6, 9))` → `False`
- Edge case: `ranges_overlap((0, 5), (5, 10))` → `True` (boundary inclusive — verify against current behavior)
- Happy path: `overlap_width((0, 5), (3, 8))` → `3` (msgs 3, 4, 5)
- Edge case: `overlap_width((0, 5), (10, 15))` → `0`
- Happy path: `record_time_refs({"meta": {"time_refs": ["a", "b"]}})` → `["a", "b"]`
- Edge case: `record_time_refs({})` → `[]` (current behavior — verify)

**Verification:**
- New module imports cleanly: `python -c "from benchmarks.adapters import conversation_mapping"`
- All test scenarios above pass
- `wc -l benchmarks/adapters/conversation_mapping.py` shows ~80–100 lines (5 helpers + module docstring + header)

---

- [x] U2. **Add `memory_record_snapshot` and `extract_records_by_uri` to the new module**

**Goal:** Lift the two reusable I/O-adjacent helpers into the shared module. `memory_record_snapshot` is byte-identical between adapters; `extract_records_by_uri` is the new abstraction over the `{str(r["uri"]): dict(r) for r in payload["records"] if r["uri"]}` pattern that appears in both store and mainstream branches.

**Requirements:** R2, R4

**Dependencies:** U1 (same module)

**Files:**
- Modify: `benchmarks/adapters/conversation_mapping.py`
- Test: `tests/test_conversation_mapping.py`

**Approach:**
- `async def memory_record_snapshot(oc) -> Dict[str, Dict[str, Any]]` — verbatim body from `conversation.py:226–247` (byte-identical to `locomo.py:343–364`), with `self` removed and `oc` as the first argument.
- `def extract_records_by_uri(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]` — the canonical `{str(r.get("uri", "") or ""): dict(r) for r in payload.get("records", []) if str(r.get("uri", "") or "")}` pattern. Make robust to: `payload` missing the `records` key (returns `{}`), records with empty/missing `uri` (filtered out), records with a non-string `uri` (coerced to string then filtered).

**Patterns to follow:**
- The legacy comprehension at `conversation.py:424–428` and `locomo.py:548–552`

**Test scenarios:**
- Happy path: `extract_records_by_uri({"records": [{"uri": "u1", "content": "x"}, {"uri": "u2", "content": "y"}]})` → `{"u1": {"uri": "u1", "content": "x"}, "u2": {...}}`
- Edge case: empty / missing `records` key → `{}`
- Edge case: record with `uri=""` filtered out
- Edge case: record with `uri=None` filtered out (after string coercion → `"None"` is a separate concern; verify the current code path treats it as filtered)
- Edge case: records dict-copy semantics — mutating the result does not affect input
- For `memory_record_snapshot`: integration-style with a stub `oc` that exposes `memory_list` returning a fixed payload, asserts the resulting dict matches the legacy `_memory_record_snapshot` shape exactly

**Verification:**
- All scenarios pass
- `extract_records_by_uri` body is < 10 lines (single comprehension + early-return guard)

---

- [x] U3. **Add `map_session_uris` with `return_all` parameter**

**Goal:** Extract the structurally-identical `_map_session_uris` from both adapters into a single function that exposes the divergent return shape via a kwarg. Conversation.py default (`return_all=False`) returns a single-element list with the tightest-overlap URI; locomo.py (`return_all=True`) returns the full sorted list so the caller can apply its own tie-break.

**Requirements:** R3

**Dependencies:** U1 (same module)

**Files:**
- Modify: `benchmarks/adapters/conversation_mapping.py`
- Test: `tests/test_conversation_mapping.py`

**Approach:**
- Copy the structurally-identical body (conversation.py:249–306 / locomo.py:366–421). The first ~50 lines match line-for-line; the divergence is in the last 5 lines (single-best vs full-sorted).
- Add `return_all: bool = False` as a keyword-only parameter at the end of the signature. When `False`, return `[best_uri]` if any, else `[]`. When `True`, return the full sorted list.
- Document the parameter clearly: "When `False`, returns a single-element list with the tightest-overlap URI for each session — matches the conversation.py contract. When `True`, returns the full sorted list and defers tie-break to the caller — matches the locomo.py contract."
- Preserve the per-session sorting semantics exactly. The locomo path relies on the lexical tie-break that `_select_best_session_uri` applies later in `build_qa_items`.

**Patterns to follow:**
- The structurally-identical body in both source files
- Keyword-only `return_all: bool = False` matches the project's `*, kwarg_only` style (see `SessionRecordsRepository.load_merged` from plan 005)

**Test scenarios:**
- Happy path (`return_all=False`): one session with two overlapping records, helper returns `[winning_uri]`
- Happy path (`return_all=True`): same fixture, helper returns sorted list with both URIs in tightest-first order
- Critical: reproduces the fixture from `test_ingest_prefers_tightest_overlapping_merged_record` (tests/test_locomo_bench.py:197–235) as a unit test in `test_conversation_mapping.py`, asserts the tightest URI is first in the `return_all=True` list (this is what `_select_best_session_uri` then picks). Distinct from the U5 integration test, which exercises store-vs-mcp path equivalence.
- Edge case: empty `records_by_uri` → `{}`
- Edge case: session with no overlapping records → session key absent from result dict
- Edge case: session with `time_refs` only (no `msg_range`) — falls back to time-refs intersection (verify against current code path)
- Edge case: `return_all=False` with multiple equally-overlapping URIs — returns the lexically first (preserved from current code path)

**Verification:**
- All scenarios pass
- Function body is the union of both legacy bodies plus the conditional return at the end (~60–65 lines)

---

- [x] U4. **Wire both adapters to use `conversation_mapping`; delete local copies**

**Goal:** Switch `LongMemEvalBench` and `LoCoMoBench` to import the seven helpers from `benchmarks.adapters.conversation_mapping`. Delete the local copies from both adapters. Verify byte-identical behavior via the existing benchmark suite.

**Requirements:** R1, R2, R3, R4, R5

**Dependencies:** U1, U2, U3

**Files:**
- Modify: `benchmarks/adapters/conversation.py`
- Modify: `benchmarks/adapters/locomo.py`

**Approach:**
- Add `from benchmarks.adapters import conversation_mapping as cm` to both files (cleaner than per-symbol import — call sites are dense and grouped).
- Replace `self._normalize_text_set(x)` → `cm.normalize_text_set(x)` etc. The five pure helpers were already `@staticmethod` or free functions with no `self` access — no behavioral change.
- Replace `self._memory_record_snapshot(oc)` → `cm.memory_record_snapshot(oc)`.
- Replace the inline `{str(r.get("uri", "") or ""): dict(r) for r in payload.get("records", [])}` comprehensions in the store and mainstream branches → `cm.extract_records_by_uri(payload)`.
- Replace `self._map_session_uris(...)` → `cm.map_session_uris(..., return_all=False)` in `conversation.py` and `cm.map_session_uris(..., return_all=True)` in `locomo.py`.
- Delete the seven now-unused local methods from both classes. Preserve the surrounding comments (especially `include_session_summary=False` perf note and the `asyncio.CancelledError` REL-04 cite) — these stay in the adapters because they document adapter-specific decisions, not helper contracts.
- Do **not** modify `_lme_session_to_uri` mapping in `conversation.py` — stays adapter-side per scope boundaries.
- Do **not** touch the `asyncio.gather(..., return_exceptions=True)` cascade in either adapter — that's REL-04 protected and lives in adapter orchestration, not the helper.

**Patterns to follow:**
- Module-alias import style (`as cm`) keeps call sites scannable when 7 symbols are used densely
- Preserve adapter-specific comments verbatim

**Test scenarios:**
- Behavior parity: full `tests.test_locomo_bench` suite passes with no test changes
- Behavior parity: full `tests.test_beam_bench` suite passes (unaffected, but verify)
- Behavior parity: `tests.test_http_server` + `tests.test_locomo_bench` end-to-end smoke runs cleanly
- No regressions in `tests.test_e2e_phase1` / `tests.test_context_manager` (these don't use the adapters but should be sanity-checked)
- Net line count: `wc -l benchmarks/adapters/{conversation,locomo}.py` shows a reduction of ~160 lines combined (conversation.py loses ~80, locomo.py loses ~80). The new helper module gains ~120 lines and the new test file gains ~80 lines, so total project line count rises by ~120 — acceptable trade-off for the dedup payoff and the new regression coverage.

**Verification:**
- All listed test suites green
- `git diff --stat` shows expected line deltas
- Manual code review: no `self._normalize_text_set` / `self._message_span` / etc. references remain in either adapter (`grep -E "self\._(normalize_text_set|message_span|ranges_overlap|overlap_width|record_time_refs|memory_record_snapshot|map_session_uris)" benchmarks/adapters/{conversation,locomo}.py` returns nothing)

---

- [x] U5. **TG-3 — store/mcp URI mapping equivalence regression test**

**Goal:** Add `tests/test_conversation_mapping.py::test_store_and_mcp_paths_produce_equivalent_uri_mapping` — closes TG-3 from the closure tracker. Asserts the store-path and mcp-path produce the same per-session URI mapping for the same fixture, locking the equivalence the refactor preserves and catching any future drift between the two ingest paths.

**Requirements:** R7

**Dependencies:** U4 (need the wired adapter to exercise both paths)

**Files:**
- Modify: `tests/test_conversation_mapping.py`

**Approach:**
- Build a small fixture conversation with two sessions and known msg_range / time_refs.
- Run the same fixture through both: (a) the store-path branch (`oc.benchmark_conversation_ingest(...)` → `extract_records_by_uri` → `map_session_uris`); (b) the mcp-path branch (`oc.context_end(...)` → `memory_record_snapshot(oc)` → `map_session_uris`).
- Use a stub `oc` modeled on `tests/test_locomo_bench.py::_OCStub` that records calls and returns deterministic payloads.
- Assert both paths produce identical `Dict[int, List[str]]` session→URI mappings.

**Patterns to follow:**
- `tests/test_locomo_bench.py::_OCStub` — already provides the `memory_list` / `benchmark_conversation_ingest` / `context_end` surface needed
- Async test pattern: `unittest.IsolatedAsyncioTestCase`

**Test scenarios:**
- Two-session fixture, both paths produce the same mapping
- Edge case: empty mcp diff (no records added) → both paths return `{}`
- Edge case: store-path returns a record the mcp-path doesn't (simulating partial failure) → assertion clearly identifies which path is missing it (defensive — this would be a future bug, not current behavior)

**Verification:**
- Test passes against the wired adapter
- Test fails (with a clear diff message) if either path is silently mutated to produce a different mapping shape

---

## System-Wide Impact

- **Interaction graph:** Two callers (`LongMemEvalBench`, `LoCoMoBench`) → one new module (`conversation_mapping`). No new external dependencies. No callbacks, middleware, or observers triggered.
- **Error propagation:** Helpers are pure / I/O-adjacent (only `memory_record_snapshot` calls `oc.memory_list`). Errors propagate identically — the helpers do not catch or transform exceptions.
- **State lifecycle risks:** None — helpers are stateless. Adapter-side state (`_lme_session_to_uri`, `_session_candidates_by_key`) stays in the adapters.
- **API surface parity:** The new module is internal to `benchmarks/`. `unified_eval.py` (sole external consumer of the adapters) is unaffected. The `EvalAdapter` ABC is unchanged.
- **Integration coverage:** `tests.test_locomo_bench` exercises both adapters end-to-end with `_OCStub`. The new TG-3 test (U5) adds the cross-path equivalence lock that the existing tests do not check.
- **Unchanged invariants:**
  - `EvalAdapter` ABC contract in `benchmarks/adapters/base.py`
  - `LongMemEvalBench.ingest()` / `LoCoMoBench.ingest()` orchestration shape (segment building, semaphore, cancellation cascade)
  - REL-04 `asyncio.gather(..., return_exceptions=True)` per-item cancellation isolation in both adapters
  - LongMemEval-specific `_lme_session_to_uri` mapping (stays in `conversation.py`)
  - LoCoMo-specific `_session_candidates_by_key` accumulation (stays in `locomo.py`)
  - `oc.benchmark_conversation_ingest(...)` request shape (this PR doesn't touch the OC client)

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `map_session_uris` parametrization (`return_all`) collapses the wrong way and silently changes one adapter's behavior. | Critical fixture from `test_ingest_prefers_tightest_overlapping_merged_record` reproduced as a unit test in U3. Both `return_all=False` and `return_all=True` paths covered explicitly. |
| Helper extraction silently drops a comment that documents a load-bearing decision (e.g., `include_session_summary=False` perf note, REL-04 cancellation cite). | U4 explicitly preserves adapter-side comments verbatim. The helpers themselves do not need these comments — they belong with the adapter call sites that own the decision. |
| Future contributor re-introduces the duplicate by adding a new helper to one adapter and not the other. | The new module's docstring states "any helper used by ≥2 adapters lives here, not in the adapter class". Pairs with R2-24 closure in the tracker as the canonical decision record. |
| Beam.py drift risk — the `extract_records_by_uri` pattern exists in beam.py but is not extracted in this PR. | Documented as deferred follow-up. Low priority — beam.py uses a simpler one-shot ingest with no inner-session mapping; the dedup payoff is small. |
| Test changes required to import paths in `tests/test_locomo_bench.py`. | The tests import `LoCoMoBench` / `LongMemEvalBench` only — they do not reach into the helpers. No test changes expected. Verify in U4 by running the test suite without modifying it. |

---

## Documentation / Operational Notes

- Update `CLAUDE.md` directory map under `benchmarks/adapters/` to include `conversation_mapping.py` (sibling to `base.py`). Same pattern as plan 005's PS-001 fix that added the new `context/` modules.
- After landing, this PR is a strong `/ce-compound` capture candidate — `docs/solutions/` has zero entries on benchmark adapter patterns or shared-helper extraction. Capture decision: "When two adapters accumulate near-identical mapping helpers, extract a public free-function module sibling to `base.py` rather than introducing a base class. Use a `return_all`-style kwarg for load-bearing divergence in otherwise-identical functions."
- Update `docs/residual-review-findings/2026-04-24-review-closure-tracker.md` after merge: flip R2-24, R4-P2-8, R4-P2-9 from ❌ to ✅ with closing commit hash; remove the action-queue line item.

---

## Sources & References

- **Closure tracker:** [docs/residual-review-findings/2026-04-24-review-closure-tracker.md](../../docs/residual-review-findings/2026-04-24-review-closure-tracker.md) — R2-24 (line 164), R4-P2-8 (line 266), R4-P2-9 (line 267), §25 Phase 7 (line 328), action queue ranking (line 363), TG-3 pairing (line 385)
- **Source code:** `benchmarks/adapters/conversation.py`, `benchmarks/adapters/locomo.py`, `benchmarks/adapters/base.py`
- **Tests:** `tests/test_locomo_bench.py`, `tests/test_beam_bench.py`
- **Prior PR closures:** PR #7 (plan 005, server-side patterns) — closed §25 Phases 3+5+6; this PR closes Phase 7 as the orthogonal complement
- **Related learnings:** `docs/residual-review-findings/feat-benchmark-offline-conv-ingest.md` — REL-04 cancellation cascade (preserve), F3/ADV-003 `_lme_session_to_uri` non-determinism (out of scope)
