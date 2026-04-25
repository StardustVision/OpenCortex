---
title: "perf: Push source_uri into Qdrant filter for SessionRecordsRepository (PERF-02)"
type: perf
status: active
date: 2026-04-25
---

# perf: Push source_uri into Qdrant filter for SessionRecordsRepository (PERF-02)

## Overview

Replace the in-memory `meta.source_uri` post-filter in `SessionRecordsRepository` with a server-side Qdrant filter on the nested `meta.source_uri` payload key. Add a `meta.source_uri` payload index to the context collection so the filter is fast at scale, and add an `ensure_scalar_indexes()` startup hook so existing deployments pick up the new index without recreating collections.

The investigation during planning collapsed the original schema-change concern: Qdrant's `FieldCondition.key` accepts dot-path strings for nested payload fields, and the codebase already uses this pattern at `src/opencortex/orchestrator.py:3512` (`meta.superseded`) and `src/opencortex/intent/probe.py:348` (`meta.layer`). No top-level field lift is needed. No backfill is needed — existing records already have `meta.source_uri`; the new server-side filter works on them as-is.

Closes REVIEW closure tracker entry **PERF-02** from `docs/residual-review-findings/refactor-server-side-design-patterns.md`.

---

## Problem Frame

`SessionRecordsRepository.load_merged` / `load_directories` / `load_layers` (in `src/opencortex/context/session_records.py`) all call `_scroll_all` with a server-side filter that includes `session_id` (and since PR #7 U2, optionally `source_tenant_id` + `source_user_id`). They then iterate the returned records and discard non-matching `meta.source_uri` in Python:

```python
if source_uri:
    if str(meta.get("source_uri", "") or "") != source_uri:
        continue
```

This means every call fetches **all** records for the session_id and throws away non-matching ones in Python. The waste is invisible when each `session_id` only ever has records from one source — but real workloads accumulate multi-source records:

- **Torn-run replay**: `_purge_torn_benchmark_run` deletes records from a failed prior run, but if any are missed, they remain attached to the same `session_id` with their original `source_uri`. Subsequent loads scan them every time.
- **Manual re-ingest**: an operator triggers benchmark ingest twice with the same `session_id` but a slightly different transcript; SourceConflictError fires on production paths but advisory paths may still create stale records.
- **High-cardinality benchmark sessions**: LoCoMo runs many sessions back-to-back; the `_session_records` repository is hit on every `_generate_session_summary` (PERF-01 already collapsed two scans to one) and every `_run_full_session_recomposition`. Each scan returns more than needed when sources are mixed.

The fix: push `source_uri` into the storage filter so Qdrant returns only matching records, and add an index so the filter is O(log n) instead of O(n).

---

## Requirements Trace

- **R1.** `SessionRecordsRepository._build_session_filter` MUST include a `meta.source_uri` cond when `source_uri` is non-None and non-empty. The cond uses the same `op="must"` shape the existing `session_id` / `source_tenant_id` / `source_user_id` conds use.
- **R2.** When `source_uri` is None or empty string, the filter shape MUST stay identical to today (no source_uri cond pushed). Preserves the legacy fallback for callers that explicitly want session-wide scope (acknowledged in R3-RC-03 as a separate open concern; not the target of this PR).
- **R3.** The in-memory `meta.source_uri` post-filter in `load_merged`, `load_directories`, and `load_layers` MUST be removed. The server-side filter is exact for any record with `meta.source_uri`; redundant in-memory passes add no value and dilute the perf win.
- **R4.** `meta.source_uri` MUST be added to the context collection's `ScalarIndex` list in `src/opencortex/storage/collection_schemas.py`. New collections get the payload index automatically via `create_collection`.
- **R5.** Existing Qdrant collections MUST receive the new index via a startup hook. Add `QdrantStorageAdapter.ensure_scalar_indexes()` (analogous to the existing `ensure_text_indexes()`) that iterates schema-declared `ScalarIndex` fields and idempotently creates any missing payload indexes. Wire it into the orchestrator init path.
- **R6.** Behavior parity: existing test suites (`tests.test_session_records_repository`, `tests.test_benchmark_ingest_lifecycle`, `tests.test_benchmark_ingest_service`, `tests.test_e2e_phase1`, `tests.test_context_manager`, `tests.test_locomo_bench`) MUST pass without test modification. The behavior contract is byte-identical for any caller that uses the `source_uri` kwarg correctly.
- **R7.** New repository test asserts the filter dict shape includes the `meta.source_uri` cond when `source_uri` is provided, and asserts the in-memory pass is no longer applied (e.g., a fixture where the storage filter returns a record with mismatched `meta.source_uri` proves the repository now trusts the server-side filter).

---

## Scope Boundaries

- **No top-level `source_uri` payload field.** Investigation found Qdrant's nested-field filter on `meta.source_uri` is sufficient; lifting to top-level would require backfill across every existing context record without a corresponding perf benefit.
- **No backfill migration.** Existing records already have `meta.source_uri`; the new filter works on them as-is. The new index is created idempotently on existing collections via the startup hook (R5).
- **No change to filter DSL grammar.** The repository keeps using the same `{"op": "must", "field": "X", "conds": [val]}` shape. The new cond is a `meta.source_uri` field name — supported by the existing translator.
- **R3-RC-03** (`source_uri=None` degrades to session-wide scan) is **out of scope.** That's a separate concern about whether the legacy fallback is intentional. This PR preserves it.
- **No removal of the `_session_records` repository's tenant/user scope kwargs.** PR #7 U2 added them; they stay.
- **No new collection schema fields.** Only the `ScalarIndex` list grows; `Fields` block is unchanged.
- **No changes to `ensure_text_indexes()`.** R5 adds a sibling method, doesn't fold scalar indexes into the text-index path. The two index types have different `field_schema` arguments and different rationale — keep them separate.
- **No changes to `meta.source_uri` write sites.** All 12 writer sites already set the field correctly. Verified during planning.

---

## Context & Research

### Relevant Code and Patterns

- **`src/opencortex/context/session_records.py`** — `_build_session_filter` (line ~149): the central filter constructor, takes `tenant_id` / `user_id` / `session_id` and emits the `{"op": "and", "conds": [...]}` filter dict. New `source_uri` push-down lands here.
- **`src/opencortex/context/session_records.py`** — `load_merged` / `load_directories` / `load_layers` / `layer_counts`: the four public methods that invoke `_scroll_all`. Three of them have the in-memory `source_uri` post-filter that R3 removes; `layer_counts` doesn't take a `source_uri` parameter today and stays unchanged.
- **`src/opencortex/storage/qdrant/filter_translator.py`** — `translate_filter` and `_must_condition`: the translator that converts the DSL into Qdrant's `Filter`/`FieldCondition` models. `FieldCondition.key` accepts dot-path strings (verified by existing usage of `meta.layer`, `meta.superseded`).
- **`src/opencortex/storage/collection_schemas.py`** — `context_collection` schema (line ~30): the `ScalarIndex` list this PR extends. Other collections (trace, knowledge, etc.) are out of scope.
- **`src/opencortex/storage/qdrant/adapter.py`** — `create_collection` (line ~124) creates payload indexes for `schema["ScalarIndex"]` at collection creation. `ensure_text_indexes` (line ~90) is the migration pattern for existing collections — sibling `ensure_scalar_indexes` follows the same shape.
- **`src/opencortex/storage/qdrant/adapter.py`** — `_infer_payload_type` (line ~868): returns `PayloadSchemaType.KEYWORD` for unknown field names. `meta.source_uri` is not declared in the schema's `Fields` block (it's nested), so it falls through to KEYWORD — exactly what we want for exact-match URI filter.
- **`src/opencortex/orchestrator.py`** — `init` method: where the new `ensure_scalar_indexes()` call wires in, alongside the existing `ensure_text_indexes()` call.
- **`tests/test_session_records_repository.py`** — exists, tests the repository in isolation with mock storage. New tests for source_uri pushdown land here.

### Institutional Learnings

- **PR #7 / U2** (`refactor(context): scope discipline + cursor pagination + overflow guard`) — added `source_tenant_id` / `source_user_id` push-down to `_build_session_filter`. This PR follows the exact same pattern for `source_uri`.
- **PR #9 / PERF-01** — `_generate_session_summary` already paid down its cost via `load_layers` (single scroll). The remaining waste was the in-memory source_uri filter — the residual this PR closes.
- **REVIEW closure tracker entry** at `docs/residual-review-findings/refactor-server-side-design-patterns.md` (search "PERF-02") — the source-of-truth description and confidence rating.
- **Qdrant nested field syntax** — `FieldCondition(key="meta.source_uri", match=MatchValue(value="..."))` is well-supported by Qdrant. The codebase already does this with `meta.superseded` and `meta.layer`. No external research needed.

### External References

None — pure repository-internal change with strong local patterns to follow.

---

## Key Technical Decisions

- **Filter on `meta.source_uri` directly, not a lifted top-level field.** Investigation found Qdrant supports nested-field filters via dot-path; lifting to top-level would need backfill across every existing context record for no perf gain. The dot-path approach reuses the existing schema and the existing data — minimum blast radius.
- **Add `ensure_scalar_indexes()` as a sibling to `ensure_text_indexes()`, not a fold.** The two methods have different `field_schema` arguments (`TextIndexParams` vs `PayloadSchemaType`) and different mental models (full-text search vs exact-match indexed lookup). Keeping them separate makes each method's contract obvious. The orchestrator init path calls both.
- **Drop the in-memory post-filter (R3) rather than keep it as defense-in-depth.** Once the server-side filter is exact, the in-memory pass is strictly redundant. Keeping it would dilute the perf win for negligible safety value — the storage filter is unit-tested by `tests.test_session_records_repository.py` (filter shape) and the existing test suite (behavior parity), and the new repository test in U3 explicitly proves the server filter is the only check (mock storage can return mismatched records and the repository must trust the server result).
- **Do not change `_build_session_filter`'s signature.** Add `source_uri: Optional[str] = None` as a kwarg with the same semantics as the existing tenant/user kwargs (omitted from the cond list when None or empty). This keeps every existing caller working and only requires updating the four sites that have the in-memory post-filter today.
- **Index migration is idempotent and runs at startup.** `Qdrant create_payload_index` is no-op on existing indexes (Qdrant client handles it), so the new `ensure_scalar_indexes` call is safe to run on every startup. Operators don't need to do anything; the first restart after deploy adds the index. Docs in `CLAUDE.md` Operational Notes section noted as a follow-up.

---

## Open Questions

### Resolved During Planning

- **Schema lift vs nested filter?** Nested filter — Qdrant supports it, codebase already uses it for other fields, no backfill cost.
- **Backfill?** Not needed — existing records already have `meta.source_uri`.
- **Drop the in-memory post-filter?** Yes — server-side filter is exact, in-memory pass is dead code post-fix. The new repository test proves the repository trusts server result.
- **How do existing collections get the new index?** New `ensure_scalar_indexes()` startup hook wired into orchestrator init. Qdrant `create_payload_index` is idempotent.
- **Change `_build_session_filter` signature?** Add `source_uri` kwarg with `Optional[str] = None` default. Pattern matches the tenant/user kwargs added in PR #7 U2.

### Deferred to Implementation

- **Exact wording of the migration log line in `ensure_scalar_indexes`.** Match the style of `ensure_text_indexes` (`"[QdrantAdapter] Scalar indexes ensured on N collections"`).
- **Whether to add `meta.source_uri` to the trace / knowledge collection ScalarIndex too.** Out of scope for this PR. During U1 implementation, grep for `source_uri` filtering on trace/knowledge collection repositories. If found, file a separate follow-up PR — do NOT expand this PR's scope. If not found, no action needed (most likely outcome — those collections have different filter shapes per `collection_schemas.py`).

---

## Implementation Units

- [ ] U1. **Schema: add `meta.source_uri` to context collection ScalarIndex + ensure_scalar_indexes startup hook**

**Goal:** Declare the new payload index in the schema and add the startup migration that propagates it to existing collections.

**Requirements:** R4, R5

**Dependencies:** None

**Files:**
- Modify: `src/opencortex/storage/collection_schemas.py` (context collection's `ScalarIndex` list)
- Modify: `src/opencortex/storage/qdrant/adapter.py` (new `ensure_scalar_indexes()` method, mirroring `ensure_text_indexes`)
- Modify: `src/opencortex/orchestrator.py` (call `ensure_scalar_indexes()` in init alongside `ensure_text_indexes`)
- Test: `tests/test_qdrant_adapter.py` if it exists, otherwise extend an existing storage test (look during execution)

**Approach:**
- Append `"meta.source_uri"` to the context collection's `ScalarIndex` list. Confirm dot-path notation works at schema declaration site (consistent with the rest of the list which uses bare field names — the dot-path is a string and the adapter passes it straight to Qdrant's `create_payload_index(field_name=...)`).
- New method `QdrantStorageAdapter.ensure_scalar_indexes()`: iterate over each existing collection, look up its schema (via `_get_schema_for_collection` or equivalent — investigate during execution), and idempotently call `create_payload_index` for each `ScalarIndex` field. Use `_infer_payload_type` to determine the schema_type.
- Wire into orchestrator init: the method lives on `QdrantStorageAdapter`, NOT on the abstract `StorageInterface`. Call it via `hasattr(self._storage, 'ensure_scalar_indexes')` guard at the same init point as the existing `ensure_text_indexes` call (which uses the same pattern). InMemoryStorage doesn't implement it; the guard makes test paths a no-op without requiring a base-class shim. This mirrors how `ensure_text_indexes` is currently wired — verify the existing pattern during execution and follow it exactly.
- Investigate during execution: grep for `meta.source_uri` filter usage on the trace and knowledge collections. If any other collection's repository would benefit from the same index, file as a follow-up PR (don't expand this PR's scope).

**Patterns to follow:**
- `QdrantStorageAdapter.ensure_text_indexes()` (`src/opencortex/storage/qdrant/adapter.py:90`) — same loop shape, same try/except for idempotency, same logger.info summary line.
- The `for field_name in schema.get("ScalarIndex", []):` block inside `create_collection` (~line 166) — same `_infer_payload_type` + `create_payload_index` call.

**Test scenarios:**
- Happy path: `ensure_scalar_indexes()` creates the new index on a collection that didn't have it. Verify via `client.get_collection(name).payload_schema` includes `meta.source_uri`.
- Edge case: idempotency — calling `ensure_scalar_indexes()` twice doesn't error (Qdrant's `create_payload_index` is idempotent).
- Edge case: collection with no `ScalarIndex` declared — method skips it cleanly.

**Verification:**
- New index visible in `client.get_collection(...)` payload schema after one orchestrator init cycle.
- `ensure_scalar_indexes` is callable without error on a fresh InMemory test storage (no-op fallback acceptable since InMemoryStorage doesn't have payload indexes).
- Existing collection startup tests still pass.

---

- [ ] U2. **Push `source_uri` into `_build_session_filter` + drop in-memory post-filter**

**Goal:** Wire the server-side filter so callers that pass `source_uri` get records pre-filtered by Qdrant. Remove the now-redundant in-memory post-filter from the three repository methods.

**Requirements:** R1, R2, R3, R6

**Dependencies:** U1 (the index needs to exist for the filter to be fast; correctness-wise the filter works without the index but at full-scan cost)

**Files:**
- Modify: `src/opencortex/context/session_records.py`

**Approach:**
- Extend `_build_session_filter(...)` signature with `source_uri: Optional[str] = None`. When `source_uri` is non-None and non-empty, append `{"op": "must", "field": "meta.source_uri", "conds": [source_uri]}` to the conds list.
- Update each of `load_merged`, `load_directories`, `load_layers` to pass `source_uri=source_uri` when calling `_build_session_filter`. (`layer_counts` doesn't take source_uri today — leave it.)
- Remove the in-memory post-filter from `load_merged`, `load_directories`, `load_layers`. Each currently has:
  ```python
  if source_uri:
      if str(meta.get("source_uri", "") or "") != source_uri:
          continue
  ```
  These three blocks go away. The layer-filter block (`if str(meta.get("layer", "") or "") != "merged": continue` etc.) stays — it's not part of this change.
- Update the docstrings: `_build_session_filter` mentions the new kwarg; the four public methods drop the "filtered in-memory after fetch" note for `source_uri` (the kwarg is now exact at the storage layer).

**Patterns to follow:**
- The existing `tenant_id` / `user_id` push-down in `_build_session_filter` (lines ~170-180): same pattern of conditional `conds.append(...)` with `op="must"` and the new field name.

**Test scenarios:**
- Happy path: `_build_session_filter(session_id="s1", source_uri="src1")` produces a filter dict whose conds include `{"op": "must", "field": "meta.source_uri", "conds": ["src1"]}`.
- Edge case: `_build_session_filter(session_id="s1", source_uri=None)` produces a filter dict that does NOT include any `source_uri` cond (legacy fallback shape preserved).
- Edge case: `_build_session_filter(session_id="s1", source_uri="")` produces no `source_uri` cond — empty string is falsy and omitted from the filter, preserving backwards compat with the old `if source_uri:` truthiness check (the in-memory filter pre-fix also skipped empty strings).
- Trust-the-server test: a mock storage returns records where some have `meta.source_uri` mismatching the requested filter; assert the repository returns them anyway (proves the in-memory pass was removed and the repository trusts the server). This is the regression lock that catches anyone re-adding the in-memory filter.
- Existing tests in `tests/test_session_records_repository.py` continue to pass (they use the in-memory test storage which doesn't enforce the server filter; the test storage will return all records and the repository will now return all of them since there's no in-memory drop).

**Verification:**
- `tests/test_session_records_repository.py` updated as needed for the new filter shape; new tests added; all green.
- `tests.test_benchmark_ingest_lifecycle`, `tests.test_benchmark_ingest_service`, `tests.test_e2e_phase1`, `tests.test_context_manager` pass without modification (behavior parity for callers).
- Manual: grep `meta.get("source_uri"` inside `session_records.py` returns nothing inside the `load_*` methods (only the docstring reference, if any, remains).

---

- [ ] U3. **Test coverage: filter pushdown + in-memory drop + index migration**

**Goal:** Lock the new behavior with tests. Catches the three things that could regress: (a) someone re-adding the in-memory post-filter, (b) the filter dict shape drifting, (c) the index migration breaking on a fresh collection.

**Requirements:** R7

**Dependencies:** U2 (need the new behavior to test against)

**Files:**
- Modify: `tests/test_session_records_repository.py`
- Modify: `tests/test_qdrant_adapter.py` if it exists (otherwise integration coverage via `tests/test_e2e_phase1.py` is sufficient — investigate during execution)

**Approach:**
- Add `test_load_merged_pushes_source_uri_into_filter`: instantiate the repository with a `_CapturingStorage` (already used in the existing tests at `test_tenant_user_kwargs_push_scope_into_filter`), call `load_merged(session_id="s1", source_uri="src1")`, assert the captured filter includes the `meta.source_uri` cond with the right shape.
- Add `test_load_merged_omits_source_uri_when_not_provided`: same setup, call `load_merged(session_id="s1")` (no source_uri), assert the captured filter does NOT include any `source_uri`-related cond. Locks R2.
- Add `test_load_merged_trusts_server_filter`: mock storage returns 3 records — 2 with `meta.source_uri="src1"`, 1 with `meta.source_uri="src_other"`. Call `load_merged(session_id="s1", source_uri="src1")`. Assert all 3 records come through (the storage filter is mocked to return all 3, and the repository must trust the server). This is the regression lock that fails if someone re-adds the in-memory post-filter.
- Add similar tests for `load_directories` and `load_layers` (one each, mirroring the load_merged tests for the central path). Don't duplicate the full edge-case set — the central pattern is shared.
- For U1's index migration: add an adapter-level test that `ensure_scalar_indexes()` is called and idempotently creates indexes on existing collections. If `tests/test_qdrant_adapter.py` doesn't exist (likely), add basic coverage to whichever test exercises Qdrant adapter init today. Investigate during execution.

**Patterns to follow:**
- `tests/test_session_records_repository.py::test_tenant_user_kwargs_push_scope_into_filter` — the `_CapturingStorage` fake that records every filter dict passed to `storage.filter`. Reuse the same fake for the new tests.

**Test scenarios:**
- Already enumerated in Approach above. Each is a single test method; total ~5 new tests.

**Verification:**
- All new tests pass.
- `uv run python3 -m unittest tests.test_session_records_repository` returns OK (currently 11 tests; will be ~16).
- Full regression sweep (`tests.test_benchmark_ingest_lifecycle`, `tests.test_benchmark_ingest_service`, `tests.test_locomo_bench`, `tests.test_e2e_phase1`, `tests.test_context_manager`, `tests.test_http_server`) passes with only the pre-existing `test_update_regenerates_fact_points_after_content_change` failure.

---

## System-Wide Impact

- **Interaction graph:** Repository → storage → Qdrant. The new `meta.source_uri` cond is parsed by the existing `translate_filter` translator at `src/opencortex/storage/qdrant/filter_translator.py`. No new translator code path. The `ensure_scalar_indexes()` startup hook is called once per orchestrator init, in series with `ensure_text_indexes()`.
- **Error propagation:** If Qdrant rejects the new filter cond (e.g., index not yet created), the storage `filter` call would still work (Qdrant filters without an index are full-scan). No new error paths.
- **State lifecycle risks:** None. The new index is created idempotently; failure to create logs a warning but doesn't crash the adapter (mirrors `ensure_text_indexes` behavior).
- **API surface parity:** No HTTP / MCP surface change. Pure internal optimization.
- **Integration coverage:** `tests/test_e2e_phase1.py` exercises the full lifecycle; verifies behavior parity for production callers. `tests/test_locomo_bench.py` exercises the benchmark adapter; verifies the perf win doesn't introduce regressions.
- **Unchanged invariants:**
  - `_build_session_filter`'s legacy filter shape when `source_uri` is None / empty.
  - `layer_counts` (no source_uri parameter today; not changed).
  - The four `load_*` methods' return contract: still msg_range-sorted, still drops records with no `msg_range`, still applies `layer` filter in-memory (fast — already loaded the small set).
  - All `meta.source_uri` write sites (12 of them) — none modified.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| The new `meta.source_uri` payload index doesn't get created on existing production collections (operator forgets to restart, startup hook fails silently). | `ensure_scalar_indexes` runs at every init; idempotent. Even without the index the filter still works (full-scan), so the worst case is the perf-fix is delayed, not broken. Log line at INFO so operators see when the migration runs. |
| Test storage (`InMemoryStorage` in `tests/test_e2e_phase1.py`) doesn't honor the new `meta.source_uri` cond → tests pass even if the server filter is broken on real Qdrant. | The U3 unit-level tests (`_CapturingStorage`) verify the filter dict shape independently. If the dot-path notation breaks server-side, those tests catch the contract drift. End-to-end tests against real Qdrant are out of CI scope today; document as a known gap. |
| `ensure_scalar_indexes` calls `create_payload_index` with a dot-path key on a Qdrant version that doesn't support it. | Wrap each `create_payload_index` call in try/except (mirror `ensure_text_indexes` pattern at line 113). Log warning, continue — the filter still works without the index, just slower. |
| Removing the in-memory post-filter (U2/R3) could expose latent bugs where storage and meta have inconsistent `source_uri` values. | The U3 trust-the-server test explicitly exercises this — assertions document the intent. If real production data turns out to have `meta.source_uri` drift, that's a separate issue (write-site bug) that the in-memory filter was masking; surfacing it is better than hiding it. |
| Backwards compat: callers that explicitly pass `source_uri=""` (empty string) today get "no filter" behavior; this PR preserves that with the `if source_uri:` truthiness check. | Tested in U2 edge-case scenario; locked by U3 test. Documented in `_build_session_filter` docstring. |

---

## Documentation / Operational Notes

- Update `CLAUDE.md` Storage section to mention the new `meta.source_uri` payload index on the context collection. Operational note: after deploy, the first orchestrator init creates the index on existing collections; no operator action required.
- After merge, update `docs/residual-review-findings/refactor-server-side-design-patterns.md` to flip PERF-02 from open to closed with the merging commit hash.
- After merge, update `docs/residual-review-findings/2026-04-24-review-closure-tracker.md` to flip PERF-02 ❌ → ✅.
- Strong `/ce-compound` capture candidate after merge: "Push nested-field post-filters into the storage layer instead of in-memory iteration; Qdrant supports dot-path field names natively." Pattern applies broadly to any other `meta.X` filter currently done in Python.

---

## Sources & References

- **Closure tracker:** [docs/residual-review-findings/2026-04-24-review-closure-tracker.md](../../docs/residual-review-findings/2026-04-24-review-closure-tracker.md) — PERF-02 row, action queue tier 2 #4
- **PR #9 residuals:** [docs/residual-review-findings/refactor-server-side-design-patterns.md](../../docs/residual-review-findings/refactor-server-side-design-patterns.md) — PERF-02 description with confidence rating 85
- **Source code:** `src/opencortex/context/session_records.py` (target), `src/opencortex/storage/qdrant/filter_translator.py` (translator capabilities — `meta.X` dot-path support), `src/opencortex/storage/qdrant/adapter.py` (`ensure_text_indexes` pattern to mirror), `src/opencortex/storage/collection_schemas.py` (`ScalarIndex` list to extend), `src/opencortex/orchestrator.py` (init wiring)
- **Tests:** `tests/test_session_records_repository.py` (`_CapturingStorage` fake to reuse), `tests/test_e2e_phase1.py` (integration coverage baseline)
- **Prior PR closures:** PR #7 / U2 (`refactor(context): scope discipline + cursor pagination + overflow guard`) — established the tenant/user push-down pattern this PR follows for source_uri; PR #9 (PERF-01 + PERF-03) — closed the other two storage round-trip wastes; PERF-02 was deferred from #9 because of the (now-disproved) schema-change concern
