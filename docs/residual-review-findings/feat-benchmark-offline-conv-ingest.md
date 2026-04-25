# Residual Review Findings — feat/benchmark-offline-conv-ingest

**Source:** ce-code-review run `20260425-093102-b5a311de`
**Mode:** autofix (LFG pipeline)
**Plan:** `docs/plans/2026-04-25-003-fix-benchmark-conversation-ingest-review-fixes-plan.md`
**Verdict:** Ready with fixes
**Branch HEAD:** `a2d2188`
**Run artifact:** `.context/compound-engineering/ce-code-review/20260425-093102-b5a311de/REVIEW.md`

This file is the durable handoff for residuals because no GitHub PR existed at the time of the review (and `gh` was not authenticated). When a PR is opened, copy these items into the PR body and delete this file.

---

## Residual Review Findings

### Must address before merge

- **[P1][gated_auto] `src/opencortex/context/manager.py:1606` — Sibling-task race in derive gather (F1 / REL-01 / ADV-001)**
  3-reviewer agreement (correctness, reliability, adversarial). `await asyncio.gather(*[task for _, task in derive_tasks])` propagates first exception but does not cancel siblings. The except handler runs `cleanup.compensate()` which removes URIs; surviving derive tasks then call `_complete_deferred_derive` on those removed URIs and unconditionally invoke `fs.write_context`, creating orphan CortexFS subtrees.
  *Fix:* wrap in try/except that cancels remaining tasks then awaits with `return_exceptions=True` before re-raising.

- **[P2][gated_auto] `src/opencortex/context/manager.py:2018-2046` — Anchorless cap split-on-seed bug (ADV-002)**
  The U7 fix only stops APPEND when caps are exceeded; it does not split a single oversized seed entry. With U2 payload caps allowing 64 KB content per message → ~16K tokens, a single anchorless leaf above 6K tokens passes through and blows `_derive_parent_summary`'s context window.
  *Fix:* check seed-entry size before initializing `current` and split if it already exceeds caps.

- **[P2][gated_auto] `src/opencortex/context/manager.py:2736` — Per-call directory derive semaphore (PERF-001 / KP-09)**
  `_DIRECTORY_DERIVE_CONCURRENCY=3` semaphore is created **per call** rather than as instance attr. With U13 default `--ingest-concurrency=4`, total in-flight directory-derive LLMs = 4×3 = 12 (plus global merged-leaf semaphore=3 + per-conv summaries).
  *Fix:* hoist to `self._directory_derive_semaphore` in `__init__`.

### Should address before benchmark suite expansion

- **[P2][manual] `src/opencortex/context/manager.py:1480` — Pre-ingest purge unimplemented (TEST-003 / F5 / ADV-007)**
  Plan U5 mentioned R2-17 pre-ingest purge but only the transcript-hash short-circuit landed. If a prior run failed mid-cleanup leaving partial leaves, the next replay short-circuits as "idempotent" returning the partial set. No `run_complete` marker on `source_uri.meta` to disambiguate fresh-write vs partial-recovery.
  *Fix options:* implement the pre-ingest purge OR add a `run_complete: true` marker on source meta and treat hash-match without that marker as "needs full re-ingest".

- **[P2][gated_auto] `src/opencortex/context/manager.py:2692` — Inner cleanup escapes outer tracker (REL-02)**
  Directory URIs created before recompose failure escape the cleanup tracker when `raise_on_error=True`. The inner `_delete_immediate_families` is `contextlib.suppress(Exception)`-wrapped, so partial failures silently leak orphan directory records.
  *Fix:* propagate created URIs to the outer cleanup tracker even on the raise_on_error path.

- **[P2][gated_auto] `benchmarks/adapters/locomo.py:586` + `conversation.py:471` — Adapter cancellation cascade (REL-04)**
  `asyncio.gather(_process_one, return_exceptions=False)` lets one CancelledError cancel all sibling conversations. Inner `except Exception` does not catch CancelledError → silent partial state across the whole batch.
  *Fix:* use `return_exceptions=True` and inspect each result.

- **[P2][gated_auto] `src/opencortex/context/manager.py:1499` — Idempotent-hit drops summary_uri (api-contract-005 / F4)**
  Idempotent short-circuit always returns `summary_uri: None` even when first ingest produced one. Asymmetric vs normal-success path. No adapter consumes summary today, but contract drift.
  *Fix:* read existing summary_uri from session and pass it through.

- **[P2][manual] `src/opencortex/orchestrator.py:5237` — Public facade no defense-in-depth admin check (api-contract-007)**
  `MemoryOrchestrator.benchmark_conversation_ingest` accepts arbitrary `tid`/`uid` with no policy enforcement; admin gate only at HTTP layer. Docstring says "non-HTTP callers are trusted by construction" — defensible today, fragile if anything else ever calls it.
  *Fix:* add `is_admin()` check inside the facade as defense-in-depth.

### Nice-to-have follow-ups

- **[P3][gated_auto] `src/opencortex/context/manager.py:1130` — Hash transcript list ordering (ADV-006)**
  `_hash_transcript` orjson sorts dict keys recursively but list ordering is preserved. Reorderable list values like `time_refs` cause false 409 conflicts on benign replays.
  *Fix:* recursively sort list values during hash normalization.

- **[P3][manual] `benchmarks/adapters/locomo.py:581` — Cross-conv shared state non-determinism (F3 / ADV-003)**
  `self._lme_session_to_uri` non-deterministic when LongMemEval `haystack_session_ids` repeat across items under U13 concurrency. No corruption (GIL-atomic) but unstable answer-key mapping between runs.
  *Fix:* serialize the check-then-set, or use deterministic ordering before assignment.

- **[P2][manual] `src/opencortex/http/admin_routes.py` (URL change) — No 410-Gone migration shim (api-contract-001)**
  Old endpoint `/api/v1/benchmark/conversation_ingest` returns 404. No CHANGELOG / migration breadcrumb.

- **[P2][manual] `src/opencortex/http/admin_routes.py` — Inconsistent error envelope shape (api-contract-004)**
  403/504 use string `detail`, 409 uses dict `detail`, pre-existing bench-collections uses `{error: ...}`. Three styles in one file.

- **[P2][manual] `src/opencortex/context/manager.py` (recomposition entries) — TypedDict needed (KP-08 / R2-15)**
  Pre-existing P2 carryover: recomposition entries should be a TypedDict; current `Dict[str, Any]` lets shape drift across construction sites.

- **[P3][manual] `src/opencortex/context/manager.py:1265` — Method-name asymmetry (M1)**
  ContextManager method `benchmark_ingest_conversation` (verb-first) vs orchestrator/HTTP/client `benchmark_conversation_ingest` (noun-first). R2-26 only partially addressed.

- **[P3][manual] `src/opencortex/context/manager.py:2640` — Tri-state return contract (M2)**
  `_run_full_session_recomposition` dual-mode return contract is non-obvious. Recommend split into two methods (always-list vs always-None).

- **[P3][manual] `src/opencortex/context/manager.py:103` — Cleanup tracker private-API coupling (M3)**
  `_BenchmarkRunCleanup.compensate` reaches into `manager._orchestrator.remove`. Pass a bound `remove_callable` at construction instead.

- **[P3][manual] `src/opencortex/context/manager.py` + `admin_routes.py` (11 sites) — Inline REVIEW tags (M4)**
  Inline tags like `U5`, `R2-04`, `R3-P-12` point to a CE artifact path future readers won't have. Prose around tags is load-bearing; bare tags are noise. Move to commit trailers.

- **[P2][manual] `src/opencortex/http/models.py:9` — stdlib json vs orjson convention (KP-01)**
  Project memory notes orjson migration; new `field_validator` uses stdlib `json.dumps`. Migrate for consistency, but watch `ensure_ascii` differences.

- **[P3][manual] tests/test_benchmark_ingest_lifecycle.py — Test coverage gaps (TEST-001/002/005/008)**
  - TEST-001: AR7 derive parity test asserts scheduling but cannot verify post-derive L0/L1 quality (no LLM in `_test_app_context`).
  - TEST-002: U5 backward-compat branch (existing source with no transcript_hash) untested.
  - TEST-005: Anchorless-respects-cap branch untested. ADV-002 found the off-by-one because no test pinned this contract.
  - TEST-008: Cross-conv concurrency (U13) has no race or ordering test.

- **[P3][manual] Adversarial small-radius items (ADV-005, KP-02/03/04/05/07)**
  Race between gather child cancellation and compensate FS-write completion (ADV-005); style and small refactor items in kieran-python persona output.

### Advisory (no action required)

- **[P3][advisory] `src/opencortex/http/models.py:342-358` — Worst-case admin DoS (SEC-1)**
  200 segments × 2000 msgs × 64 KB content = ~25.6 GB worst-case body before Pydantic runs. Admin-only, low priority.

- **[P3][advisory] `src/opencortex/http/models.py:316-331` — meta validator nested dict DoS (SEC-2)**
  `json.dumps` recurses on Dict[str, Any] before byte budget check. Admin-only RecursionError.

---

## Applied This Run (autofix)

| Finding | File | Change |
|---|---|---|
| F2 | `src/opencortex/context/manager.py` | 3 bare `return` statements → `return [] if return_created_uris else None`. |
| F6 | `src/opencortex/context/manager.py` | Added `except asyncio.CancelledError` clause to `_benchmark_ingest_direct_evidence` mirroring U3 fix. |
| KP-10 | `src/opencortex/context/manager.py` | `{u:t for u,t in results}` → `dict(results)` in `_hydrate_record_contents`. |

Committed as `a2d2188 fix(review): apply autofix feedback`. All 95 tests pass.

---

## Learnings & Past Solutions (informational)

Two existing entries cited:
- `docs/solutions/best-practices/single-bucket-scoped-probe-2026-04-16.md` — applies to U6 (drop layer_counts) and adapter URI mapping.
- `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md` — applies to U20 typed response model if benchmark response gains retrieval-shaped output.

7 new candidates for `/ce-compound` capture after merge (no existing entries):
1. Run-scoped cleanup tracker / compensation pattern (highest priority, broadest applicability)
2. Source/transcript hash-based idempotency with 409 (highest priority)
3. CancelledError-vs-Exception rollback discipline
4. Defer-derive lifecycle / scheduling completion contract
5. Benchmark-only HTTP route convention (admin-gated, public facade, server timeout)
6. Bounded-concurrency LLM derive pattern
7. Anchor-clustered recomposition cap discipline (anti-pattern: 1_000_000 sentinel that disables a cap)
