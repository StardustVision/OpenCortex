---
title: "refactor: Close P2 residuals on benchmark offline ingest path"
type: refactor
status: active
date: 2026-04-25
---

# refactor: Close P2 residuals on benchmark offline ingest path

## Overview

PRs #3 and #4 closed every P0 and P1 finding from the prior 4-round CE review of `feat/benchmark-offline-conv-ingest`. Five P2 items and one cheap P3 (`ADV-006`) remain on the residual sheet (`docs/residual-review-findings/feat-benchmark-offline-conv-ingest.md`). This plan closes them in a single PR — none requires architectural change, the touched surfaces are well-known from the prior work, and bundling avoids review thrash on six trivially-related fixes.

---

## Problem Frame

Six independent residuals on the merged benchmark ingest path. None blocks production use; together they tighten contracts, eliminate cross-codebase inconsistencies, and remove one real correctness footgun (`ADV-006` false 409s on benign list reordering). All cited code paths exist on master at HEAD `90eb069` after PRs #3/#4 merged.

Source documents:
- Prior CE review: `.context/compound-engineering/ce-code-review/20260424-152926-6301c860/REVIEW.md`
- Follow-up review (after PR #3 work): `.context/compound-engineering/ce-code-review/20260425-093102-b5a311de/REVIEW.md`
- Residual handoff (canonical list): `docs/residual-review-findings/feat-benchmark-offline-conv-ingest.md`

---

## Requirements Trace

- **R1 (api-contract-001):** A request to the legacy URL `/api/v1/benchmark/conversation_ingest` returns a 410 Gone with a `Location`/`detail` pointing to the new admin URL, and the change is documented in a CHANGELOG entry.
- **R2 (api-contract-004):** All admin route error responses use a single envelope shape — either FastAPI's default `{"detail": ...}` or `{"error": ...}` — consistently across `_require_admin`, payload validation, timeout, conflict, and bench-collection paths.
- **R3 (KP-01):** The Pydantic `field_validator` on `BenchmarkConversationMessage.meta` uses `orjson` (project convention) instead of stdlib `json` for serialization.
- **R4 (KP-06):** The `benchmark_ingest_conversation` response builder no longer carries a 3-level lookup chain (in-memory map → fallback FS hydration → empty string). Hydration is unified in one helper that takes the merged record set and returns a complete URI→content map.
- **R5 (KP-08 / R2-15):** Recomposition entries are typed via a `RecompositionEntry` TypedDict. All three construction sites (`_build_recomposition_entries`, `_benchmark_recomposition_entries`, the inline builder inside `_run_full_session_recomposition`) produce that type, and consumers narrow against it.
- **R6 (ADV-006):** `_hash_transcript` produces the same digest for two transcripts that differ only in list-element ordering of meta values (e.g. `time_refs`), so benign replays do not return false 409s.

---

## Scope Boundaries

- Do not modify production conversation lifecycle (`context_commit`, `context_end`).
- Do not introduce a 12th MCP tool. Benchmark route stays admin-only HTTP.
- Do not change benchmark scoring methodology.
- Do not touch `docs/solutions/`, `docs/brainstorms/`, or other CE pipeline artifacts.
- Do not bundle the still-deferred U12 `add_batch` work — that needs its own plan.

### Deferred to Follow-Up Work

- Phase 2 behavior-preserving refactor (extract `BenchmarkConversationIngestService`, route relocation polish, observability via `StageTimingCollector`) — too big for this PR; warrants its own plan.
- The remaining nice-to-have residuals not in this plan (F3/ADV-003 cross-conv shared state non-determinism, KP-02/03/04/05/07 Pythonic style nits, M1-M4 maintainability, TEST-001/002/005/008 coverage gaps) — separate polish PR.

---

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/http/server.py` (post-PR-#3 state) — old benchmark route was *removed* in U1, so today a request to `/api/v1/benchmark/conversation_ingest` hits FastAPI's default 404 handler with no body context. The 410 shim has to be added back as an explicit route.
- `src/opencortex/http/admin_routes.py:240-289` — current admin benchmark handler (post-PR-#4); error envelopes today: 403 = string `detail`, 504 = string `detail`, 409 = dict `detail`. Pre-existing bench-collection routes at `:200, :213` use `JSONResponse({"error": ...}, status_code=400)` — a *third* style.
- `src/opencortex/http/models.py:316-331` — `BenchmarkConversationMessage._meta_within_byte_budget` uses stdlib `json.dumps(value, ensure_ascii=False, sort_keys=True)`. Project memory (`MEMORY.md`) records `orjson migration across codebase` completed 2026-03-22+. Other places in the same module already import orjson.
- `src/opencortex/context/manager.py:1150-1163` — `_hash_transcript` uses `orjson.dumps(normalized, option=orjson.OPT_SORT_KEYS)`. `OPT_SORT_KEYS` recurses into nested dicts but leaves list ordering unchanged — confirmed by the adversarial reviewer in the follow-up run.
- `src/opencortex/context/manager.py:1857-1893` — current 3-level hydration lookup (`merged_content_by_uri.get(uri, fallback_hydrated.get(uri, ""))`).
- `src/opencortex/context/manager.py:_benchmark_recomposition_entries`, `_build_recomposition_entries`, and the inline builder inside `_run_full_session_recomposition` — three call sites all returning `Dict[str, Any]` entries with the same shape (text, uri, msg_start, msg_end, token_count, anchor_terms, time_refs, source_record, immediate_uris, superseded_merged_uris).

### Institutional Learnings

- `docs/solutions/best-practices/single-bucket-scoped-probe-2026-04-16.md` — already cited in plan 003. Still informs U2 (error envelope consistency belongs to the same "narrow contract surface" discipline).
- `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md` — same. The TypedDict change in U5 is a small instance of "make the contract typecheckable so drift hurts at compile time."
- No existing entry for **error envelope conventions**, **transcript hashing canonical-form rules**, or **TypedDict rollout patterns**. After merge, U2 and U6 are reasonable `/ce-compound` candidates if the team finds them useful.

### External References

External research skipped — every change has multiple direct local examples to mirror (orjson usage, Pydantic field_validator, `JSONResponse` patterns, TypedDict patterns elsewhere in the codebase).

---

## Key Technical Decisions

- **Error envelope choice (R2): standardize on FastAPI's `{"detail": ...}` for all admin routes.** The two existing string-detail responses (403/504) and the one dict-detail response (409) all flow through `HTTPException`, which already wraps the value as `{"detail": ...}`. The pre-existing bench-collection `JSONResponse({"error": ...})` calls become `raise HTTPException(status_code=400, detail=...)` for symmetry. Keeping the dict-detail shape on 409 (with `existing_hash`/`supplied_hash`) is fine — `detail` accepts strings or dicts. Choosing FastAPI's default avoids inventing a custom envelope class that would itself become the "fourth style".
- **410 Gone shim (R1): explicit route, not a middleware.** Adding a single route for `/api/v1/benchmark/conversation_ingest` (all methods) returning 410 with a `detail` and `Location` header is more discoverable than middleware-based path interception, and lets us drop the route after a release or two.
- **Hydration helper signature (R4):** unify on one async helper `_hydrate_record_contents_with_overrides(records, overrides_by_uri)` that returns a `dict[uri, content]`. Records whose URI is in `overrides` skip the FS read. The benchmark caller passes its captured in-memory map as `overrides`. Direct evidence path passes its own map. The 3-level lookup at the comprehension site collapses to a single `hydrated.get(uri, "")`.
- **TypedDict shape (R5):** `RecompositionEntry` carries the union of fields all three call sites currently produce — explicit `Required[...]` for fields every site sets, `NotRequired[...]` for the optional ones (`immediate_uris`, `superseded_merged_uris` are unconditional empty lists in two of the three sites; mark as `Required` so the compiler catches future drift). Define in `src/opencortex/context/recomposition_types.py` so it can be imported without circular deps.
- **Hash canonicalization (R6):** sort lists recursively during the normalization pass before serializing. Implemented as a small recursive helper `_canonicalize_for_hash(value)` that sorts lists of primitives in-place but leaves lists-of-dicts untouched (their order may be semantic — message ordering matters). Apply only to meta values, not to the transcript message list itself (message order IS semantic).

---

## Open Questions

### Resolved During Planning

- **Should the 410 shim include a CORS preflight handler?** No. Admin endpoints don't need CORS; the legacy URL was admin-only by intent.
- **Should we also normalize the bench-collection error envelopes (pre-existing, not in this plan's residual set)?** Yes — they're in the same file and the inconsistency is what R2 explicitly calls out. The fix is a few lines.
- **Should `_canonicalize_for_hash` sort lists of dicts?** No. Lists of dicts in benchmark meta are uncommon, and order can be load-bearing (e.g., `tool_calls` sequence). Keeping the helper conservative avoids changing hash semantics for cases not flagged by ADV-006.

### Deferred to Implementation

- Exact `RecompositionEntry` field set — pinned by the 3 construction sites. May discover one site sets a field the others don't; surface as part of unit U5.
- Precise wording of the 410 detail string and CHANGELOG entry — short prose, not architectural.

---

## Implementation Units

- [ ] U1. **410 Gone shim + CHANGELOG for legacy benchmark URL**

**Goal:** Requests to `/api/v1/benchmark/conversation_ingest` (all methods) return 410 with a structured detail pointing at the new admin URL. Document the URL change in `CHANGELOG.md` (or create one if absent).

**Requirements:** R1.

**Dependencies:** None.

**Files:**
- Modify: `src/opencortex/http/server.py` (add a single route that raises `HTTPException(status_code=410, detail={...})` for the legacy path; mount before fall-through 404)
- Modify (or create): `CHANGELOG.md` — note the URL change with the deprecation date and target replacement
- Test: `tests/test_http_server.py`

**Approach:**
- Register the legacy path as `@app.api_route(legacy_url, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])` so any verb gets 410 (not just POST). Body: `{"detail": {"reason": "moved", "new_url": "/api/v1/admin/benchmark/conversation_ingest", "removed_in": "0.8.0"}}`.
- CHANGELOG entry under `## Unreleased` (or whatever convention exists if the file is already there).

**Patterns to follow:**
- Existing FastAPI route registrations in `src/opencortex/http/server.py:_register_routes`.

**Test scenarios:**
- Happy path: `POST /api/v1/benchmark/conversation_ingest` returns 410, response JSON has `detail.new_url == "/api/v1/admin/benchmark/conversation_ingest"`.
- Edge case: `GET` and `OPTIONS` to the same legacy path also return 410 (verbs don't matter for a removed endpoint).
- Edge case: the new admin URL is unaffected — `POST /api/v1/admin/benchmark/conversation_ingest` with admin role still returns 200 (regression lock).

**Verification:**
- 410 surfaces consistently for the old URL; no other admin/business route is shadowed.

---

- [ ] U2. **Standardize admin route error envelopes**

**Goal:** Every admin route error path returns FastAPI's standard `{"detail": ...}` envelope. The pre-existing `JSONResponse({"error": ...})` returns in `bench-collections` are converted to `raise HTTPException(status_code=400, detail=...)`.

**Requirements:** R2.

**Dependencies:** None (independent of U1; can land in either order).

**Files:**
- Modify: `src/opencortex/http/admin_routes.py` (change `JSONResponse({"error": ...})` to `HTTPException` raises in `create_bench_collection` and `delete_bench_collection`)
- Test: `tests/test_http_server.py` (add envelope-shape assertions for 400/403/409/504)

**Approach:**
- Replace each `return JSONResponse({"error": "..."}, status_code=400)` with `raise HTTPException(status_code=400, detail="...")`. Verify response shape stays `{"detail": "..."}` (FastAPI's default exception handler).
- The 409 path's existing dict `detail` (`{"reason": ..., "session_id": ..., ...}`) is fine — `HTTPException.detail` is `Any`. No change there.
- Leave `_require_admin()` (403, string detail) and the 504 timeout (string detail) unchanged — they already use the standard envelope.

**Patterns to follow:**
- Existing `HTTPException` raises in `admin_routes.py` (`_require_admin`, the 504 in `admin_benchmark_conversation_ingest`).

**Test scenarios:**
- Happy path: `POST /api/v1/admin/collection` with non-admin → 403, body has `detail` key (string).
- Error path: `POST /api/v1/admin/collection` with bad name → 400, body has `detail` key (string), no `error` key.
- Error path: `DELETE /api/v1/admin/collection/foo` with non-`bench_` name → 400, same envelope.
- Regression: 409 hash-mismatch still surfaces `detail.existing_hash` and `detail.supplied_hash` (dict detail).

**Verification:**
- `grep "JSONResponse" src/opencortex/http/admin_routes.py` shows zero hits inside route handlers.

---

- [ ] U3. **Switch `models.py` field_validator to orjson**

**Goal:** `BenchmarkConversationMessage._meta_within_byte_budget` uses `orjson.dumps` instead of stdlib `json.dumps`, matching the project-wide convention recorded in MEMORY.md.

**Requirements:** R3.

**Dependencies:** None.

**Files:**
- Modify: `src/opencortex/http/models.py`
- Test: `tests/test_http_server.py` (existing payload-bounds test in `test_04g` already exercises the validator; add one assertion that a CJK-heavy meta value still passes the byte-budget check correctly under orjson, since `ensure_ascii=False` was the stdlib quirk being relied on)

**Approach:**
- Drop `import json` at module top (or keep if used elsewhere in models.py — verify by grep).
- Replace `json.dumps(value, ensure_ascii=False, sort_keys=True)` with `orjson.dumps(value, option=orjson.OPT_SORT_KEYS)`. orjson always emits non-ASCII as UTF-8 bytes (no `ensure_ascii` flag needed) and supports `OPT_SORT_KEYS`.
- `len(serialized)` becomes `len(serialized)` directly — orjson returns bytes already, so the existing `.encode("utf-8")` cast is redundant; drop it.

**Patterns to follow:**
- Other orjson usage in the codebase, e.g. `src/opencortex/context/manager.py` `import orjson as json`.

**Test scenarios:**
- Edge case: meta dict with CJK characters near the 16 KB limit still validates correctly (the orjson-encoded bytes count, not the Python string length).
- Regression: existing `test_04g_benchmark_ingest_payload_bounds` passes (oversized meta still rejected with 422).

**Verification:**
- No `json.dumps` call remains in `src/opencortex/http/models.py`.

---

- [ ] U4. **Unify hydration lookup in benchmark response builder**

**Goal:** Replace the 3-level `merged_content_by_uri.get(uri, fallback_hydrated.get(uri, ""))` lookup chain with a single helper call that returns a complete `dict[uri, content]` for all records being exported, consuming the in-memory write-time captures as overrides.

**Requirements:** R4.

**Dependencies:** None.

**Files:**
- Modify: `src/opencortex/context/manager.py` (`_hydrate_record_contents` extension OR new `_hydrate_record_contents_with_overrides`; collapse the two response-build comprehensions in `benchmark_ingest_conversation` and `_benchmark_ingest_direct_evidence`)
- Test: `tests/test_benchmark_ingest_lifecycle.py` (existing tests cover content-non-empty assertion; no new test needed unless behavior changes — verify with regression run)

**Approach:**
- Extend the existing helper signature: `_hydrate_record_contents(records, overrides=None)`. When `overrides` is provided, URIs found there short-circuit (no FS read), URIs not in overrides go through the existing FS read path, and the returned dict contains every URI from `records`.
- The benchmark merged_recompose path passes `overrides=merged_content_by_uri`. Direct evidence path passes `overrides=evidence_content_by_uri`. The comprehension collapses to `_export_memory_record(rec, hydrated_content=hydrated.get(uri, ""))`.
- Keep `_export_memory_record`'s `hydrated_content` kwarg unchanged — the simplification is at the call site, not the API.

**Patterns to follow:**
- Existing `_hydrate_record_contents` shape (returns `dict[str, str]`, missing keys map to "").

**Test scenarios:**
- Happy path: merged_recompose response records all have non-empty `content` (existing test_04d assertion).
- Happy path: direct_evidence response records have non-empty `content` (existing test_04e assertion).
- Edge case: records whose URI is in `overrides` do not trigger a FS read (use a write-counter mock or assert via internal log).
- Regression: empty record set → empty dict, no FS calls.

**Verification:**
- Both response builders use the same helper call shape; the 3-level fallback chain is gone.

---

- [ ] U5. **Introduce `RecompositionEntry` TypedDict**

**Goal:** All three recomposition-entry construction sites produce a `RecompositionEntry` TypedDict instead of bare `Dict[str, Any]`. Consumers (`_build_anchor_clustered_segments`, `_finalize_recomposition_segment`, the directory derive loop) annotate against it.

**Requirements:** R5.

**Dependencies:** None — this is a pure type-shape change with no behavioral implications.

**Files:**
- Create: `src/opencortex/context/recomposition_types.py` (defines `RecompositionEntry` TypedDict)
- Modify: `src/opencortex/context/manager.py` (annotate `_benchmark_recomposition_entries`, `_build_recomposition_entries`, the inline builder inside `_run_full_session_recomposition`, and consumer signatures)
- Test: `tests/test_benchmark_ingest_lifecycle.py` AND `tests/test_context_manager.py` — no behavioral change, but a static type-check pass is the real verification

**Approach:**
- New module `recomposition_types.py` to avoid pulling `manager.py` into a typing-only import cycle.
- Fields based on actual current shape: `text: Required[str]`, `uri: Required[str]`, `msg_start: Required[int]`, `msg_end: Required[int]`, `token_count: Required[int]`, `anchor_terms: Required[Set[str]]`, `time_refs: Required[Set[str]]`, `source_record: Required[Dict[str, Any]]`, `immediate_uris: Required[List[str]]`, `superseded_merged_uris: Required[List[str]]`. All currently set unconditionally at all three sites — if implementation reveals one site omits a field, mark `NotRequired` and document why.
- Add `from __future__ import annotations` at the top of `manager.py` if not already present (avoids runtime evaluation cost for the TypedDict reference).
- This is not a behavior change — pure type annotation. No new tests needed; existing tests stay green.

**Execution note:** Run `mypy --strict src/opencortex/context/manager.py` (or whatever the project type-check incantation is) after the change to confirm the TypedDict catches drift. If the project doesn't run mypy, surface that in the verification.

**Patterns to follow:**
- Other TypedDicts in the codebase if any exist; otherwise the standard PEP 589 form.

**Test scenarios:**
- Test expectation: none -- pure type-shape change with no runtime behavior. Verification is via existing test suite remaining green plus, when available, a mypy / pyright pass.

**Verification:**
- All three construction sites import and return `RecompositionEntry`.
- Consumers annotate parameter types accordingly.
- Existing test suite (95+ tests) stays green.

---

- [ ] U6. **Sort lists in `_hash_transcript` to canonicalize benign reordering**

**Goal:** `_hash_transcript` produces the same digest for two transcripts that differ only in the ordering of list values inside meta (e.g., `time_refs: ["2023-05-01", "9 am on 1 May"]` vs `["9 am on 1 May", "2023-05-01"]`). Eliminates false 409 conflicts on benign benchmark replays.

**Requirements:** R6.

**Dependencies:** None.

**Files:**
- Modify: `src/opencortex/context/manager.py` (`_hash_transcript` + new `_canonicalize_for_hash` helper)
- Test: `tests/test_benchmark_ingest_lifecycle.py` (new test asserting hash equality for time_refs reordering; new test asserting hash inequality when message text differs)

**Approach:**
- Add module-level `_canonicalize_for_hash(value)` that:
  - For `dict`: recurse on values.
  - For `list`: if every element is a primitive (str / int / float / bool / None), return `sorted(value)`. Otherwise (list of dicts) leave order intact — order may be load-bearing for `tool_calls`-style sequences.
  - For everything else: return as-is.
- Apply to each message's `meta` field during normalization. Do NOT apply to the message list itself — message order is semantic.
- Keep `OPT_SORT_KEYS` for dict-key ordering (already there).

**Patterns to follow:**
- Existing `_hash_transcript` structure.

**Test scenarios:**
- Happy path: same transcript with `time_refs` list reordered → same hash → idempotent re-ingest (200, no 409).
- Edge case: meta with nested dict values reorders dict keys → same hash (already covered by OPT_SORT_KEYS, regression lock).
- Edge case: meta with `tool_calls` list (list of dicts) — order preserved, hash differs if order differs (correct: tool_call order is semantic).
- Error path: same `session_id` with genuinely different message content → still 409 (existing test_409_on_same_session_different_transcript stays green).

**Verification:**
- New regression test demonstrates the false-409 case is fixed; the genuine-409 case still fires.

---

## System-Wide Impact

- **Interaction graph:** U1 adds a route to the business-routes app (parallel to admin routes — affects only the legacy URL). U2 changes response shape for a few error paths. U3 is internal validator only. U4-U5 are internal refactors. U6 changes a hash function used only by the benchmark idempotent-hit detector.
- **Error propagation:** U2 unifies envelopes — adapter / client code that reads `response.json()["detail"]` continues to work; any code reading `response.json()["error"]` for bench-collections breaks (none currently exists).
- **State lifecycle risks:** U6 changes hash semantics — a previously-stored source record's transcript_hash will not match a re-canonicalized fresh one. **Mitigation:** the F5 `run_complete` marker AND the hash check are both required for idempotent-hit. A pre-existing source record from before this change will not match the new canonical hash, so the next ingest will treat it as a hash mismatch (409). This is a single-time event per session_id and can be cleared by deleting the source record. Consider noting in CHANGELOG.
- **API surface parity:** U1 publicly documents the URL move. No other interface (MCP, CLI) needs an update — they all go through `OCClient` which already targets the new URL.
- **Integration coverage:** The lifecycle test suite (`tests/test_benchmark_ingest_lifecycle.py`) covers the AR3-AR7 contracts. New tests in U1, U2, U6 add coverage for the changed shapes. U4-U5 are pure refactors covered by existing tests.
- **Unchanged invariants:** Production conversation lifecycle (`context_commit`, `context_end`); MCP tool surface; benchmark scoring methodology; admin gate enforcement; cleanup tracker compensation order.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| **U6 hash change invalidates existing source records.** Sessions ingested before this PR will return 409 on first re-ingest after deploy. | Document in CHANGELOG. The 409 is recoverable: delete the source record OR rotate `session_id`. Acceptable cost; the false-409 fix is a correctness improvement, and benchmark sessions are short-lived. |
| **U5 TypedDict reveals shape drift the current codebase tolerates.** One construction site might omit a field that consumers expect. | The plan's verification step requires existing tests to stay green; a runtime AttributeError would fail `tests.test_e2e_phase1`. Use `Required[...]` for fields all sites set today; downgrade to `NotRequired` only after evidence. |
| **U2 envelope normalization is observable to any HTTP client reading `response.json()["error"]`.** | Internal grep shows no consumer reads `error`-key responses from bench-collections; if one is found in implementation, document and convert it. |
| **U4 helper change accidentally regresses content hydration on records outside the override map.** | Existing test_04d/04e assertions on non-empty `content` cover the regression. |
| **U1 410 route accidentally shadows the admin route.** | The legacy URL `/api/v1/benchmark/conversation_ingest` and the admin URL `/api/v1/admin/benchmark/conversation_ingest` have different prefixes; route matching is path-exact in FastAPI. Add a regression test that the admin URL still returns 200. |

---

## Documentation / Operational Notes

- **CHANGELOG entry** (created or updated by U1): note the URL move with deprecation date, the 410 shim removal target, and the U6 hash-canonicalization side effect that may produce one-time 409s for sessions ingested before this release.
- **No infrastructure changes.** No new env vars, no migration scripts, no deployment ordering constraints.

---

## Sources & References

- **Prior REVIEW.md:** `.context/compound-engineering/ce-code-review/20260424-152926-6301c860/REVIEW.md`
- **Follow-up REVIEW.md:** `.context/compound-engineering/ce-code-review/20260425-093102-b5a311de/REVIEW.md`
- **Residual handoff:** `docs/residual-review-findings/feat-benchmark-offline-conv-ingest.md`
- **Phase 1 plan (template followed by this plan):** `docs/plans/2026-04-25-003-fix-benchmark-conversation-ingest-review-fixes-plan.md`
- **Related PRs:** #3 (Phase 1 + must-address residuals), #4 (should-address residuals)
- **Branch base:** `master @ 90eb069` (after PR #4 merge)
