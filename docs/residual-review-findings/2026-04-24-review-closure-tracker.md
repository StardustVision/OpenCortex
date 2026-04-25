# Closure Tracker — REVIEW.md `20260424-152926-6301c860`

**Source:** `.context/compound-engineering/ce-code-review/20260424-152926-6301c860/REVIEW.md`
**Original verdict (2026-04-24):** Not ready (P0 + 14 P1)
**Current verdict (2026-04-25):** All Phase 1 verification gates passed; long-tail items tracked below.
**PRs that closed items here:** #3, #4, #5, #6 (all merged into master)

## Status legend

| Symbol | Meaning |
|---|---|
| ✅ | Done — closed in a merged PR; commit cited |
| ⚠️ | Partial — some aspect closed, some still open; explained inline |
| ❌ | Not done — open |
| ➖ | Deferred by design — explicitly deferred to follow-up; reason cited |
| 🔄 | N/A — superseded by a later finding or no longer applicable |

---

## Closure summary

| Bucket | Total | ✅ | ⚠️ | ❌ / ➖ |
|---|---|---|---|---|
| Round 1 findings (§4, F1–F26) | 26 | 18 | 4 | 4 |
| Round 2 P1 (§12, R2-01–R2-09) | 9 | 8 | 1 | 0 |
| Round 2 P2 (§13, R2-10–R2-28) | 19 | 7 | 2 | 10 |
| Round 2 P3 (§14, R2-29–R2-37) | 9 | 1 | 0 | 8 |
| Round 3 store-perf (§20.3, R3-P-*) | 10 | 5 | 0 | 5 |
| Round 3 recall-perf (§20.4, R3-RP-*) | 2 | 0 | 0 | 2 |
| Round 3 recall-correctness (§20.5–6, R3-RC-*) | 9 | 7 | 0 | 2 |
| Round 4 consensus (§22) | 8 | 8 | 0 | 0 |
| Round 4 P2/P3 add-ons (§23) | 13 | 5 | 1 | 7 |
| Pre-existing (§5) | 6 | 2 | 0 | 4 |
| Round 2 Residual Risks (§15) | 7 | 1 | 0 | 6 |
| Round 2 Testing Gaps (§16) | 8 | 1 | 2 | 5 |
| Design-Pattern Refactor (§25, Phases 1–7) | 7 | 3 | 0 | 4 |
| **Totals** | **133** | **66** | **10** | **57** |

Note: Many items overlap across rounds (e.g., R3-P-12 = R2-01; R2-09 ↔ F6; R2-15 ↔ F13). Each row counts the item once in its earliest-introduced section; overlap notes appear inline. Round 4 mostly confirms prior findings rather than introducing new ones.

---

## Round 1 — Section 4 Findings (F1–F26)

### P0 — must fix before merge

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| F1 | ✅ | `http/server.py:472` → `http/admin_routes.py` | Admin gate on benchmark endpoint | PR #3 / U1 (`e50810b`) |

### P1 — strongly recommended before merge

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| F2 | ✅ | `http/models.py:294` | Pydantic payload bounds (segments / messages / content / meta) | PR #3 / U2 (`e50810b`) |
| F3 | ✅ | `context/manager.py:1302` | `except Exception` doesn't catch `CancelledError` | PR #3 / U3 (`4cf2c57`) + autofix F6 (`a2d2188`) for symmetric direct_evidence path |
| F4 | ✅ | `context/manager.py:1214` | Same-session concurrent ingest mixes transcripts | PR #3 / U5 (`ddae6b3`) — transcript hash + 409 |
| F5 | ✅ | `context/manager.py:1302` | No failure-injection test for the rollback branch | PR #3 / U16 (`d8ebcce`) — `tests/test_benchmark_ingest_lifecycle.py` |

### P2 — state and cleanup

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| F6 | ✅ | `context/manager.py:1302` | `_run_full_session_recomposition` directory URIs not in `cleanup_uris` | PR #4 / REL-02 (`8221bea`) — `RecompositionError` carries created URIs |
| F7 | ⚠️ | `context/manager.py:1214` | `source_uri` written before try, no rollback | Acknowledged by U4/U5 — source intentionally kept for idempotent retry; F5 torn-replay marker handles asymmetry |
| F8 | ✅ | `context/manager.py:1271` | Re-ingest with different `msg_range` orphans old leaves | PR #4 / F5 (`8221bea`) — `_purge_torn_benchmark_run` drops stale leaves under same source |

### P2 — timeouts

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| F9 | ✅ | `http/server.py:473` | Server-side timeout wrapper | PR #3 / U15 (`e50810b`) — `asyncio.wait_for(540s)` in `admin_routes.py` |

### P2 — performance

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| F10 | ✅ | `context/manager.py:1286` | Three back-to-back filter scans in response build | PR #3 / R3-P-14 enhancement; reduced to single scan |
| F11 | ➖ | `context/manager.py:1243` | Serial embed for merged leaves; `embed_batch` ignored | **Deferred** — same as R2-08 / R3-P-05 (U12 add_batch). Throughput recovered by U13 cross-conv concurrency. |
| F12 | ✅ | `benchmarks/oc_client.py:239` | `include_session_summary` defaults True; adapter never overrides | PR #3 / U11 (`5ef4583`) — adapter passes False |

### P2 — code quality

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| F13 | ✅ | `context/manager.py:1302` | `with contextlib.suppress(Exception)` swallows cleanup errors silently | PR #3 / U3 — added `logger.warning(..., exc_info=True)` on per-URI failures |
| F14 | ➖ | `context/manager.py:1168` | `ContextManager` already 3495 lines; benchmark code adds to bloat | **Deferred** — equivalent to §25 Phase 3 (Service Layer). Not done. |
| F15 | ✅ | `context/manager.py:1118` | Recomposition entry shape across 3 sites; introduce TypedDict | PR #5 / U5 (`a39220f`) — `RecompositionEntry` TypedDict |
| F16 | ⚠️ | `context/manager.py:1168` | Naming inconsistency: `benchmark_ingest_conversation` vs `benchmark_conversation_ingest` | Partial — orchestrator facade renamed in U1; ContextManager method still `benchmark_ingest_conversation` (R2-26 / M1 confirms unfixed) |
| F17 | ✅ | `context/manager.py:1174` | Type weakness on `segments: List[List[Dict[str, Any]]]` | PR #3 / U2 — Pydantic `BenchmarkConversationMessage` validates at HTTP boundary |

### P2 — testing coverage

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| F18 | ⚠️ | `context/manager.py:1278` | `include_session_summary=False` branch untested | Partial — `tests/test_locomo_bench.py` asserts adapter passes False (PR #3); end-to-end no-summary HTTP test still missing (PR #5 review T-007) |
| F19 | ❌ | `context/manager.py:1204` | Empty segments / all-empty-content early return untested | Not done; only logger.info added in PR #3 |
| F20 | ❌ | `tests/test_locomo_bench.py:253` | Adapter store-path test doesn't validate message shape / event_date / time_refs propagation | Not done |
| F21 | ⚠️ | `tests/test_http_server.py:757` | `test_04d` doesn't check per-record `source_uri`, `summary_uri`, `layer_counts` | Partial — `summary_uri` and absence of `layer_counts` now asserted (PR #3); `source_uri` per-record still not checked |
| F22 | ❌ | `context/manager.py:1051` | `_benchmark_segment_meta`, `_benchmark_recomposition_entries`, `_export_memory_record` no direct unit tests | Not done |

### P3 — optional

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| F23 | ✅ | `context/manager.py:1126` | Segment-level meta merged onto each message entry incorrectly | PR #3 / U9 (`5ef4583`) — dict-merge inverted |
| F24 | ❌ | `context/manager.py:1042` | Names: `_persist_rendered_conversation_source`, `_benchmark_segment_meta`, `_export_memory_record` are vague | Not done |
| F25 | ❌ | `benchmarks/oc_client.py:322` | Document the 600s + `retry_on_timeout=False` contract in docstring | Not done |
| F26 | ⚠️ | `context/manager.py:1170` | `benchmark_ingest_conversation` has no structured logging | Partial — added log on success/idempotent (PR #3+#4); structured timing metrics still missing (= R2-22 / R3-P-09) |

---

## Round 2 — Section 12 P1 (R2-01 – R2-09)

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| R2-01 | ✅ | `context/manager.py:1266` | `defer_derive=True` permanently truncates L0/L1 (LoCoMo F1 root cause) | PR #3 / U8 (`f17c7d0`) — schedule + await `_complete_deferred_derive` |
| R2-02 | ✅ | `context/manager.py:1181` | `_session_locks` / `_session_activity` leak per ingest call | PR #3 / U4 (`4cf2c57`) — cleanup tracker; ingest within `async with lock:` releases on exit |
| R2-03 | ✅ | `context/manager.py:1126` | dict-merge order makes `_benchmark_segment_meta` dead code | PR #3 / U9 (`5ef4583`) |
| R2-04 | ✅ | `context/manager.py:1365` | Cross-tenant leak via `layer_counts` in response | PR #3 / U6 (`5ef4583`) — field removed |
| R2-05 | ✅ | `context/manager.py:1214` | Transcript freeze (`_persist_rendered_conversation_source` returns existing without diffing) | PR #3 / U5 + PR #4 / F5 (`8221bea`) — hash mismatch raises 409; torn-replay purges |
| R2-06 | ⚠️ | `context/manager.py:1302` + `orchestrator.py:2898` | Qdrant ↔ CortexFS double-write non-atomic; cleanup asymmetric | Partial — in-memory map sidesteps write-time race for content (U10); per-URI cleanup isolation (REL-04). Underlying double-write atomicity not addressed (no 2PC). |
| R2-07 | ✅ | `benchmarks/adapters/{locomo,conversation}.py` | Cross-conversation serial ingest; R11 unmet | PR #3 / U13 (`f17c7d0`) — `--ingest-concurrency` + `asyncio.Semaphore` + `gather` |
| R2-08 | ➖ | `context/manager.py:1243` + `orchestrator.py` | `CachedEmbedder.embed_batch` exists but new path doesn't use it | **Deferred** — orchestrator-layer `add_batch` change too invasive for the review-fix sequence. Plan 003 §"Deferred to Follow-Up" cites this. |
| R2-09 | ✅ | `context/manager.py:1168` | `_orchestrator.add` internal side-effect failures escape cleanup tracker | PR #4 / REL-02 (`8221bea`) — `RecompositionError` carries partial URIs back to outer tracker |

---

## Round 2 — Section 13 P2 (R2-10 – R2-28)

### Architecture / Design

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| R2-10 | ✅ | `http/server.py:472` → `http/admin_routes.py` | Admin endpoint in business router | PR #3 / U1 (`e50810b`) |
| R2-11 | ❌ | `context/manager.py:1168` | SRP violation: `benchmark_ingest_conversation` packs 6 responsibilities | Not done — = §25 Phase 3 Service Layer |
| R2-12 | ❌ | `context/manager.py:1302` | Cleanup ownership scattered across 4 layers; no transaction boundary | Not done — partially addressed by `_BenchmarkRunCleanup` but not unified |

### Correctness / data integrity

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| R2-13 | ✅ | `_build_anchor_clustered_segments` | Empty-anchor entries bypass `within_caps`, blow LLM context window | PR #3 / U7 (`00f2a1e`) — anchorless branch checks caps; ADV-002 fix (PR #3 patch `059a693`) added seed split |
| R2-14 | ❌ | `context/manager.py:1118` | Cross-input-session merge via shared `msg_index` | Not done — same root as R3-RC-02 |
| R2-15 | ✅ | `context/manager.py:2583` | `_delete_immediate_families` aborts on first failure (orphans rest) | PR #4 / REL-04 (`8221bea`) — per-URI try/except |
| R2-16 | ❌ | `context/manager.py:1250` | `source_uri: ""` empty string semantically undefined | Not done |
| R2-17 | ✅ | `context/manager.py:1268` | Re-ingest different msg_range orphans old merged-leaf URIs | PR #4 / F5 (`8221bea`) — `_purge_torn_benchmark_run` |
| R2-18 | ❌ | `storage/qdrant/adapter.py` | Concurrent same-URI add can produce Qdrant duplicate points (TOCTOU) | Not done — would need point ID via `uuid5(NAMESPACE_URL, uri)` |

### Performance

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| R2-19 | ✅ | `context/manager.py:1286` + `:2135` | Two bit-identical filter scans (refinement of F10) | PR #3 (Phase 1 hydration consolidation) — single scan via `_run_full_session_recomposition` returning records |
| R2-20 | ✅ | `context/manager.py` | `_RECOMPOSE_CLUSTER_MAX_*` = 1_000_000 (effectively no cap) | PR #3 / U7 — reduced to 6_000 / 60 |
| R2-21 | ❌ | `context/manager.py` `_generate_session_summary` | 1-directory case has no short-circuit (still 1 LLM + 2 scans + 1 add) | Not done |
| R2-22 | ❌ | `context/manager.py:1170` | New endpoint zero observability: no `StageTimingCollector`, no metrics | Not done — = R3-P-09; = §25 Phase missing |
| R2-23 | ❌ | `context/manager.py:1041` | `_persist_rendered_conversation_source` redundant `fs.write_context` (orchestrator already schedules) | Not done |

### Duplication / naming / patterns

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| R2-24 | ❌ | `benchmarks/adapters/{conversation,locomo}.py` | 169 lines duplicate (10.83% jscpd); shared helper missing | Not done — = §25 Phase 7 Shared Adapter Helper |
| R2-25 | ❌ | `context/manager.py` | `_export_memory_record` is hand-written projection; `MemorySearchResultItem` exists | Not done |
| R2-26 | ⚠️ | `context/manager.py:1168` | Naming asymmetry: ContextManager method should be `benchmark_conversation_ingest` | Partial — facade + URL renamed (U1); ContextManager method still old name |
| R2-27 | ❌ | `tests/test_locomo_bench.py` `_OCStub` | Hand-maintained test stub vs `AsyncMock(spec_set=OCClient)` | Not done |
| R2-28 | ❌ | `http/models.py:288` | Bare `Dict[str, Any]` response; missing Pydantic envelope | Not done — = §25 Phase 6 DTO |

---

## Round 2 — Section 14 P3 (R2-29 – R2-37)

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| R2-29 | ❌ | `context/manager.py:1118` | `immediate_uris: []` / `superseded_merged_uris: []` shape tax in benchmark entries | Not done — TypedDict (U5/PR #5) keeps fields Required; future cleanup candidate |
| R2-30 | ❌ | `context/manager.py:522` | `_export_memory_record` 18-line static helper, single call site, could inline | Not done |
| R2-31 | ✅ | `benchmarks/unified_eval.py:906` | CLI `--ingest-method` help text uses internal term "merged-leaf" | PR #3 (`5ef4583`) — simplified to `store (benchmark-only offline ingest)` |
| R2-32 | ❌ | `context/manager.py:1052` | `speaker` field not aggregated into merged-leaf top-level | Not done |
| R2-33 | ❌ | `benchmarks/adapters/{conversation,locomo}.py` | `turn_id` naming + 0/1-based offset divergence between adapters | Not done |
| R2-34 | ❌ | `context/manager.py` | New helpers (`_benchmark_*`, `_export_*`) deviate from `verb_noun` convention | Not done |
| R2-35 | ❌ | `context/manager.py:1250` | `msg_range` double-encoded (URI + meta) — drift risk | Not done — advisory |
| R2-36 | ❌ | `context/manager.py:1204` | Empty `normalized_segments` returns silently (no warning) | Partial — added `logger.info` (PR #3) but not warning level |
| R2-37 | ❌ | `context/manager.py:2208` | N+1 keywords patch: filter+update when ID known locally (pre-existing) | Not done |

---

## Round 3 — Section 20.3 store-perf (R3-P-*)

### Mapped to Round 1/2

| # | Status | Maps to | Closed by |
|---|---|---|---|
| R3-P-12 | ✅ | = R2-01 | PR #3 / U8 — defer-derive parity (LoCoMo F1 root cause) |
| R3-P-04 | ✅ | = R2-07 | PR #3 / U13 — cross-conv concurrency |
| R3-P-05 | ➖ | = R2-08 | Deferred (U12 add_batch) |
| R3-P-14 | ✅ | = R2-19 / F10 refinement | PR #3 |

### New in Round 3

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| R3-P-02 | ✅ | `manager.py:2187` | 8 directory `_derive_parent_summary` calls serial (36s); `Semaphore(3)` → 12s | PR #3 / U14 (`00f2a1e`) + PR #3 patch (`059a693`) — instance-scoped semaphore |
| R3-P-01 | ❌ | `manager.py:1183` | Per-session lock held for entire 56s ingest; serializes same-session concurrent requests | Not done |
| R3-P-03 | ❌ | `manager.py:2259` | N+1 keywords patch (overlap with R2-37) | Not done |
| R3-P-06 | ❌ | `orchestrator.py:2510` | `_sync_anchor_projection_records` 2 stale-filter scans on empty defer-derive projection | Not done |
| R3-P-09 | ❌ | `manager.py:1168` | Endpoint zero observability (= R2-22) | Not done |
| R3-P-13 | ➖ | `orchestrator.py:2762` | Local embedder is CPU-bound; `run_in_executor` no concurrency benefit; needs `embed_batch` | Deferred (depends on U12 add_batch) |
| R3-P-07 | ❌ | `cortex_fs.py:1170` | CortexFS write_context 4-worker thread pool bottleneck (advisory P3) | Not done |

---

## Round 3 — Section 20.4 recall-perf (R3-RP-*)

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| R3-RP-01 | ➖ | — | Rerank artificially fast on truncated abstracts; quality penalty | Resolved by R3-P-12 / R2-01 (defer-derive parity) — once L0/L1 are LLM-derived, rerank quality recovers. Marker only; no separate fix needed. |
| R3-RP-02 | ❌ | anchor parallel search | Anchor parallel search wastes 4-10ms on near-empty anchor index | Not done — advisory; net effect microscopic |

---

## Round 3 — Section 20.5–6 recall-correctness (R3-RC-*)

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| R3-RC-01 | ✅ | (composite of R2-01 + R2-03 + defer-derive) | Recall systematically degraded on benchmark merged leaves (P1, conf 100) | PR #3 / U8 + U9 (`f17c7d0` + `5ef4583`) — closing R2-01 + R2-03 closes the composite |
| R3-RC-02 | ❌ | `manager.py:1118` | Cross-input-session merge via single `msg_index` stream (= R2-14) | Not done |
| R3-RC-03 | ❌ | `manager.py:1372` | `source_uri=None` filter degrades to session-wide scan | Not done |
| R3-RC-04 | ✅ | `manager.py:1018` | Conversation source freeze (= R2-05 recall-side) | PR #3 / U5 — hash-based versioning |
| R3-RC-05 | ✅ | `manager.py:1126` | dict-merge bug latent until message meta drops time_refs (= R2-03 condition) | PR #3 / U9 — root R2-03 fix covers this |
| R3-RC-06 | ✅ | `manager.py:522` `_export_memory_record` | `content` field always empty | PR #3 / U10 (`5ef4583`) — in-memory hydration |
| R3-RC-07 | ❌ | `_starting_point_probe` | Session summary records may slip into recall candidates | Not done — advisory P3 |
| R3-RC-08 | ❌ | `tests/test_http_server.py` | Test asserts don't include `recomposition_stage='benchmark_offline'` as legal value | Not done |
| R3-RC-09 | ❌ | `manager.py:922` `_merged_leaf_uri` | Adapter relies on URI lex sort = msg_range order; no test pins zero-padding format | Not done |

---

## Round 4 — Section 22 consensus

Round 4 mostly **confirms** prior P0/P1; entries below already counted in Round 1–3 closure status.

| Round 4 row | Maps to | Status |
|---|---|---|
| Admin gate missing | F1 / R2-10 | ✅ |
| Request bounds | F2 | ✅ |
| Cancellation rollback | F3 | ✅ |
| Source/payload pollution | F4 / R2-05 | ✅ |
| Summary failure → orphan directories | F6 | ✅ |
| `layer_counts` scope leak | R2-04 | ✅ |
| Anchorless caps | R2-13 | ✅ |
| Benchmark store still serial-dominated | R2-08 / R3-P-05 | ➖ (U12 deferred; offset by U13) |

---

## Round 4 — Section 23 P2/P3 add-ons

| Row | Status | Title | Closed by |
|---|---|---|---|
| R4-P2-1 | ✅ | Route placement + private member access | PR #3 / U1 + facade |
| R4-P2-2 | ❌ | SRP: extract `BenchmarkConversationIngestService` | Not done — = §25 Phase 3 |
| R4-P2-3 | ❌ | source_uri rollback | Not done — same as F7 acknowledgement |
| R4-P2-4 | ⚠️ | `_load_session_merged_records(source_uri=None)` tenant-isolation footgun | Partial — `source_uri` always set on benchmark path now; underlying helper still unscoped |
| R4-P2-5 | ❌ | `filter(limit=10000)` silent truncation across multiple sites | Not done |
| R4-P2-6 | ❌ | source / directory write goes around orchestrator side-effect boundary (redundant `fs.write_context`) | Not done — = R2-23 |
| R4-P2-7 | ✅ | `_delete_immediate_families` per-URI cleanup isolation | PR #4 / REL-04 (= R2-15) |
| R4-P2-8 | ❌ | adapter store branch duplication; future timeout/summary/batch params will drift | Not done — = §25 Phase 7 / R2-24 |
| R4-P2-9 | ❌ | adapter mapping helper duplication | Not done — = §25 Phase 7 / R2-24 |
| R4-P2-10 | ❌ | Bare `Dict[str, Any]` response; `_export_memory_record` projection | Not done — = R2-28 / §25 Phase 6 |
| R4-P3-1 | ✅ | `args.ingest_method` double state in `unified_eval.py` | PR #3 cleanup |
| R4-P3-2 | ✅ | CLI help "merged-leaf ingest" internal term | PR #3 / R2-31 |
| R4-P3-3 | ❌ | `_OCStub` AsyncMock(spec_set) migration (= R2-27) | Not done |
| R4-P3-4 | ✅ | TypedDict for benchmark entry pseudo-record (= F15) | PR #5 / U5 — `RecompositionEntry` |

---

## Section 5 — Pre-existing (declared "not counted toward verdict but track")

| # | Status | File:line | Title | Closed by |
|---|---|---|---|---|
| PE-1 | ❌ | `manager.py:2574` | `_delete_immediate_families` mis-named; rename to `_purge_records_and_fs_subtree` | Not done |
| PE-2 | ⚠️ | `manager.py:1378, 1427` | `_load_session_merged_records` / `_session_layer_counts` 10000-row silent truncation | Partial — `_session_layer_counts` no longer in client response (U6); underlying truncation still in helpers |
| PE-3 | ❌ | `manager.py:1181` | `_session_locks` dict unbounded growth; no reaper | Not done |
| PE-4 | ✅ | `manager.py:2208` | `_run_full_session_recomposition` internal serial LLM derive | PR #3 / U14 — `Semaphore(3)` parallelization |
| PE-5 | ✅ | `benchmarks/adapters/{locomo,conversation}.py` | Per-conversation serial ingest in adapter loop | PR #3 / U13 — `asyncio.gather` with semaphore |
| PE-6 | ❌ | `manager.py:1372` | `_load_session_merged_records` filter doesn't include `(tenant, user)` | Not done — partly papered over by always-set `source_uri` (which contains tenant/user) |

---

## Section 15 — Round 2 Residual Risks

| # | Status | Title | Closed by |
|---|---|---|---|
| RR-1 | ❌ | Observability black box (no metrics on new endpoint) | Not done — = R2-22 / R3-P-09 |
| RR-2 | ❌ | 100k-conversation scale ceiling (single event loop + embedded Qdrant 20GB cap + no checkpoint) | Not done — architectural |
| RR-3 | ❌ | Meta payload validation asymmetry (online uses Pydantic `ToolCallRecord`, benchmark passes through `Dict[str, Any]`) | Not done — U2 added size limits but not structural validation |
| RR-4 | ❌ | Compensation pattern fork: `_BenchmarkRunCleanup` parallel to existing `_restore_merge_snapshot` | Not done |
| RR-5 | ✅ | Anchor-less benchmark sample cluster degeneration (LongMemEval) | PR #3 / U7 — anchorless caps |
| RR-6 | ❌ | URI md5 truncation collision (12 bytes) | Not done — low-risk |
| RR-7 | ❌ | Qdrant `session_id` filter index assumption not pinned in `collection_schemas.py` comment | Not done |

---

## Section 16 — Round 2 Testing Gaps

| # | Status | Title | Closed by |
|---|---|---|---|
| TG-1 | ⚠️ | Test that benchmark merged leaves get semantic L0/L1 (post-derive quality, not just scheduling) | Partial — PR #3 / U16 asserts derive scheduling; quality assertion needs a real LLM in test fixture (PR #5 review T-001 / T-006 carry this forward) |
| TG-2 | ❌ | Test `_session_locks` cleared after `benchmark_ingest_conversation` returns (= R2-02 lock) | Not done |
| TG-3 | ❌ | Test store-path and mcp-path produce equivalent URI mappings for same fixture | Not done |
| TG-4 | ❌ | Test anchor-less (no time_refs/entities) cluster degeneration (= R2-13 lock) | Not done |
| TG-5 | ⚠️ | Test same-`session_id` re-ingest paths (transcript freeze R2-05, orphan residue R2-17) | Partial — torn-replay covered by PR #4 lifecycle test; transcript content-diff path covered by 409 test; both legs explicit |
| TG-6 | 🔄 | Test `layer_counts` is tenant-isolated (R2-04) | N/A — field deleted in U6 |
| TG-7 | ⚠️ | Test `include_session_summary=False` → no summary record + summary_uri=None | Partial — adapter pass-through asserted; HTTP-level no-summary assert missing (PR #5 review T-007) |
| TG-8 | ❌ | Concurrency tests (same session_id, cross-tenant same session_id, concurrent POST) | Not done |

---

## Section 25 — Design-Pattern Refactor Plan

| Phase | Pattern | Status | Closed by |
|---|---|---|---|
| 1 | Router Boundary — move benchmark route under admin router | ✅ | PR #3 / U1 |
| 2 | Facade — public `MemoryOrchestrator.benchmark_conversation_ingest` | ✅ | PR #3 / U1 |
| 3 | **Service Layer** — extract `BenchmarkConversationIngestService` (or split helpers) | ❌ | **Not done** |
| 4 | Unit of Work / Cleanup Tracker | ✅ | PR #3 / U4 + PR #4 / REL-02 — `_BenchmarkRunCleanup` + `RecompositionError` |
| 5 | **Repository / Gateway** — wrap session record queries with scope + paging | ❌ | **Not done** |
| 6 | **DTO / Response Model** — `BenchmarkConversationIngestResponse` | ❌ | **Not done** (= R2-28 / R4-P2-10) |
| 7 | **Shared Adapter Helper** — `benchmarks/adapters/conversation_mapping.py` | ❌ | **Not done** (= R2-24 / R4-P2-8/9) |

### §25.3 Behavior-Changing Decisions Requiring ADR (resolved as part of Phase 1 work)

| Decision | Resolved as |
|---|---|
| Same-session different transcript | 409 conflict (PR #3 / U5) |
| `include_session_summary` default | Adapter passes False explicitly (PR #3 / U11); endpoint default stays True |
| `layer_counts` response | Field removed (PR #3 / U6) |
| Deferred derive parity | Schedule + await `_complete_deferred_derive` (PR #3 / U8) |
| Cleanup semantics | Source preserved for idempotent retry; merged/directory/summary all in tracker (PR #3 / U4 + PR #4 / REL-02) |

---

## Cross-cutting outside-the-review items also closed in this work

These were not in REVIEW.md but were discovered and fixed during the closure work:

| Item | Closed by |
|---|---|
| `MemoryOrchestrator.close()` crashes on partially-initialized instances (`_derive_worker_task` AttributeError in test_perf_fixes) | PR #6 (`f576299`) — defensive `getattr` throughout teardown |
| `pyproject.toml` version 0.6.5 vs `__init__.py` 0.7.0 mismatch | PR #6 (`22468d1`) — bumped pyproject to 0.7.0 |
| Defense-in-depth admin check on `MemoryOrchestrator.benchmark_conversation_ingest` facade | PR #4 / api-contract-007 (`8221bea`) — `enforce_admin=True` default |
| Adapter cross-conv `gather` cancel cascade | PR #4 / REL-04 (`8221bea`) — `return_exceptions=True` |
| Sibling derive task race in U8 gather (orphan FS writes) | PR #3 patch (`059a693`) / F1 must-address — explicit cancel of siblings on first exception |
| Anchorless seed split (single oversized leaf bypassed cap) | PR #3 patch (`059a693`) / ADV-002 — seed-size check |
| Hash transcript list-ordering canonicalization | PR #5 / U6 (`a39220f`) / ADV-006 |

---

## Action queue (the still-open items, ranked)

### Highest leverage — short, valuable, cumulative

1. **§25 Phases 3 + 5 + 6** (Service Layer + Repository + DTO) — server-side design refactor. Opens path to fix R2-11, R2-12, R2-22, R2-25, R2-28, R4-P2-2/4/5/6/10, RR-1/4 in one structured pass.
2. **§25 Phase 7** (Shared Adapter Helper) — closes R2-24, R2-33, R4-P2-8/9 in one PR. Orthogonal to §25 server-side; can be parallel.

### Medium — bug-fix grade, scattered

3. **R3-RC-02 + R2-14** (cross-input-session merge bug — same root cause, off-by-one risk in URI mapping)
4. **R3-RC-03 + PE-6** (`_load_session_merged_records` tenant filtering)
5. **R2-21** (session_summary 1-directory short-circuit)
6. **R2-23** (redundant `fs.write_context` in `_persist_rendered_conversation_source`)
7. **R3-P-06** (stale-filter short-circuit on empty defer-derive projection)
8. **PE-1** (rename `_delete_immediate_families` → `_purge_records_and_fs_subtree`, update 3 call sites)
9. **PE-3** (`_session_locks` reaper)

### Long-term residuals — track but don't block

10. **R2-08 / R3-P-05** (orchestrator `add_batch` + `embed_batch`) — explicitly deferred; only worth picking up after §25 refactor settles
11. **R2-06** (Qdrant ↔ CortexFS write atomicity, no 2PC) — architectural; needs separate ADR
12. **RR-2** (100k-conversation scale ceiling) — architectural; checkpoint/queue-backed ingest
13. **R2-18** (Qdrant TOCTOU duplicate via `uuid5` point ID) — needs schema migration

### Testing gaps — pair with above where natural

14. **TG-2** test session_locks teardown — pair with PE-3
15. **TG-3** test store/mcp URI mapping equivalence — pair with §25 Phase 7
16. **TG-4** test anchor-less degeneration lock — pair with R2-13 (already fixed in code; test is the lock)
17. **TG-7 + F18 + F19** — `include_session_summary=False`, empty segments, edge cases — small testing PR
18. **TG-8** concurrency tests — pair with R3-P-01 (lock scope reduction)

### Cosmetic / advisory only

19. **R2-29 / R2-32 / R2-33 / R2-34 / R2-35 / R2-36 / R2-37** (P3 polish)
20. **R3-P-07** (CortexFS write_context bottleneck) — only matters at higher load
21. **R3-RC-07 / R3-RC-08 / R3-RC-09** (test asserts + URI lex-sort lock)
22. **F24 / F25 / F26** (naming, docstring, structured logging)
23. **RR-6 / RR-7** (md5 collision tracking + `collection_schemas.py` comment)

---

## How to use this tracker

- **Updating after a PR merges:** flip status, add the closing commit hash. Move the row's "Action queue" entry off the queue.
- **When picking next work:** start from the Action queue's top of the appropriate tier. The Highest-Leverage tier has the best ROI per PR.
- **When a finding gets re-flagged in a future review:** add a row noting the rediscovery; don't delete the original.

This file is the canonical "what's done from REVIEW.md" record. Keep it next to the source review at `.context/compound-engineering/ce-code-review/20260424-152926-6301c860/REVIEW.md`.
