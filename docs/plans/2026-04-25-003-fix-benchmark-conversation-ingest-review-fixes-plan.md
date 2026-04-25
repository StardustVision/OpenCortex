---
title: "fix: Close benchmark-offline conversation ingest review findings (Phase 1+2)"
type: fix
status: active
date: 2026-04-25
origin: docs/brainstorms/2026-04-23-benchmark-offline-conversation-ingest-requirements.md
---

# fix: Close benchmark-offline conversation ingest review findings (Phase 1+2)

## Overview

The branch `feat/benchmark-offline-conv-ingest` introduces a benchmark-only offline conversation ingest path (`POST /api/v1/benchmark/conversation_ingest` + `ContextManager.benchmark_ingest_conversation` + `OCClient.benchmark_conversation_ingest`). A four-round CE code review (`.context/compound-engineering/ce-code-review/20260424-152926-6301c860/REVIEW.md`) returned **Not ready** with 1 P0, 14+ P1, and a long P2/P3 tail. This plan closes the merge-blocking findings (Phase 1, behavior-changing) and the highest-leverage cleanup (Phase 2, behavior-preserving) without touching production conversation lifecycle (`context_commit` / `context_end`).

The defining failure mode the review surfaced: `defer_derive=True` is set on benchmark merged-leaf writes but `_complete_deferred_derive` is **never scheduled**, so L0/L1/embed/anchor projections stay frozen on truncated first-line content. This violates the brainstorm R7 spirit ("benchmark merged leaves preserve enough text for `full_recompose` to operate on conversation text") and is the structural root cause behind the 2026-03-18 LoCoMo F1 0.49→0.33 regression captured in project memory. Phase 1 must close it before any other "performance" optimization on this path is meaningful.

---

## Problem Frame

REVIEW.md Round 4 verdict: **Not ready.** Three classes of risk are still open on the branch:

1. **Security / data integrity.** Public benchmark endpoint with no admin gate, no payload bounds, cross-tenant `layer_counts` leak, source/transcript collision on re-ingest, no cancellation-safe rollback, partial cleanup of summary/directory URIs.
2. **Benchmark correctness.** Deferred derive never completes (R2-01 / R3-P-12). Dict-merge order silently drops segment-level anchor aggregation (R2-03). `_export_memory_record` returns `content=""` so adapters can't fall back on raw content (R3-RC-06). Anchorless input bypasses recomposition caps (R2-13).
3. **Benchmark feasibility (R11).** Per-leaf serial embed (47 × 200ms = 14s/conv), serial directory LLM derive (8 × 4s = 36s/conv), default-on session summary, and serial cross-conversation adapter loop together push LoCoMo to ~33h serial; only batched embed + bounded concurrency at conversation and directory layers gets it to operationally feasible (~4-8h).

The branch has 9 files changed, ~1188 lines diff (~500 non-test executable). All code positions cited below are verified against the working tree as of HEAD `27f857a`.

---

## Requirements Trace

Carried forward from `docs/brainstorms/2026-04-23-benchmark-offline-conversation-ingest-requirements.md`:

- R1. Change applies only to benchmark ingestion paths (LoCoMo, LongMemEval).
- R2. Production / dev conversation-mode runtime unchanged.
- R3. Benchmark retrieval, QA generation, and scoring paths continue to use the existing evaluation flow after ingestion.
- R4. Offline ingest builds conversation records from full benchmark conversation/session input, without per-turn replay.
- R5. Offline merged leaves expose metadata compatible with current conversation retrieval and benchmark URI mapping (`session_id`, `msg_range`, `source_uri` where required).
- R6. Offline path reuses current full-session recomposition logic.
- R7. Offline path preserves enough original conversation text in merged leaves that `full_recompose` operates on text rather than only compressed summaries.
- R8. LoCoMo benchmark URI / session mapping remains valid under the offline path.
- R9. LongMemEval benchmark URI / session mapping remains valid under the offline path.
- R10. Offline ingest avoids per-item full-collection snapshot scans whose cost grows with collection size.
- R11. Full LoCoMo and LongMemEval runs are operationally feasible on local benchmark infrastructure.

Plus REVIEW Section 26 merge-blocking acceptance:

- AR1. Non-admin token calling `POST /api/v1/benchmark/conversation_ingest` returns 403.
- AR2. Requests exceeding segments / messages / content / meta upper bounds return 422.
- AR3. Same `session_id` + identical transcript is idempotent. Same `session_id` + different transcript returns 409 (or uses a versioned `source_uri`) — never silently mixes runs.
- AR4. Summary failure, recomposition failure, and `CancelledError` mid-ingest leave no orphan merged / directory / source / summary records.
- AR5. `layer_counts` is either removed from the response or scoped by `(tenant_id, user_id, source_uri)`; cannot be used to enumerate other tenants.
- AR6. `include_session_summary=False` is the default for benchmark adapters; URI mapping and recall inputs remain valid with summary disabled.
- AR7. Benchmark merged leaves end up with semantically meaningful L0 / L1 / embed / anchors equivalent to production conversation merged leaves.
- AR8. Production conversation lifecycle (`context_commit`, `context_end`) regression suite passes unchanged.

---

## Scope Boundaries

- Do not modify production `context_commit`, `context_end`, merge follow-up, or session-end orchestration.
- Do not change benchmark scoring methodology or evaluation flow downstream of ingest.
- Do not generalize the offline path into a public conversation-import API.
- Do not introduce a 12th MCP tool (REVIEW agent-native check passed; this remains benchmark infra).
- Do not write a 2-phase commit / external transaction coordinator across Qdrant + CortexFS — compensation tracker is sufficient.

### Deferred to Follow-Up Work

- Cross-tenant `session_id` filter hardening in `_load_session_merged_records` for the production hot path (pre-existing, REVIEW Section 5).
- `_session_locks` reaper for unbounded per-session lock dict (pre-existing, REVIEW Section 5).
- `_RECOMPOSE_CLUSTER_MAX_*` constants tuning beyond the no-effective-limit fix (R2-20 follow-up after Phase 1).
- 100k-conversation scale ceiling rework (queue-backed ingest / checkpointing) — outside this PR.
- `_OCStub` migration to `AsyncMock(spec_set=OCClient)` (R2-27) — Phase 2 cleanup follow-up PR.
- `BenchmarkConversationIngestService` extraction beyond helper-method splitting (R2-11) — Phase 2 follow-up PR if Phase 1 keeps growing the method.

---

## Context & Research

### Relevant Code and Patterns

- `src/opencortex/http/admin_routes.py` (lines 43-46 + ten existing admin routes) — `_require_admin()` pattern + 403 on missing token.
- `src/opencortex/http/server.py:479-497` — current benchmark route (no admin gate, accesses `_orchestrator._context_manager` private member).
- `src/opencortex/http/models.py:256-265, 292-312` — `ContextMessage`, `BenchmarkConversationSegment`, `BenchmarkConversationIngestRequest` (no `max_length`).
- `src/opencortex/context/manager.py:1193-1347` — `benchmark_ingest_conversation` body, including the `try/except Exception:` rollback at 1340.
- `src/opencortex/context/manager.py:1020-1063` — `_persist_rendered_conversation_source` (existing-source short-circuit).
- `src/opencortex/context/manager.py:1066-1120` — `_benchmark_segment_meta`.
- `src/opencortex/context/manager.py:1141-1191` — `_benchmark_recomposition_entries` (dict-merge bug at line ~1152).
- `src/opencortex/context/manager.py:2864-2881` — production `_complete_deferred_derive` scheduling pattern to mirror.
- `src/opencortex/context/manager.py:2208` — `_run_full_session_recomposition` directory derive loop (serial today).
- `src/opencortex/context/manager.py:2583` — `_delete_immediate_families` (single-URI failure aborts cleanup loop).
- `src/opencortex/context/manager.py:1378, 1427` — `_load_session_merged_records`, `_session_layer_counts` (10 000 silent truncation; not tenant-scoped).
- `src/opencortex/orchestrator.py:2550-2677` — `add` method including `defer_derive` truncation branch.
- `src/opencortex/orchestrator.py:1931-1941` — `_complete_deferred_derive` signature.
- `src/opencortex/storage/qdrant/adapter.py` (existing) — `embed_batch` already used by `_sync_anchor_projection_records`; not exposed at orchestrator layer for arbitrary writes.
- `benchmarks/oc_client.py:239-257` — `benchmark_conversation_ingest` client (timeout=600.0, retry_on_timeout=False).
- `benchmarks/adapters/locomo.py:507-524` and `benchmarks/adapters/conversation.py:387-421` — store-mode dispatch in two adapters (duplicated structure).

### Institutional Learnings

- `docs/solutions/best-practices/single-bucket-scoped-probe-2026-04-16.md` — Recall paths must keep structured scope inputs; scoped miss stays scoped. Applies to U6 (scope leak) and to ensuring benchmark store/mcp paths stay scope-equivalent.
- `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md` — If benchmark response gains retrieval-shaped output, route through `memory_pipeline.{probe,planner,runtime}`; do not revive flat fields. Currently informs U20 (typed response model) — keep the response schema closed against accidental drift back to flat retrieval shape.
- No existing `docs/solutions/` entry for session-lock lifecycle, partial-write failure cleanup, or benchmark-only HTTP route patterns. REVIEW Section 6 flags these as `/ce-compound` candidates after merge.

### External References

External research skipped: codebase has strong local patterns for every change (admin route gating, deferred-derive scheduling, per-session lock, embed batching). REVIEW.md provides authoritative cross-validation across 12 reviewer personas in 4 rounds.

---

## Key Technical Decisions

- **Route placement.** Move the benchmark endpoint into `admin_routes.py` (or a sibling `benchmark_routes.py` mounted under the admin router). The route is benchmark-only infrastructure with admin-gated semantics; keeping it in `server.py` while adding `_require_admin()` would still violate the existing layering convention (REVIEW R2-10).
- **Source versioning strategy.** Add a `transcript_hash` (SHA-256 over normalized transcript) to the source meta. On re-ingest:
  - Same hash → idempotent: return existing source, skip leaf rewrite.
  - Different hash → return HTTP 409 with the existing hash so the caller can rotate `session_id` deliberately. No silent overwrite.
  Versioned `source_uri` was considered and rejected — it would break R8/R9 URI mapping stability and make `_load_session_merged_records(source_uri=...)` ambiguous.
- **Cleanup tracker.** Introduce a small `_BenchmarkRunCleanup` dataclass tracking `source_uri`, `merged_uris: list`, `directory_uris: list`, `summary_uri: Optional[str]`. `_run_full_session_recomposition` returns the directory URIs it created so the benchmark layer can register them with the tracker. Tracker exposes `compensate()` that iterates and removes each URI with per-item `try/except`. This avoids a heavyweight Unit-of-Work abstraction (REVIEW guardrail) while closing F6 / R2-09.
- **Cancellation handling.** Catch `asyncio.CancelledError` separately from `Exception`; run the same compensation; re-raise. Do not collapse into `BaseException` (would catch `KeyboardInterrupt` / `SystemExit`).
- **`layer_counts` decision.** Drop from response. The adapter does not consume it (verified). Keep `_session_layer_counts` as an internal helper but call it inside structured logging only, not in the HTTP envelope. Smaller surface, no scoping work needed at the helper.
- **Defer-derive parity.** Schedule `_complete_deferred_derive` for each benchmark merged leaf via the same `_bounded_derive` semaphore pattern as production. **Await all derive tasks before returning the response** so the benchmark response represents the post-derive record state. This adds latency back but is required for AR7 and brainstorm R7. Net wall-clock is still dominated by Phase 1 batching wins.
- **Embed batching.** Add `MemoryOrchestrator.add_batch(records: list[AddRequest], *, defer_derive=True) -> list[Context]` that does a single `embed_batch` then per-record Qdrant upsert. Keep `add()` as the single-record convenience wrapper. Do not change call sites outside the benchmark path in this PR.
- **Cross-conversation concurrency.** Add `--ingest-concurrency` (default 4) to the benchmark adapters; wrap the per-conversation loop in `asyncio.Semaphore`. Independent client-side change; no server contract change.
- **Adapter `include_session_summary` default.** Adapter defaults to `False` for store-path. Keep request model default `True` so direct API callers retain current behavior. (Adapter-only behavior change, not API-default change.)
- **Anchorless cap fix.** In `_build_anchor_clustered_segments`, anchorless append branch must check `_RECOMPOSE_CLUSTER_MAX_TOKENS` / `_MAX_MESSAGES`. Also reduce both constants from `1_000_000` to realistic values (target: 6 000 tokens / 60 messages — matches typical LLM context budget for `_derive_parent_summary`).
- **Routing of behavior change vs refactor.** Phase 1 ships behavior-changing fixes only. Phase 2 ships behavior-preserving cleanups (route move can land in either; ship in Phase 1 because it's coupled to admin gate).

---

## Open Questions

### Resolved During Planning

- **Route placement now or later?** — Now. Admin gate + route relocation are coupled; moving in Phase 1 avoids a second migration.
- **Drop or scope `layer_counts`?** — Drop. Cheaper than scoping correctly; adapter doesn't consume it.
- **Versioned `source_uri` or 409?** — 409. Preserves URI stability for R8/R9.
- **Await deferred derive in response or fire-and-forget?** — Await. AR7 requires merged leaves to have completed L0/L1 by the time the adapter reads them.
- **Per-conversation lock around full ingest, or scoped to source-write only?** — Keep full-ingest lock for now (R3-P-01 is P2). Scoping requires guarantees about idempotent re-entry that interact with U4 (versioning); decoupling that risk from this PR.

### Deferred to Implementation

- Exact constants for `_RECOMPOSE_CLUSTER_MAX_*` — start at 6 000 tokens / 60 messages, tune if benchmark anchor distribution shows premature splitting.
- Exact `--ingest-concurrency` ceiling — start at 4, raise to 8 if local Qdrant + embedder hold up. Driven by Phase 1 measurements.
- Whether to keep `with contextlib.suppress(Exception)` around per-URI cleanup or use the new logging shim — implementation will pick whichever reads cleaner once `_BenchmarkRunCleanup` exists.
- Whether `MemoryOrchestrator.add_batch` becomes a public orchestrator method or a benchmark-only helper hanging off `ContextManager` — depends on whether U12 surfaces other call sites that want batching during implementation.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
HTTP layer (admin_routes.py)
  POST /api/v1/admin/benchmark/conversation_ingest
    -> _require_admin()
    -> Pydantic validation (segments<=N, messages<=M, content<=L)
    -> asyncio.wait_for(handler, timeout=540s)   # server-side ceiling under client 600s
    -> orchestrator.benchmark_conversation_ingest(...)   # public facade

ContextManager.benchmark_ingest_conversation(...):
  cleanup = _BenchmarkRunCleanup()
  try:
    source_uri, action = _persist_rendered_conversation_source_versioned(...)
        # action ∈ {created, idempotent_hit, conflict}
        # conflict -> raise HTTPException(409)
    cleanup.source_uri = source_uri if action == "created" else None
    entries = _benchmark_recomposition_entries(normalized_segments)   # dict-merge fixed
    leaves = await orchestrator.add_batch([...], defer_derive=True)   # batched embed
    cleanup.merged_uris.extend(l.uri for l in leaves)
    directory_uris = await _run_full_session_recomposition(..., return_created=True)
        # directory derive: bounded asyncio.gather(Semaphore(3))
    cleanup.directory_uris.extend(directory_uris)
    if include_session_summary:
        summary_uri = await _generate_session_summary(...)
        cleanup.summary_uri = summary_uri
    await _await_pending_deferred_derives(leaves)   # AR7 parity
    merged_records = await _load_session_merged_records(session_id, source_uri=source_uri)
    return _build_response(merged_records, source_uri, summary_uri)   # NO layer_counts
  except asyncio.CancelledError:
    await cleanup.compensate()
    raise
  except Exception:
    await cleanup.compensate()
    raise
  finally:
    _cleanup_session(sk)   # tear down per-session state

Adapter layer (locomo.py / conversation.py):
  semaphore = asyncio.Semaphore(args.ingest_concurrency)
  async def _one_conv(conv):
      async with semaphore:
          await oc.benchmark_conversation_ingest(
              session_id=...,
              segments=...,
              include_session_summary=False,   # adapter default flip
          )
  await asyncio.gather(*[_one_conv(c) for c in conversations])
```

---

## Implementation Units

Phased delivery: U1-U16 are Phase 1 (behavior-changing, must merge before branch is ready). U17-U21 are Phase 2 (behavior-preserving, ship in same PR if time permits, otherwise follow-up PR before benchmark suite expansion).

### Phase 1 — Behavior-Changing P0/P1 Fixes

- [ ] U1. **Move benchmark route to admin_routes.py with admin gate**

**Goal:** Close P0 (REVIEW Finding #1) by moving `POST /api/v1/benchmark/conversation_ingest` into `admin_routes.py` (or new `benchmark_routes.py` mounted under admin router) and gating with `_require_admin()`. Also adds public orchestrator facade so the route stops accessing `_orchestrator._context_manager` private member (REVIEW Section 23 P2).

**Requirements:** AR1, AR8, R1, R2.

**Dependencies:** None.

**Files:**
- Modify: `src/opencortex/http/server.py` (remove existing route at lines 479-497)
- Modify: `src/opencortex/http/admin_routes.py` (add benchmark route mounted under existing admin router)
- Modify: `src/opencortex/orchestrator.py` (add public `benchmark_conversation_ingest(...)` facade delegating to `_context_manager.benchmark_ingest_conversation`)
- Test: `tests/test_http_server.py` (move existing `test_04d`; add 403 for non-admin token; add 200 for admin token)

**Approach:**
- Endpoint URL: `POST /api/v1/admin/benchmark/conversation_ingest` (under admin namespace) — adapter URL update lands in U13.
- First line in the new handler must be `_require_admin()`; mirror the pattern from `admin_routes.py:67/85/94`.
- Public facade signature on `MemoryOrchestrator` matches `ContextManager.benchmark_ingest_conversation` keyword args 1:1; no business logic in the facade.
- Adapter (`benchmarks/oc_client.py`) endpoint string updates in U13 — keep coupled with concurrency change so adapter regression runs once.

**Patterns to follow:**
- Existing admin routes in `admin_routes.py:64-237`.
- Existing public facades on `MemoryOrchestrator` (e.g., `recall`, `add`).

**Test scenarios:**
- Happy path: admin token POST returns 200 with `{status, records, source_uri, summary_uri}`. Covers AE for R1.
- Error path: non-admin authenticated token returns 403 `{detail: "Admin access required"}`.
- Error path: unauthenticated request returns 401 (existing middleware behavior; assert it still triggers under new mount point).
- Integration: `_require_admin()` actually short-circuits before any `_orchestrator` call (mock the orchestrator and assert it was never invoked under non-admin path).

**Verification:** non-admin token cannot reach handler; existing test_04d_benchmark_conversation_ingest_preserves_traceability_contract passes after URL update.

---

- [ ] U2. **Add Pydantic payload bounds**

**Goal:** Close P1 #2. Cap `segments`, `segments[].messages`, `messages[].content`, and `meta` size on the request models so a single admin-token slip cannot fan out to thousands of LLM calls.

**Requirements:** AR2, R10.

**Dependencies:** U1 (need new route in place to wire 422 tests).

**Files:**
- Modify: `src/opencortex/http/models.py` (lines 256-265 `ContextMessage`, lines 292-312 `BenchmarkConversationSegment` + `BenchmarkConversationIngestRequest`)
- Test: `tests/test_http_server.py`

**Approach:**
- `BenchmarkConversationIngestRequest.segments`: `Field(..., max_length=200)`.
- `BenchmarkConversationSegment.messages`: `Field(..., max_length=2000)`.
- `ContextMessage.content`: `Field(..., max_length=64_000)` (per-message; matches LLM context budget headroom).
- `ContextMessage.meta`: keep `Optional[Dict[str, Any]]` but add Pydantic-side check that the JSON-serialized dict ≤ 16 KB (use `model_validator`).
- Limits chosen to comfortably accommodate LoCoMo / LongMemEval real distributions: max LoCoMo conversation observed = 27 sessions × ~22 turns ≈ 600 messages, single-message content typically <2 KB.

**Patterns to follow:**
- `BenchmarkConversationIngestRequest.session_id` already uses `Field(..., pattern=...)` — extend the same posture.

**Test scenarios:**
- Error path: 201 segments → 422 with field error path.
- Error path: 2001 messages in one segment → 422.
- Error path: content > 64 KB → 422.
- Error path: meta dict serializing to > 16 KB → 422.
- Happy path: a synthetic LoCoMo-shaped payload (27 segments × 22 messages × 1 KB) passes validation.

**Verification:** Adapter regression (LoCoMo + LongMemEval) still passes — limits are above real benchmark distribution.

---

- [ ] U3. **Cancellation-safe rollback in benchmark_ingest_conversation**

**Goal:** Close P1 #3. Catch `asyncio.CancelledError` separately from `Exception`, run the same compensation, re-raise. Add structured `logger.warning(..., exc_info=True)` around per-URI cleanup so silent compensation failures stop being invisible (REVIEW Finding #13).

**Requirements:** AR4, R2.

**Dependencies:** None (precedes U4 cleanup tracker so the new tracker inherits cancellation safety).

**Files:**
- Modify: `src/opencortex/context/manager.py` (lines 1263-1347 try/except block)
- Test: `tests/test_context_manager.py` or new `tests/test_benchmark_ingest_rollback.py`

**Approach:**
- Replace `except Exception:` with `except (Exception, asyncio.CancelledError):` followed by compensation + `raise`. Verify Python re-raise semantics preserve `CancelledError`.
- Wrap per-URI cleanup calls in `try/except` per item (U17 generalizes this; U3 is the localized first pass for the existing block before tracker introduction).
- Add `logger = logging.getLogger(__name__)` if not already imported in manager.py; use `logger.warning("benchmark_ingest cleanup failed for %s", uri, exc_info=True)`.

**Execution note:** Add the failure-injection regression test (U16) before the fix; confirm it fails on master, passes after this unit lands.

**Patterns to follow:**
- Look for any existing `except (Exception, asyncio.CancelledError)` patterns in `src/opencortex/orchestrator.py` or `src/opencortex/context/manager.py` for stylistic alignment.

**Test scenarios:**
- Error path: monkeypatch `_run_full_session_recomposition` to raise `asyncio.CancelledError`; assert compensation runs; assert `CancelledError` re-raises; assert `created_merged_uris` records were removed from Qdrant.
- Error path: monkeypatch `_orchestrator.remove` to raise during compensation; assert `CancelledError` still re-raises; assert warning was logged.
- Integration: simulate client disconnect mid-ingest via `asyncio.wait_for(..., timeout=0.01)`; verify no orphan records.

**Verification:** Cancelled in-flight request leaves no orphan merged / source / summary in storage.

---

- [ ] U4. **Run-scoped cleanup tracker**

**Goal:** Close P1 #5 / Finding #6 / R2-09. Introduce `_BenchmarkRunCleanup` dataclass tracking `source_uri`, `merged_uris`, `directory_uris`, `summary_uri`. `_run_full_session_recomposition` returns the directory URIs it created so the benchmark layer can register them. Compensation iterates each list with per-item failure isolation.

**Requirements:** AR4, R2.

**Dependencies:** U3 (cancellation-safe try/except is in place).

**Files:**
- Modify: `src/opencortex/context/manager.py` (add `_BenchmarkRunCleanup` near `benchmark_ingest_conversation`; modify `_run_full_session_recomposition` to optionally return created directory URIs without changing existing call sites)
- Modify: `src/opencortex/context/manager.py` (`benchmark_ingest_conversation` body — replace local lists with tracker)
- Test: `tests/test_context_manager.py` or rollback test from U3

**Approach:**
- Dataclass with `compensate()` that calls `await self._orchestrator.remove(uri)` per URI in reverse order (summary → directories → leaves → source); each in a `try/except + log` block.
- `_run_full_session_recomposition` adds a kwarg `return_created_uris: bool = False`. When True, returns `(existing_return_value, list[str])`. All existing call sites pass False (default) and behavior is unchanged. Production `context_end` path is one of those existing sites — verify no behavioral drift.
- After `_orchestrator.add` returns, append URI to tracker BEFORE awaiting any subsequent side-effect chain (R2-09 fix).

**Patterns to follow:**
- Look at how `_restore_merge_snapshot` (REVIEW Section 15 mentions it) implements compensation to keep stylistic consistency. Avoid forking a second compensation pattern.

**Test scenarios:**
- Error path: `_generate_session_summary` raises after recomposition completes; assert directory URIs are removed (was orphan before this fix).
- Error path: `_orchestrator.add` succeeds for leaf 5/10, then `_sync_anchor_projection_records` raises; assert leaves 1-5 are tracked and removed.
- Error path: each cleanup remove fails independently; assert all are attempted and warnings logged for each.
- Integration: simulate full failure cascade end-to-end; assert zero orphan records via Qdrant filter scan.

**Verification:** No orphan merged / directory / summary / source after any failure mode.

---

- [ ] U5. **Source versioning via transcript hash**

**Goal:** Close P1 #4 / R2-05 / R2-17. Persist a `transcript_hash` on the source meta. On re-ingest:
- Same hash → idempotent: return existing source URI, skip leaf rewrite, return early with the existing record set.
- Different hash → return HTTP 409 `Conflict` with payload `{existing_hash, supplied_hash}`.
- Also: ingest start clears any pre-existing merged / directory records under the same `(session_id, source_uri)` to prevent stale URI residue from a prior aborted run (R2-17).

**Requirements:** AR3, R2, R5, R8, R9.

**Dependencies:** U4 (cleanup tracker so pre-ingest purge is reusable).

**Files:**
- Modify: `src/opencortex/context/manager.py` (`_persist_rendered_conversation_source` → `_persist_rendered_conversation_source_versioned`)
- Modify: `src/opencortex/context/manager.py` (`benchmark_ingest_conversation` add purge step + handle versioning return)
- Modify: `src/opencortex/http/admin_routes.py` (translate `SourceConflictError` → `HTTPException(409)`)
- Test: `tests/test_context_manager.py` or new test file

**Approach:**
- Compute `hashlib.sha256(json.dumps(normalized_transcript, sort_keys=True).encode()).hexdigest()` once.
- Write `transcript_hash` into source meta. On read-back, compare. Define `SourceConflictError(existing_hash: str, supplied_hash: str)` raised from helper, caught at HTTP layer.
- For idempotent path: return existing merged/directory records via `_load_session_merged_records(session_id, source_uri=source_uri)` and skip leaf write entirely. Response indicates `idempotent_hit=True` in meta (kept out of records[] so adapter behavior is unchanged).
- For pre-ingest purge: `await _delete_immediate_families(existing_merged_uris + existing_directory_uris)` BEFORE writing new leaves. Skip purge if `idempotent_hit`.

**Patterns to follow:**
- Existing `_get_record_by_uri` / `_load_session_merged_records` for reading.

**Test scenarios:**
- Happy path: identical re-ingest is idempotent (same response, no new Qdrant writes — assert via a write-counting mock).
- Error path: same `session_id`, different transcript → 409 with both hashes in detail.
- Edge case: re-ingest after partial failure (some leaves wrote, then crash) — pre-purge clears stale leaves before new write.
- Integration: LoCoMo adapter re-runs same conversation back-to-back → second run is idempotent → adapter URI mapping still matches.

**Verification:** No silent transcript mixing under same `session_id`. R8/R9 URI mapping unchanged for non-conflicting runs.

---

- [ ] U6. **Drop `layer_counts` from response**

**Goal:** Close P1 R2-04. Remove `layer_counts` from the benchmark endpoint response. Confirm via codebase grep + adapter trace that no consumer reads it. Internal logging still uses `_session_layer_counts` if useful.

**Requirements:** AR5, R1.

**Dependencies:** None.

**Files:**
- Modify: `src/opencortex/context/manager.py` (`benchmark_ingest_conversation` response build — drop `layer_counts` key)
- Modify: `tests/test_http_server.py` (`test_04d` — drop assertion on `layer_counts`; add assertion that key is NOT in response)
- Modify: `tests/test_locomo_bench.py` (`_OCStub` — drop `layer_counts` from stub response; verify adapter still works)

**Approach:**
- `grep -rn "layer_counts" benchmarks/ tests/` to confirm no other consumer.
- If `_session_layer_counts` has no remaining call site after this change, mark the helper deprecation-pending (don't delete here — pre-existing usage in logs may still exist).

**Patterns to follow:**
- N/A — pure removal.

**Test scenarios:**
- Happy path: response shape no longer contains `layer_counts`.
- Negative test: attempt to enumerate via `layer_counts` returns no signal (key absent).

**Verification:** No tenant can derive other-tenant record counts from this endpoint's response.

---

- [ ] U7. **Anchorless recomposition cap enforcement + realistic cluster limits**

**Goal:** Close P1 R2-13 + R2-20. In `_build_anchor_clustered_segments`, the anchorless append branch must check `_RECOMPOSE_CLUSTER_MAX_TOKENS` / `_RECOMPOSE_CLUSTER_MAX_MESSAGES`. Reduce both constants from `1_000_000` to realistic values (initial: 6 000 tokens / 60 messages).

**Requirements:** AR8, R2.

**Dependencies:** None.

**Files:**
- Modify: `src/opencortex/context/manager.py` (constants + `_build_anchor_clustered_segments` anchorless branch)
- Test: `tests/test_context_manager.py` or `tests/test_recomposition.py` if it exists

**Approach:**
- Constants near top of `manager.py`.
- Anchorless branch: same `within_caps` check as the anchor branch; on cap exceeded, flush current cluster and start a new one.
- This change touches a function on the production conversation path. Run full conversation lifecycle regression after.

**Patterns to follow:**
- Existing anchor-branch cap check.

**Test scenarios:**
- Edge case: 200-message anchorless input → multiple clusters, each ≤ 60 messages.
- Edge case: single 8 000-token anchorless message → split or routed to single-cluster fallback (assert no LLM context overflow in `_derive_parent_summary`).
- Regression: existing anchor-clustering tests still pass.
- Integration: production `context_commit` / `context_end` test on a fixture with no entities/topics still completes.

**Verification:** No `_derive_parent_summary` invocation receives `children_abstracts` exceeding model context window.

---

- [ ] U8. **Defer-derive parity: schedule `_complete_deferred_derive` in benchmark path**

**Goal:** Close R2-01 / R3-P-12. The structural root cause behind LoCoMo F1 0.49→0.33. After all merged leaves are written with `defer_derive=True`, schedule `_complete_deferred_derive` for each via the existing `_bounded_derive` semaphore pattern, **and await all derive tasks** before returning the response so AR7 holds.

**Requirements:** AR7, R5, R7, R10, R11.

**Dependencies:** U12 (batch write must land before scheduling derive on the batched leaves; otherwise we re-introduce sequential overhead).

**Files:**
- Modify: `src/opencortex/context/manager.py` (`benchmark_ingest_conversation` — after `add_batch` returns, build derive task list mirroring `manager.py:2864-2881`; await `asyncio.gather(*tasks, return_exceptions=False)`)
- Test: `tests/test_context_manager.py` or new `tests/test_benchmark_derive_parity.py`

**Approach:**
- Build derive task list using same `_derive_semaphore` instance as production.
- `raise_on_error=True` so a failed derive surfaces as ingest failure (cleanup tracker handles).
- Each task gets `combined` content of its segment (already in scope) and `aggregated_meta` from `_benchmark_segment_meta` (post-U9 fix).

**Patterns to follow:**
- Production `_commit_merge` scheduling at `manager.py:2864-2881` is the reference implementation.

**Test scenarios:**
- Happy path: after ingest, fetch a merged leaf via `_orchestrator.recall`; assert L0/L1 differ from the truncated-first-line placeholder (LLM-derived content present).
- Happy path: assert anchors include entities/topics (non-empty `slots` projection).
- Error path: monkeypatch `_complete_deferred_derive` to raise; assert ingest fails AND cleanup runs (tracker covers leaves).
- Integration: LoCoMo adapter store-mode test — fetch one merged leaf, assert its L1 is not equal to the first 1200 characters of raw content.
- Regression: production `context_commit` / `context_end` scheduling unchanged.

**Verification:** Benchmark merged leaves have semantically meaningful L0/L1/embed/anchors equivalent to production. Re-running 2026-03-18 LoCoMo benchmark should show F1 recovery (capture in PR description).

---

- [ ] U9. **Fix dict-merge order in `_benchmark_segment_meta` / `_benchmark_recomposition_entries`**

**Goal:** Close R2-03 / R3-RC-05. The current `{**segment_meta, **(message.get("meta") or {})}` order causes message-level meta to silently overwrite segment-level aggregation (entities, topics, time_refs). Fix by inverting the order OR by only populating segment-level keys when message-level keys are absent.

**Requirements:** AR7, R5.

**Dependencies:** U8 (the derive then sees the corrected anchors).

**Files:**
- Modify: `src/opencortex/context/manager.py` (`_benchmark_recomposition_entries` line ~1152 + any other call site of `_benchmark_segment_meta`)
- Test: `tests/test_context_manager.py`

**Approach:**
- Invert merge: `{**(message.get("meta") or {}), **segment_meta}` so segment-level wins. Confirm semantic intent: the segment-level aggregation IS the canonical source for entities/topics across the segment; message-level meta is per-message context that should not displace segment anchors.
- Alternative considered: per-key conditional merge (`for k, v in segment_meta.items(): merged.setdefault(k, v)`) — more verbose, equivalent semantics. Pick whichever reviewer feedback in U16 prefers.

**Patterns to follow:**
- N/A — bug fix.

**Test scenarios:**
- Happy path: input with conflicting `topics` between segment-level and message-level meta — assert merged entry uses segment-level value.
- Edge case: input with no segment-level `entities` but message-level `entities` present — message value preserved.
- Integration: end-to-end LoCoMo conversation ingest — anchors on merged leaves include both segment and message contributions.

**Verification:** `_benchmark_segment_meta` is no longer dead code on the hot path.

---

- [ ] U10. **Fix `_export_memory_record` content always empty**

**Goal:** Close R3-RC-06. `Context.to_dict()` does not include `content` (it lives in CortexFS). `_export_memory_record` does `record.get("content", "")` so the field is always empty in the response, breaking any adapter that wants to fall back on raw content.

**Requirements:** AR7, R5.

**Dependencies:** None.

**Files:**
- Modify: `src/opencortex/context/manager.py` (`_export_memory_record` ~line 522)
- Test: `tests/test_http_server.py` (extend `test_04d` to assert content is non-empty for at least one record)

**Approach:**
- Two options: (a) hydrate content from CortexFS via `await fs.read_context(uri, level="l2")`; (b) explicitly remove `content` from response and document that adapter must fetch via separate API.
- Recommend (a) — adapter expects content-bearing records (R7). Hydrate content during response build. Acceptable cost: ~37 reads × ~1ms cached FS = ~40ms, negligible vs LLM time.
- If multiple reads in a row, use existing CortexFS batch-read if available; otherwise sequential is fine for now.

**Patterns to follow:**
- Existing `MemoryOrchestrator.recall` content-hydration path.

**Test scenarios:**
- Happy path: response records include non-empty `content` matching the original transcript text for that `msg_range`.
- Edge case: missing CortexFS file → `content=""` and a warning logged (don't fail the whole ingest).
- Integration: LoCoMo adapter `_OCStub` and real adapter paths both consume non-empty content.

**Verification:** Adapter content-fallback path works against real responses.

---

- [ ] U11. **Adapter default `include_session_summary=False`**

**Goal:** Close P2 #12 / F12. Adapter passes `include_session_summary=False` explicitly. API request model default stays `True` so direct API callers retain current behavior.

**Requirements:** AR6, R11.

**Dependencies:** None.

**Files:**
- Modify: `benchmarks/adapters/locomo.py` (line ~514 — `oc.benchmark_conversation_ingest(..., include_session_summary=False)`)
- Modify: `benchmarks/adapters/conversation.py` (line ~410 — already does this for mainstream, ensure store branch matches)
- Modify: `benchmarks/oc_client.py` (docstring note that adapters override default)
- Test: `tests/test_locomo_bench.py` (assert `_OCStub` receives `include_session_summary=False`)

**Approach:**
- Single-line change per adapter.
- Add CLI flag `--benchmark-session-summary` (default off) for explicit override, in `benchmarks/unified_eval.py`. Optional — only add if implementation reveals a real need; otherwise hard-code false for simplicity.

**Patterns to follow:**
- Existing adapter dispatch in `conversation.py:387-421`.

**Test scenarios:**
- Happy path: stub assertion that `include_session_summary=False` was passed.
- Happy path: response `summary_uri` is `None`; URI mapping still resolves.
- Regression: full LoCoMo adapter test still passes; recall scoring unchanged (summary not consumed).

**Verification:** ~4s LLM + 2 filter scans per conversation eliminated. URI mapping intact.

---

- [ ] U12. **`MemoryOrchestrator.add_batch` + `embed_batch` integration**

**Goal:** Close R2-08 / R3-P-05. Add `MemoryOrchestrator.add_batch(records, *, defer_derive=True)` that does a single `embed_batch` then per-record Qdrant upsert + CortexFS write. Update `benchmark_ingest_conversation` to call it.

**Requirements:** R10, R11.

**Dependencies:** None (precedes U8 because U8 schedules derive for the batched leaves).

**Files:**
- Modify: `src/opencortex/orchestrator.py` (add `add_batch` method)
- Modify: `src/opencortex/context/manager.py` (`benchmark_ingest_conversation` — replace per-leaf `_orchestrator.add` loop with single `add_batch`)
- Test: `tests/test_orchestrator.py` (or wherever `add` is tested) — add `add_batch` happy path + behavioral parity test
- Test: `tests/test_context_manager.py` — assert benchmark path uses batch call

**Approach:**
- Signature: `async def add_batch(self, requests: list[dict], *, defer_derive: bool = False) -> list[Context]`.
- Internally: collect texts → single `embedder.embed_batch` → loop Qdrant `upsert` (or batch upsert if Qdrant supports) → loop fire-and-forget CortexFS write (existing pattern).
- Returns Context objects in input order.
- Preserve any pre-existing observability / metrics hooks from single `add`.

**Execution note:** Add a parity test (single `add` vs `add_batch` of one record) to ensure no behavior drift.

**Patterns to follow:**
- `embed_batch` usage in `_sync_anchor_projection_records`.
- Existing `add` method as the per-record reference.

**Test scenarios:**
- Happy path: 10-record batch produces 10 Contexts with correct URIs.
- Happy path: single embed call invoked with all 10 texts.
- Edge case: empty batch → returns empty list, no embed call.
- Edge case: one record with empty content → matches `add(content="")` behavior.
- Integration: benchmark path with 37 leaves produces same merged records as before, in 2-3s embed instead of 14s.

**Verification:** Per-conversation embed time drops from ~14s to ~2-3s on LoCoMo fixture.

---

- [ ] U13. **Cross-conversation concurrency in benchmark adapters**

**Goal:** Close R2-07 / R3-P-04. Wrap the per-conversation loop in `asyncio.Semaphore(args.ingest_concurrency)`; default 4. Independent of server changes.

**Requirements:** R11.

**Dependencies:** U1 (URL change must land in adapter at the same time).

**Files:**
- Modify: `benchmarks/oc_client.py` (update endpoint URL to `/api/v1/admin/benchmark/conversation_ingest`)
- Modify: `benchmarks/adapters/locomo.py` (loop → `asyncio.gather` with semaphore)
- Modify: `benchmarks/adapters/conversation.py` (same)
- Modify: `benchmarks/unified_eval.py` (add `--ingest-concurrency` flag, default 4)
- Test: `tests/test_locomo_bench.py` (assert concurrent dispatch under semaphore; mock OCClient to record call timing)

**Approach:**
- `--ingest-concurrency` arg in `unified_eval.py` argparse; thread through to adapter `ingest()` method.
- Default 4 (conservative; embedded Qdrant + local embedder hold up).
- Add a simple progress log line per conversation start/finish so a long run is debuggable.

**Patterns to follow:**
- Existing async patterns in adapter (`for conv in conversations: await ...` becomes `await asyncio.gather(*[_one(c) for c in conversations])`).

**Test scenarios:**
- Happy path: 10-conversation adapter run at concurrency=4 finishes faster than serial (assert via wall-clock or call ordering).
- Edge case: concurrency=1 reproduces serial behavior.
- Edge case: concurrency higher than conversation count is harmless.
- Integration: full LoCoMo adapter run at concurrency=4 produces identical URI mapping as serial run.

**Verification:** Adapter wall-clock drops by ~3-4× at concurrency=4 vs serial.

---

- [ ] U14. **Directory derive bounded concurrency in `_run_full_session_recomposition`**

**Goal:** Close R3-P-02. Pre-existing serial directory `_derive_parent_summary` loop becomes `asyncio.gather` with `asyncio.Semaphore(3)`. Pre-existing but amplified by benchmark path.

**Requirements:** R11.

**Dependencies:** None.

**Files:**
- Modify: `src/opencortex/context/manager.py` (`_run_full_session_recomposition` directory loop ~line 2208)
- Test: `tests/test_context_manager.py` recomposition test (assert parallel derive dispatch via timing or call order)

**Approach:**
- Wrap directory derive in `Semaphore(3)`.
- Reuse `_derive_semaphore` if appropriate; otherwise add a `_directory_derive_semaphore` instance attribute.
- This change touches the production `context_end` path. Run full conversation lifecycle regression.

**Patterns to follow:**
- Same `Semaphore + gather` pattern used elsewhere in the orchestrator.

**Test scenarios:**
- Happy path: 8-directory recomposition completes in ~12s instead of ~36s.
- Edge case: 1 directory → no extra overhead vs serial.
- Regression: production `context_commit` / `context_end` test still passes; ordering of directory updates preserved if it matters.
- Integration: benchmark path single-conversation wall-clock drops further.

**Verification:** Directory derive wall-clock per conversation reduced to ~`ceil(N/3) * derive_latency`.

---

- [ ] U15. **Server-side timeout wrapper**

**Goal:** Close P2 #9 / F9. Wrap the handler in `asyncio.wait_for(..., timeout=540s)` (≈10% headroom under client 600s). On timeout, the cleanup tracker (U4) compensates because U3 catches `CancelledError`.

**Requirements:** AR4, R11.

**Dependencies:** U1 (handler in admin_routes.py), U3 + U4 (cancellation cleanup).

**Files:**
- Modify: `src/opencortex/http/admin_routes.py` (benchmark route wraps handler call in `asyncio.wait_for`)
- Test: `tests/test_http_server.py`

**Approach:**
- Catch `asyncio.TimeoutError` → return 504 with `{detail: "benchmark ingest exceeded server timeout"}`.
- Server timeout < client timeout so client sees 504 rather than connection drop.

**Patterns to follow:**
- Look for any existing `asyncio.wait_for` use in `server.py` / `admin_routes.py` for stylistic alignment.

**Test scenarios:**
- Error path: mock handler to sleep 1s; set timeout to 0.1s; assert 504; assert cleanup tracker compensated (no orphans).
- Happy path: normal handler completes well within timeout.

**Verification:** Server-side request budget bounded; cleanup runs on timeout.

---

- [ ] U16. **Failure-injection + cancellation regression tests**

**Goal:** Close P1 Finding #5 + Round 2 testing gaps. Add a test module that exercises the cleanup tracker, cancellation safety, source versioning conflicts, anchorless caps, and defer-derive parity.

**Requirements:** AR4, AR7, R2.

**Dependencies:** Lands incrementally alongside U3/U4/U5/U7/U8/U10. Listed as a unit so it shows up in plan review and gets right-sized coverage.

**Files:**
- Create: `tests/test_benchmark_ingest_lifecycle.py`
- Modify: `tests/test_http_server.py` (extend `test_04d`)
- Modify: `tests/test_locomo_bench.py` (extend `_OCStub`)

**Approach:**
- One test class per concern: `TestCleanupTracker`, `TestCancellationSafety`, `TestSourceVersioning`, `TestAnchorlessCaps`, `TestDeferDeriveParity`.
- Use `monkeypatch` against orchestrator + manager methods to inject failures.
- Use real Qdrant filter scans in assertions where feasible (cleaner than mocking storage).

**Test scenarios** (covered across child tests, AE-linked where they enforce origin AR):
- Covers AR1: 403 for non-admin.
- Covers AR2: 422 for over-limit payload.
- Covers AR3: idempotent replay, 409 on hash mismatch.
- Covers AR4: every failure path (`Exception` + `CancelledError` + timeout) leaves no orphan.
- Covers AR5: response has no `layer_counts`.
- Covers AR6: `include_session_summary=False` works end-to-end via stub assertion.
- Covers AR7: merged leaves carry post-derive L0/L1 (assert non-truncated).

**Verification:** `uv run python3 -m unittest tests.test_benchmark_ingest_lifecycle -v` passes. `tests.test_http_server.test_04d` still green.

---

### Phase 2 — Behavior-Preserving Cleanup

- [ ] U17. **Per-URI cleanup isolation in `_delete_immediate_families`**

**Goal:** Close R2-15. Single Qdrant `remove_by_uri` failure currently aborts the cleanup loop. Wrap each URI in `try/except + log` so subsequent URIs still get cleaned up.

**Requirements:** AR4.

**Dependencies:** U4 (tracker now consumes this helper).

**Files:**
- Modify: `src/opencortex/context/manager.py:2583` (`_delete_immediate_families`)
- Test: `tests/test_context_manager.py`

**Approach:**
- `for uri in uris: try: remove(uri); except Exception: logger.warning(...)`.
- Keep loop ordering deterministic (URIs already ordered).

**Patterns to follow:**
- U3 logging pattern.

**Test scenarios:**
- Error path: 5 URIs, second one fails to remove; assert remaining 3 still removed.
- Edge case: empty URI list → no-op.

**Verification:** Cleanup loop is failure-isolated; pre-existing call sites benefit too.

---

- [ ] U18. **Filter scan consolidation in benchmark response build**

**Goal:** Close F10 / R2-19 / R3-P-14. Three back-to-back `_load_session_merged_records` style scans in the response build merge into one. Pass the filter result through.

**Requirements:** R10.

**Dependencies:** U4 (tracker provides URIs without re-scanning).

**Files:**
- Modify: `src/opencortex/context/manager.py` (`benchmark_ingest_conversation` response build; `_run_full_session_recomposition` if it returns leaves directly)
- Test: existing recomposition tests should remain green; add a write-counting assertion if useful

**Approach:**
- Have `_run_full_session_recomposition` return its loaded leaves so the benchmark layer doesn't re-load.
- Drop `_session_layer_counts` invocation entirely (already removed from response in U6).

**Patterns to follow:**
- Existing return-value extension pattern.

**Test scenarios:**
- Happy path: response shape unchanged; assert single Qdrant filter scan via instrumented adapter.
- Regression: production path unchanged.

**Verification:** Benchmark response build issues ≤ 1 filter scan post-leaf-write.

---

- [ ] U19. **Adapter shared helper: `benchmarks/adapters/conversation_mapping.py`**

**Goal:** Close R2-24 / R2-28. Extract the repeated `ingest_one_conversation(oc, session_id, messages_by_segment, ingest_method, include_session_summary)` helper used by both LoCoMo and conversation adapters.

**Requirements:** AR8, R8, R9.

**Dependencies:** U11 (default off) + U13 (concurrency wrapper) so the helper captures the post-Phase-1 shape.

**Files:**
- Create: `benchmarks/adapters/conversation_mapping.py`
- Modify: `benchmarks/adapters/locomo.py` (use helper)
- Modify: `benchmarks/adapters/conversation.py` (use helper)
- Test: `tests/test_locomo_bench.py` (assert helper invocation; verify URI mapping unchanged)

**Approach:**
- Helper signature: `async def ingest_conversation_segments(oc, *, session_id, segments, ingest_method, include_session_summary=False, ingest_shape="merged_recompose") -> dict`.
- Both adapters' store branches collapse to a single call.

**Patterns to follow:**
- `benchmarks/oc_client.py` for client interaction patterns.

**Test scenarios:**
- Happy path: LoCoMo adapter URI mapping unchanged.
- Happy path: LongMemEval adapter URI mapping unchanged.
- Regression: existing two adapter tests still green.

**Verification:** `jscpd` duplication on `benchmarks/adapters/` drops below pre-branch 10.83%.

---

- [ ] U20. **Typed response model `BenchmarkConversationIngestResponse`**

**Goal:** Close P2 R2-28. Replace bare `Dict[str, Any]` return with a Pydantic response model. Document that `content` is hydrated.

**Requirements:** AR8, R5.

**Dependencies:** U10 (content hydration), U6 (no `layer_counts`).

**Files:**
- Modify: `src/opencortex/http/models.py` (add `BenchmarkConversationIngestResponse`, `BenchmarkConversationIngestRecord`)
- Modify: `src/opencortex/http/admin_routes.py` (annotate handler return type)
- Modify: `src/opencortex/context/manager.py` (`benchmark_ingest_conversation` returns dict matching the model; consider returning Pydantic instance directly if it doesn't pollute internal API)
- Test: `tests/test_http_server.py` (response body validates against model)

**Approach:**
- Model fields: `status: Literal["ok"]`, `session_id: str`, `source_uri: str`, `summary_uri: Optional[str]`, `records: list[BenchmarkConversationIngestRecord]`, `meta: dict`.
- Record fields: `uri`, `session_id`, `msg_range`, `source_uri`, `recomposition_stage`, `abstract`, `overview`, `content`, `meta`.

**Patterns to follow:**
- Existing Pydantic response models in `src/opencortex/http/models.py`.

**Test scenarios:**
- Happy path: response validates against model.
- Regression: adapter consumers unchanged.

**Verification:** Schema is closed; future field drift triggers Pydantic errors instead of silent shape changes.

---

- [ ] U21. **Observability via `StageTimingCollector`**

**Goal:** Close R2-22 / R3-P-09 / F26. Wrap each phase of `benchmark_ingest_conversation` with `StageTimingCollector` if it exists in the codebase, otherwise add lightweight per-phase `time.perf_counter` measurements logged structured. Surface in response `meta.timings`.

**Requirements:** R11.

**Dependencies:** U18 (final response shape stable).

**Files:**
- Modify: `src/opencortex/context/manager.py`
- Modify: `src/opencortex/http/models.py` (add `meta.timings: dict[str, float]` to response model from U20)
- Test: `tests/test_http_server.py` (assert `meta.timings` keys exist)

**Approach:**
- Phases: `source_persist`, `entry_build`, `leaf_batch_write`, `recomposition`, `derive_complete`, `summary`, `response_build`.
- Use existing `StageTimingCollector` if present; otherwise inline `time.perf_counter` deltas with structured log.

**Patterns to follow:**
- Any existing `StageTimingCollector` usage in `src/opencortex/`.

**Test scenarios:**
- Happy path: response `meta.timings` contains all phase keys with positive floats.

**Verification:** A failed long-running benchmark surfaces timing breakdown without grep-spelunking server logs.

---

## System-Wide Impact

- **Interaction graph:** Affects HTTP layer (route move), orchestrator (`add_batch` facade), ContextManager (cleanup tracker, source versioning, derive parity, anchorless caps), CortexFS write (only via existing fire-and-forget — no behavior change), Qdrant adapter (no changes), benchmark adapters (concurrency wrapper, helper extraction), benchmark CLI (new flag).
- **Error propagation:** New `SourceConflictError` raised inside ContextManager, mapped to HTTP 409 at admin route. `asyncio.TimeoutError` from server-side wait_for mapped to HTTP 504. `CancelledError` propagates after compensation.
- **State lifecycle risks:** Cleanup tracker covers source / merged / directory / summary. Per-URI failure isolation prevents partial cleanup. Pre-ingest purge prevents stale-URI residue from prior aborted runs.
- **API surface parity:** No public conversation API changes. Benchmark endpoint URL changes from `/api/v1/benchmark/conversation_ingest` to `/api/v1/admin/benchmark/conversation_ingest` — adapter updated in same PR. No external callers exist (benchmark-only).
- **Integration coverage:** Production `context_commit` / `context_end` regression tests must pass — U4 (return_created kwarg), U7 (anchorless caps + reduced constants), U14 (directory derive concurrency) all touch shared code. Run full conversation lifecycle suite.
- **Unchanged invariants:** Production conversation lifecycle behavior; merge follow-up; session summary semantics; benchmark scoring methodology; MCP tool surface; `_orchestrator.add` per-record API.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Adding `add_batch` breaks single-record `add` callers | Parity test in U12; behavior diff exposed by existing test suite. |
| `_run_full_session_recomposition` `return_created_uris=True` kwarg unintentionally changes production behavior | Default `False` keeps existing call sites unchanged; explicit production lifecycle regression run. |
| Anchorless cap reduction (1M → 6K tokens) splits clusters too aggressively for production fixtures | Constants tunable; capture baseline distribution before merge; revert constant bump if regression. |
| Defer-derive scheduling adds wall-clock and dominates the perf gains | Combined with `add_batch` + concurrency, net wall-clock still drops vs current branch (verified by Phase 1 measurements). |
| Source-versioning 409 breaks LoCoMo adapter that may legitimately re-run same `session_id` | Idempotent path (same hash) is the common case; conflict only on actual transcript divergence — adapter responsibility to rotate `session_id` between runs with different transcripts. |
| Cross-conversation concurrency saturates local embedder / Qdrant | Default 4; configurable via CLI; instrumentation in U21 surfaces saturation. |
| Phase 2 cleanup landed in same PR inflates diff and slows review | Phase 2 may be split into a follow-up PR if Phase 1 alone exceeds 1500 LOC review budget. |

---

## Phased Delivery

### Phase 1 (must merge before branch is shippable)

U1 → U2 → U3 → U4 → U5 → U6 → U7 → U8 (depends on U12) → U9 → U10 → U11 → U12 → U13 (depends on U1) → U14 → U15 → U16

Parallelizable groups:
- {U1, U2}: route + bounds
- {U3, U4}: cancellation + tracker (sequential, U3 first)
- {U5, U6}: versioning + scope leak
- {U7, U9, U10}: independent semantic fixes
- {U12, U8}: batch then derive (sequential)
- {U11, U13, U14, U15}: feasibility wins

U16 lands incrementally as each unit's tests come up; final consolidation pass before merge.

### Phase 2 (behavior-preserving, ship in same PR if review budget allows)

U17 → U18 → U19 → U20 → U21

If Phase 1 PR is already large, Phase 2 ships as a follow-up PR before benchmark suite expansion.

---

## Documentation / Operational Notes

- Update PR description on `feat/benchmark-offline-conv-ingest` after Phase 1 lands: include LoCoMo F1 / J-Score re-run numbers (expected to recover from 0.49→0.33 toward production parity per R3-RC-01 prediction).
- Add `docs/solutions/best-practices/` entry candidates after merge (REVIEW Section 6): session-lock lifecycle, partial-write failure cleanup, benchmark-only HTTP route pattern.
- `.gitignore` `.DS_Store` and `ai-agents-for-beginners/` (untracked currently — REVIEW Section 2 noted these).
- Update `MEMORY.md` 2026-03-18 benchmark baseline once new measurements land.

---

## Sources & References

- **Origin requirements:** `docs/brainstorms/2026-04-23-benchmark-offline-conversation-ingest-requirements.md`
- **Code review (4 rounds):** `.context/compound-engineering/ce-code-review/20260424-152926-6301c860/REVIEW.md`
- **Round 1 persona JSONs:** `.context/compound-engineering/ce-code-review/20260424-152926-6301c860/{security,api-contract,reliability,adversarial,performance,kieran-python,correctness,testing,maintainability,project-standards}.json`
- **Round 2 persona JSONs:** `.context/compound-engineering/ce-code-review/20260424-152926-6301c860/round2/`
- **Round 3 persona JSONs:** `.context/compound-engineering/ce-code-review/20260424-152926-6301c860/round3/`
- **Related learnings:**
  - `docs/solutions/best-practices/single-bucket-scoped-probe-2026-04-16.md`
  - `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md`
- **Related plans:**
  - `docs/plans/2026-04-25-002-feat-memory-benchmark-suite-three-layers-plan.md` (downstream consumer of the corrected benchmark path)
- **Branch:** `feat/benchmark-offline-conv-ingest` (HEAD `27f857a`)
