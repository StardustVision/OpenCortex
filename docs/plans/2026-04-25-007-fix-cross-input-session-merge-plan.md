---
title: "fix: Prevent cross-input-session merge in benchmark splitter (R3-RC-02 / R2-14)"
type: fix
status: active
date: 2026-04-25
---

# fix: Prevent cross-input-session merge in benchmark splitter (R3-RC-02 / R2-14)

## Overview

Force `_build_recomposition_segments` to split at input-segment boundaries when the entries originate from `_benchmark_recomposition_entries`. Today the benchmark splitter treats `normalized_segments` (the list of input sessions) as one continuous `msg_index` stream and re-splits purely by token / time_refs / message-count caps. Same-date adjacent sessions whose combined size stays under the caps **silently merge into one merged leaf**, losing session-level attribution.

LoCoMo's adapter consumes session-level msg ranges via `cm.map_session_uris`, so a leaf whose `msg_range` spans two input sessions becomes a tied-overlap candidate for both — recall scoring degrades on the wide leaf, and `return_all=False` callers (LongMemEval) get an off-by-one URI mapping (one session gets the leaf, the other gets nothing).

The fix tags every `RecompositionEntry` with its source input-segment index and adds a hard split when adjacent entries originate from different input segments. This restores the invariant **"a merged leaf's msg_range is a subset of one input segment's msg range"** for the benchmark path. Production conversation-lifecycle entries (which have no input-segment notion) keep their existing behavior via a `None` sentinel on the new field.

While this PR is open in the splitter / test areas, it also closes two adjacent test-only items from the same closure tracker bucket: **R3-RC-08** (assert `recomposition_stage='benchmark_offline'` in HTTP test) and **R3-RC-09** (lock the `f"...-{start:06d}-{end:06d}"` zero-padded URI contract via regression test).

---

## Problem Frame

The CE review at `.context/compound-engineering/ce-code-review/20260424-152926-6301c860/REVIEW.md` flagged R3-RC-02 (= R2-14) at confidence 75:

> `_benchmark_recomposition_entries` 跨 input-session 用单一 `msg_index` 流 + 按 time_refs / token 重切分——同日期多 session 可能被静默合并为一条 merged leaf，丢失 session-level 归因。LoCoMo 适配器的 URI 映射会 off-by-one。

**Concrete trigger**: a LoCoMo conversation with two adjacent sessions of 6 + 4 messages, both dated 2026-04-25:

- `_benchmark_recomposition_entries` produces 10 message-level entries with global msg_index 0..9 and identical time_refs `{"2026-04-25"}`.
- `_build_recomposition_segments` checks: `current_messages < _SEGMENT_MAX_MESSAGES (16)`, `current_tokens < _SEGMENT_MAX_TOKENS (1200)`, time_refs overlap (same date) → no split.
- One merged leaf created with `msg_range=[0, 9]`.
- LoCoMo's `session_spans = {1: (0, 5), 2: (6, 9)}`. The leaf overlaps both with `overlap_width=6` and `overlap_width=4` respectively.
- `cm.map_session_uris(return_all=True)` returns the wide leaf at the head of session 1's candidate list AND in session 2's list. `_select_best_session_uri` then has to disambiguate via question-aware scoring — degraded, not broken.
- LongMemEval's `cm.map_session_uris(return_all=False)` picks one session via lex-tie-break and silently drops the leaf for the other session — broken, not degraded.

This is real for typical LoCoMo / LongMemEval inputs, not a synthetic edge case. The constants `_SEGMENT_MAX_MESSAGES=16` / `_SEGMENT_MAX_TOKENS=1200` are wide enough that 2-3 small adjacent sessions fit comfortably.

---

## Requirements Trace

- **R1.** `_build_recomposition_segments` MUST split at input-segment boundaries on the benchmark path. After the fix, no merged leaf produced from a multi-segment benchmark input has a `msg_range` that crosses an input-segment boundary.
- **R2.** Production conversation-lifecycle behavior MUST be unchanged. Entries built by `_build_recomposition_entries` and the inline re-derivation in `_run_full_session_recomposition` continue to produce identical splits. Tests `test_full_session_recomposition_preserves_leaves_and_creates_directories` and `test_full_session_recomposition_preserves_leaf_uris_unchanged` pass without modification.
- **R3.** The fix is keyed on a new `source_segment_index: Optional[int]` field on `RecompositionEntry`. Benchmark entries set the field to the input-segment index; non-benchmark entries set it to `None`. The split condition only fires when both adjacent entries have non-None values.
- **R4.** A new regression test reproduces the bug (fails before the fix, passes after) and locks the cross-segment-boundary invariant.
- **R5.** R3-RC-08 closure: `tests/test_http_server.py` benchmark merged-leaf assertions include `recomposition_stage == "benchmark_offline"` so any future change that silently rebrands the stage surfaces here.
- **R6.** R3-RC-09 closure: a regression test in `tests/test_context_manager.py` (or a dedicated URI utility test file) locks `_merged_leaf_uri`'s zero-padded format `f"conversation-{hash}-{start:06d}-{end:06d}"`. Adapter-side `sorted(new_records)` depends on lex sort = numeric sort up to 6 digits; format drift would silently break LoCoMo's URI ordering.

---

## Scope Boundaries

- **R3-RC-03** (`source_uri=None` filter degrades to session-wide scan) — out of scope. Independent concern; separate PR.
- **R3-RC-05** (dict-merge latent bug under partial time_refs meta) — out of scope. Currently dead code under the test fixture; revisit when a real trigger surfaces.
- **R3-RC-06** (`content` field empty string) — already closed by PR #3.
- **No changes to LoCoMo / LongMemEval adapter code.** The fix lives entirely in the splitter; both adapters keep their current `cm.map_session_uris(return_all=...)` call shape.
- **No changes to URI format.** `_merged_leaf_uri`'s zero-padded format stays exactly as-is. R3-RC-09 only adds a test that locks the format, it does not modify the function.
- **No changes to the production conversation lifecycle splitter behavior.** R3 above is the explicit guard.
- **No changes to `_SEGMENT_MAX_MESSAGES` / `_SEGMENT_MAX_TOKENS` / `_SEGMENT_MIN_MESSAGES` constants.** The cap values are not the bug; the missing input-segment boundary check is.

---

## Context & Research

### Relevant Code and Patterns

- **`src/opencortex/context/manager.py`** — `_benchmark_recomposition_entries` (line 1584): builds `RecompositionEntry` objects from `normalized_segments`, increments `msg_index` once per message across all input segments. **Bug origin** — does not record which input segment each entry belongs to.
- **`src/opencortex/context/manager.py`** — `_build_recomposition_segments` (line 1979): the splitter. Three split conditions today: message cap, token cap, time_refs gap. **Missing**: input-segment boundary check.
- **`src/opencortex/context/manager.py`** — `_build_recomposition_entries` (line 1879): production conversation-lifecycle counterpart. Does NOT have an input-segment notion (live messages stream from `Observer`); will set `source_segment_index=None`.
- **`src/opencortex/context/manager.py`** — inline builder inside `_run_full_session_recomposition` (~line 3080): re-derives entries from already-stored merged records. Also will set `source_segment_index=None`.
- **`src/opencortex/context/recomposition_types.py`** — `RecompositionEntry` TypedDict. Add `source_segment_index: Optional[int]` here; the docstring already calls out the three construction sites that need to stay shape-aligned.
- **`src/opencortex/context/manager.py`** — `_merged_leaf_uri` (line 1092): the zero-padded URI format. R3-RC-09 lock target.
- **`benchmarks/adapters/conversation_mapping.py`** — `map_session_uris` consumes per-leaf `msg_range`. Behavior under the fix: every benchmark merged leaf now has a `msg_range` that fits inside a single input segment, so each leaf maps to exactly one session — no more cross-session ties.
- **`tests/test_benchmark_ingest_service.py`** — direct unit-test home for the service (added in PR #7). The bug-reproducing test belongs here, exercising `_build_recomposition_segments` end-to-end via `service.ingest()` with a multi-segment fixture.
- **`tests/test_http_server.py`** — `test_04c_benchmark_conversation_ingest_preserves_traceability_contract` (line ~810). R3-RC-08 lock target.
- **`tests/test_context_manager.py`** — has existing `_full_session_recomposition_*` tests (lines 769, 850). R2 verification gate.

### Institutional Learnings

- **Closure tracker** (`docs/residual-review-findings/2026-04-24-review-closure-tracker.md`) — R3-RC-02 + R2-14 are top of medium tier, ranked #3 in the action queue. Same root cause; this PR closes both with one fix. R3-RC-08 + R3-RC-09 are explicitly noted as natural pairings.
- **PR #7 / U2** added `source_tenant_id` / `source_user_id` push-down to `SessionRecordsRepository`. Same defensive pattern: tag the entry with its scope at construction time and let the consumer enforce the invariant. The `source_segment_index` field this plan adds follows the same convention.
- **PR #8** (Phase 7) extracted `cm.map_session_uris` with `return_all` kwarg. The fix here makes the `return_all=False` path (LongMemEval) actually correct for multi-segment inputs; the `return_all=True` path (LoCoMo) gets sharper candidate lists with no scoring degradation.

### External References

None — pure internal correctness fix.

---

## Key Technical Decisions

- **Fix direction (a) over (b) and (c).** Three options were on the table:
  - **(a)** Add `source_segment_index` to `RecompositionEntry` and force split in `_build_recomposition_segments` when adjacent entries cross segments. **Chosen.** Smallest blast radius; lives in the splitter; production lifecycle untouched via `None` sentinel.
  - **(b)** Reset `msg_index` per input segment in `_benchmark_recomposition_entries`. Rejected: changes the URI scheme (msg_range becomes per-segment local), cascading changes to LoCoMo's `session_spans` derivation and downstream URI ordering. Bigger surface area for a smaller win.
  - **(c)** Adapter-side fix in `cm.map_session_uris`. Rejected: doesn't fix LongMemEval's off-by-one, only papers over LoCoMo's degraded scoring. The bug is in the producer (splitter), not the consumer.
- **`source_segment_index` is `Optional[int]`, not `int`.** Production conversation-lifecycle entries (built by `_build_recomposition_entries` and the inline re-derivation in `_run_full_session_recomposition`) have no input-segment notion. Forcing them to set a sentinel like `-1` invites accidental "all entries match -1 → no split" bugs. `None` makes the absence explicit and the split condition can be a clean `if entry["source_segment_index"] is not None and prev["source_segment_index"] is not None and they_differ`.
- **Split condition is `prev != current`, not `set of prior != current`.** Entries arrive in order. Once we cross a boundary (`prev_seg=0, current_seg=1`), the new segment starts and we never look back. The check is local — last entry's `source_segment_index` vs the new one's. This is also what guarantees the production path is unchanged: any pair where either side is `None` skips the check.
- **Failing test first.** U1 writes the regression test before U2/U3. The test must fail on master (proving the bug is real) and pass after U3. This is the strongest evidence the fix actually addresses R3-RC-02.
- **R3-RC-08 / R3-RC-09 bundled, not split into separate PRs.** Both are test-only assertions in the same code area; bundling saves a CI round-trip and keeps the closure-tracker bucket together.

---

## Open Questions

### Resolved During Planning

- **Where does `_benchmark_recomposition_entries` live after PR #7?** Verified: still in `manager.py` at line 1584. PR #7 moved the *orchestration* into `BenchmarkConversationIngestService` but `_benchmark_recomposition_entries` and `_build_recomposition_segments` stayed on `ContextManager` because the inline-re-derive site in `_run_full_session_recomposition` also calls them.
- **Does the bug actually trigger?** Yes — for typical LoCoMo inputs with adjacent same-date sessions whose combined size stays under `_SEGMENT_MAX_MESSAGES=16` / `_SEGMENT_MAX_TOKENS=1200`. Two sessions of 6+4 messages with the same date is a realistic LoCoMo shape and triggers cleanly.
- **Which fix direction is correct?** (a) — see Key Technical Decisions.
- **Does the production conversation lifecycle need the same fix?** No. The production splitter (`_build_recomposition_entries`) consumes a single live message stream from `Observer`; there's no notion of "input segment" to bound. Setting `source_segment_index=None` for these entries skips the new check.
- **Should we also fix `_load_session_merged_records`-side filter degradation (R3-RC-03)?** No. Out of scope per Scope Boundaries.

### Deferred to Implementation

- **Exact location of the failing regression test.** Two viable homes — both bypass `tests/test_benchmark_ingest_service.py` because that file's `_FakeManager` stubs the splitter and a test built on it would pass vacuously. Either (a) `tests/test_context_manager.py` with a new test class instantiating a real `ContextManager` (preferred for speed; pure-logic test) OR (b) `tests/test_benchmark_ingest_lifecycle.py` extended with a multi-segment fixture (slower; full-stack). Decide during U1 — recommendation is (a). See U1 Approach for details.
- **Whether to surface `source_segment_index` in any logged/exported context.** Probably not — it's an internal splitter signal — but check during U3 whether any debug log would benefit.
- **Exact wording of the docstring update on `RecompositionEntry`.** The TypedDict already documents the three construction sites; just add the field with one-line semantics for each site.

---

## Implementation Units

- [x] U1. **Failing regression test for cross-input-session merge**

**Goal:** Lock the bug by writing a test that fails on master and will pass after U2 + U3. Establishes the verification gate the fix is graded against.

**Requirements:** R1, R4

**Dependencies:** None

**Files:**
- Test: `tests/test_context_manager.py` (add new test class `TestBenchmarkSplitterCrossSegmentBoundary`) **OR** new file `tests/test_benchmark_recomposition_splitter.py`. **NOT** `tests/test_benchmark_ingest_service.py` — see Approach for why.

**Approach:**
- **Critical**: U1 must hit the **real** `ContextManager._benchmark_recomposition_entries` and `_build_recomposition_segments`. The `_FakeManager` in `tests/test_benchmark_ingest_service.py` stubs both with simplified pass-throughs (no real splitting logic, no `RecompositionEntry` construction), so a test built on those fakes would pass vacuously regardless of whether the bug is fixed.
- Two acceptable structures:
  - **(a) Direct splitter test** (preferred for speed): instantiate a real `ContextManager` (or call the static-ish methods directly if possible), build the multi-segment fixture as `normalized_segments`, call `_benchmark_recomposition_entries(normalized_segments)` → `_build_recomposition_segments(entries)`, then assert every output segment's `msg_range` fits inside a single input-segment's span. No service / orchestrator needed.
  - **(b) Lifecycle integration**: extend `tests/test_benchmark_ingest_lifecycle.py` with a multi-segment fixture and assert against the resulting merged-leaf URIs / `msg_range` values. Slower but covers the full path.
- Recommendation: do (a). The bug lives in pure logic; integration coverage is already provided by the existing benchmark suite.
- Multi-segment fixture: two adjacent input segments with 6 + 4 messages, identical `event_date` (e.g., `"2026-04-25"`), content sized to stay within `_SEGMENT_MAX_TOKENS=1200` (~80 tokens per message keeps total well under cap).
- Assertions on the output `segments` list:
  - Input segment 0 entries cover `msg_range` [0, 5]; input segment 1 entries cover [6, 9].
  - For every `segment` in the result, `segment["msg_range"]` is a subset of either [0, 5] OR [6, 9]. No segment has `msg_range[0] <= 5 AND msg_range[1] >= 6`.
- Mark the test with a comment that says **this test must fail on master**; it's the proof artifact for R3-RC-02.

**Execution note:** Test-first. Write and verify the test fails on master before starting U2.

**Patterns to follow:**
- `tests/test_context_manager.py` already has `test_full_session_recomposition_*` tests that instantiate a real `ContextManager` — use the same setup.
- DO NOT reuse `_FakeManager` from `tests/test_benchmark_ingest_service.py` — its stubs override the splitter and the test would pass vacuously (REVIEW coherence-finding from this plan's own confidence check).

**Test scenarios:**
- Happy path: two-segment fixture (6 + 4 messages, same `event_date`), assert no merged leaf's `msg_range` crosses the boundary at `msg_index=6`.
- Edge case: two-segment fixture with vastly different sizes (1 + 15 messages, same date) — same boundary-respect assertion.
- Edge case: three adjacent segments (4 + 4 + 4 messages, same date) — assert each leaf belongs to exactly one input segment, no leaf spans 2 boundaries.
- Edge case: single-segment input (6 messages) — must still produce one leaf with `msg_range=[0, 5]` (regression guard for the production-path-unchanged invariant).

**Verification:**
- Test runs and **fails** on master with a diff-style assertion message that names the offending leaf URI and its boundary-crossing `msg_range`.

---

- [x] U2. **Add `source_segment_index` to `RecompositionEntry` and populate it on the benchmark path**

**Goal:** Tag every benchmark recomposition entry with its source input-segment index so the splitter can see where boundaries fall. Production-lifecycle and re-derived entries continue to set the field to `None`.

**Requirements:** R3

**Dependencies:** U1 (need the failing test to grade against)

**Files:**
- Modify: `src/opencortex/context/recomposition_types.py` (add `source_segment_index: Optional[int]`; update docstring to call out the three construction sites' new responsibilities)
- Modify: `src/opencortex/context/manager.py` (`_benchmark_recomposition_entries` populates with the input-segment index; `_build_recomposition_entries` and the inline re-derive in `_run_full_session_recomposition` populate with `None`)

**Approach:**
- Add the field to `RecompositionEntry` after `superseded_merged_uris` (preserving existing field order so the diff is local).
- Update the `_benchmark_recomposition_entries` loop: track `segment_index` enumerated from `normalized_segments` and stamp it on every entry derived from that segment.
- Update the production-lifecycle constructor to set `source_segment_index=None`.
- Update the inline re-derive in `_run_full_session_recomposition` to set `source_segment_index=None`.
- No change to `_finalize_recomposition_segment` or any other consumer — they don't read this field.

**Patterns to follow:**
- `recomposition_types.py` already documents the "every field set unconditionally at all three construction sites today" invariant — extend the same pattern.

**Test scenarios:**
- Construction-shape scenarios (no behavior change yet because U3 hasn't enforced the split):
  - Happy path: `_benchmark_recomposition_entries` over a 2-segment input produces entries where the first 6 entries have `source_segment_index=0` and the next 4 have `source_segment_index=1`.
  - Happy path: production-lifecycle and re-derive constructors emit `source_segment_index=None`.
- The U1 failing test should still fail after U2 (the fix isn't applied yet — only the field is added).

**Verification:**
- `RecompositionEntry` has the new field; existing recomposition tests in `tests/test_context_manager.py` still pass (no behavior change yet).
- New construction-shape tests pass.
- The U1 regression test still fails (proving U2 alone doesn't fix the bug).

---

- [x] U3. **Force input-segment boundary split in `_build_recomposition_segments`**

**Goal:** Add the split condition that closes R3-RC-02 / R2-14. After this unit, the U1 regression test passes.

**Requirements:** R1, R2

**Dependencies:** U2 (needs the field on entries)

**Files:**
- Modify: `src/opencortex/context/manager.py` (`_build_recomposition_segments`)

**Approach:**
- In the entry loop, before the existing `should_split` checks, compute `prev_seg = current[-1]["source_segment_index"]` (when `current` is non-empty) and `entry_seg = entry["source_segment_index"]`. If both are non-None and they differ, set `should_split = True`.
- Order: this check should run **before** the time_refs / token / message-count checks. Boundary crossing is a hard split; the soft semantic checks are downstream.
- Production path (where both values are `None`) skips the new check entirely — no behavior change for `_build_recomposition_entries` or the inline re-derive callers.

**Patterns to follow:**
- The existing `should_split` boolean accumulator at the top of the loop. The new condition slots in as the first additional rule.

**Test scenarios:**
- The U1 regression test now **passes**.
- Existing `_full_session_recomposition_preserves_leaves_and_creates_directories` and `_full_session_recomposition_preserves_leaf_uris_unchanged` tests still pass (production path unchanged via `None` sentinel).
- Edge case: benchmark fixture with a single input segment (no boundaries) — splitter behavior identical to pre-fix (size/token/time_refs caps are the only triggers).
- Edge case: benchmark fixture with two segments where each is already over `_SEGMENT_MAX_MESSAGES` — splits happen on the size cap as before; the new boundary check is redundant but harmless.

**Verification:**
- `tests.test_benchmark_ingest_service` — all tests pass including U1's new regression.
- `tests.test_locomo_bench` + `tests.test_beam_bench` + `tests.test_benchmark_ingest_lifecycle` + `tests.test_http_server` — all green.
- `tests.test_context_manager` — existing recomposition tests pass (only the pre-existing `test_update_regenerates_fact_points` failure remains).
- Manual: run a benchmark fixture with adjacent same-date sessions, observe that each merged leaf's `msg_range` fits inside a single input session's span.

---

- [x] U4. **R3-RC-08 — assert `recomposition_stage='benchmark_offline'` in HTTP test**

**Goal:** Lock the contract that benchmark merged leaves carry `recomposition_stage="benchmark_offline"`. Future code paths that filter by `recomposition_stage in {online_tail, final_full}` would silently exclude benchmark records — this assertion makes the regression visible.

**Requirements:** R5

**Dependencies:** None (test-only addition; orthogonal to U1-U3)

**Files:**
- Modify: `tests/test_http_server.py` (`test_04c_benchmark_conversation_ingest_preserves_traceability_contract` around line 868)

**Approach:**
- After the existing `assertEqual(len(ingest_data["records"]), 2)` block, iterate over `ingest_data["records"]` and assert each carries `recomposition_stage == "benchmark_offline"`.
- Use a per-record assertion (one `assertEqual` per record) rather than `assertSetEqual` over all stages so the failure message names the specific record that drifted.

**Patterns to follow:**
- The adjacent assertion `assertNotIn("layer_counts", ingest_data)` (line 875) — same per-record style.

**Test scenarios:**
- Test expectation: assertion passes against current code (the records already carry the field).
- Failure mode covered: if a future change rebrands the stage (e.g., to `"benchmark_recompose"`), the test fails with a clear diff.

**Verification:**
- `tests.test_http_server::test_04c_*` passes with the new assertion.
- Verify the assertion would fail under a hypothetical rebrand by renaming the field as a local off-branch smoke check (revert before commit).

---

- [x] U5. **R3-RC-09 — lock `_merged_leaf_uri` zero-padded URI format**

**Goal:** Add a regression test that locks `_merged_leaf_uri`'s `f"conversation-{hash}-{start:06d}-{end:06d}"` format. The LoCoMo adapter's `sorted(new_records)` relies on lex sort = numeric sort, which only holds with zero padding. A future change to use `{start}-{end}` (no padding) would silently break URI ordering for any session with `>9` merged leaves.

**Requirements:** R6

**Dependencies:** None (test-only; orthogonal to U1-U4)

**Files:**
- Modify: `tests/test_context_manager.py` (add new test in the same class as the existing `_merged_leaf_uri` references, or create `TestMergedLeafUriContract` if no natural class exists)

**Approach:**
- Direct unit test against `ContextManager._merged_leaf_uri(tenant_id, user_id, session_id, msg_range)`.
- Assertions:
  - URI matches regex `r"conversation-[0-9a-f]{12}-\d{6}-\d{6}$"` (path component) — locks the zero-padding length.
  - Sort-order invariant: build URIs for `msg_range=[0,1]`, `[2,9]`, `[10,11]`, `[100,101]`, `[1000,1001]`. Assert `sorted(uris)` matches the numeric order. Without zero-padding, lex sort would put `[10,11]` before `[2,9]` — this is the production bug the lock prevents.
  - Negative example in a comment: document why removing padding would fail this test (and what would silently break in `LoCoMoBench`).
- Use deterministic inputs (fixed tenant/user/session) so the URI hash is stable and the regex match is exact.

**Patterns to follow:**
- The plan-005 / plan-006 style of "lock a load-bearing format with a regex + a sort-order assertion".

**Test scenarios:**
- Happy path: URI shape regex matches.
- Critical: 5-element URI list sorts numerically by `(start, end)`.
- Edge case: `msg_range=[0, 0]` (zero-width) produces `...-000000-000000`.
- Edge case: large indices (`msg_range=[123456, 123457]`) still fit in the 6-digit field.
- Edge case (documented in test comment, not asserted): if `msg_range[0] >= 1_000_000` the format would silently overflow the 6-digit field — out of scope for this PR but flagged.

**Verification:**
- New test passes.
- Format-change verification: as a local off-branch smoke check, edit `_merged_leaf_uri` to drop zero-padding and confirm the test fails. Revert before commit — do NOT push the format change as a separate commit.

---

## System-Wide Impact

- **Interaction graph:** `_benchmark_recomposition_entries` → `_build_recomposition_segments` → `_finalize_recomposition_segment` → `_write_merged_leaves` (in `BenchmarkConversationIngestService`). Only the first two are touched; the finalizer doesn't read `source_segment_index`, and the writer doesn't either.
- **Error propagation:** No new error paths. The split condition is pure data, no I/O.
- **State lifecycle risks:** None. The entries are in-memory only between construction and segmentation.
- **API surface parity:** `RecompositionEntry` is internal to `src/opencortex/context/`; `recomposition_types.py` is consumed only by `manager.py`. No external callers.
- **Integration coverage:** `tests.test_benchmark_ingest_service` (U1 new test) covers the splitter via the service. `tests.test_locomo_bench` and `tests.test_benchmark_ingest_lifecycle` provide downstream coverage that the LoCoMo adapter's URI mapping is unchanged for single-segment inputs and improved for multi-segment ones (no test changes — behavior is byte-identical for single-segment, sharper for multi-segment).
- **Unchanged invariants:**
  - `_merged_leaf_uri` format (locked by U5, not modified)
  - LoCoMo / LongMemEval adapter call shape (`cm.map_session_uris(return_all=True/False)`)
  - Production conversation-lifecycle splitter behavior (R2 verification gate)
  - Segment cap constants `_SEGMENT_MAX_MESSAGES` / `_SEGMENT_MAX_TOKENS` / `_SEGMENT_MIN_MESSAGES`

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| The `source_segment_index=None` sentinel accidentally fires the split when one entry is benchmark-tagged and an adjacent one isn't (mixed mode). | The split condition explicitly requires **both** values to be non-None before comparing. Production-lifecycle and benchmark entries never appear in the same `entries` list (different code paths construct different lists), so mixed mode is impossible by design. The check is defensive belt-and-suspenders. |
| Adding a TypedDict field is a structural change that mypy / pyright will flag at every construction site. | The plan explicitly enumerates the three construction sites (per `recomposition_types.py` docstring). U2 updates all three. Type checker happy. |
| Test built against `_FakeManager` from PR #7 would pass vacuously because the fake stubs `_benchmark_recomposition_entries` and `_build_recomposition_segments` with simplified pass-throughs (no real splitting logic, no `RecompositionEntry` construction). | U1 explicitly avoids `tests/test_benchmark_ingest_service.py` and places the test in `tests/test_context_manager.py` (or a dedicated file) against the real `ContextManager`. See U1 Files + Approach. This was caught by the plan's own coherence review pass and the U1 instructions are explicit about the constraint. |
| Production lifecycle tests pass but production *behavior* drifts (e.g., a subtle recomposition difference under a multi-message live session). | R2 is a hard gate: `tests.test_e2e_phase1` and `tests.test_context_manager` (production lifecycle) MUST pass with no test changes. Run them after U3. The `None` sentinel makes the new check a strict no-op for production. |
| R3-RC-09's URI format lock is too strict and breaks if someone legitimately needs to change the format (e.g., for a 7-digit msg_index world). | The test failure surfaces the format change, which is the point. A future migration that bumps the digit count would update the test alongside the code change. The test does not prevent format evolution; it makes it intentional. |

---

## Documentation / Operational Notes

- Update `docs/residual-review-findings/2026-04-24-review-closure-tracker.md` after merge: flip R3-RC-02, R2-14, R3-RC-08, R3-RC-09 from ❌ to ✅ with the closing commit hash. Move the action-queue entry "R3-RC-02 + R2-14" off the queue.
- After landing, this PR is a strong `/ce-compound` capture candidate: "Splitter that crosses input boundaries because msg_index was global → tag entries with source segment, hard-split on boundary change. Pattern applies to any chunker that re-segments pre-grouped input."
- No CLAUDE.md update needed — `recomposition_types.py` was added in plan 005 and is already documented in CLAUDE.md (PR #7's autofix added the `context/` block).
- No migration / rollout notes — pure correctness fix, no schema change, no behavior change for production.

---

## Sources & References

- **Closure tracker:** [docs/residual-review-findings/2026-04-24-review-closure-tracker.md](../../docs/residual-review-findings/2026-04-24-review-closure-tracker.md) — R2-14 (line 164), R3-RC-02 / R3-RC-08 / R3-RC-09 (Round 3 recall-correctness section), action queue tier 2 #3 (line 367)
- **CE review:** `.context/compound-engineering/ce-code-review/20260424-152926-6301c860/REVIEW.md` — R3-RC-02 (confidence 75, P1 apply)
- **Source code:** `src/opencortex/context/manager.py` (`_benchmark_recomposition_entries` line 1584, `_build_recomposition_segments` line 1979, `_merged_leaf_uri` line 1092), `src/opencortex/context/recomposition_types.py`, `src/opencortex/context/benchmark_ingest_service.py`
- **Tests:** `tests/test_benchmark_ingest_service.py`, `tests/test_http_server.py`, `tests/test_context_manager.py`, `tests/test_locomo_bench.py`, `tests/test_benchmark_ingest_lifecycle.py`
- **Prior PR closures:** PR #7 (plan 005) extracted the service + repo + DTO; PR #8 (plan 006) extracted `cm.map_session_uris` with `return_all` kwarg — both consumers of the splitter behavior this PR fixes
