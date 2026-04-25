# Residual Review Findings — refactor/server-side-design-patterns

**Run:** `.context/compound-engineering/ce-code-review/20260425-130006-07250c15/`
**Branch:** `refactor/server-side-design-patterns`
**HEAD:** `3e24971`
**Plan:** [docs/plans/2026-04-25-005-refactor-benchmark-ingest-server-side-design-patterns-plan.md](../plans/2026-04-25-005-refactor-benchmark-ingest-server-side-design-patterns-plan.md)
**CE Run summary:** [SUMMARY.md](../../.context/compound-engineering/ce-code-review/20260425-130006-07250c15/SUMMARY.md)

This file records residual `gated_auto` / `manual` findings from the CE
autofix review of plan 005 (Service + Repository + DTO refactor).
8 `safe_auto` findings were already applied in commit `3e24971`. The
findings below are the durable record of work that needs human
judgment before landing.

## Residual Review Findings

### P1 (8 findings)

- **AC-01** [gated_auto] `src/opencortex/http/models.py:447` — Wire-format change: `merged_recompose` path now serializes `"ingest_shape": null` instead of omitting the key. All known Python adapters use `payload.get(...)` so unaffected. Decision: ship as-is or add `model_config = ConfigDict(...)` with `exclude_none=True` to restore pre-DTO wire shape if strict-schema clients exist. (api-contract, confidence 80)
- **AC-02** [gated_auto] `benchmarks/oc_client.py:21` — `OCClient` retries all `status >= 500` responses 8 times with backoff. New 507 from `SessionRecordOverflowError` is deterministic; retries waste ~4 minutes per affected session. Fix: exclude 507 from retryable set. (api-contract, confidence 85)
- **REL-01** [manual] `src/opencortex/context/manager.py:1380-1386` — `_purge_torn_benchmark_run` calls `load_directories()` inside a generic `except Exception` that silently swallows `SessionRecordOverflowError`. Directory-layer records from a failed prior run become permanent storage orphans with no cleanup signal. (reliability, confidence 90)
- **correctness-001** [manual] `src/opencortex/context/benchmark_ingest_service.py:271-274` — Idempotent-hit summary lookup migrated from `_get_record_by_uri` (catches Exception, returns None) to `_repo.load_summary` (raises). Transient storage error during summary lookup now fails the entire idempotent path instead of degrading to `summary_uri=None`. (correctness, confidence 75)
- **T-01** [manual] `src/opencortex/context/benchmark_ingest_service.py` — No direct unit tests for `BenchmarkConversationIngestService`. The `ValueError` on unsupported `ingest_shape` (line 110) and the empty-segments early-exit (lines 127-133) are completely untested. Add `tests/test_benchmark_ingest_service.py`. (testing, confidence 90)
- **T-02** [manual] `src/opencortex/context/benchmark_ingest_service.py:470-483` — Sibling-cancel branch in `_write_merged_leaves` is unreachable by current tests. Existing failure injection at `test_benchmark_ingest_lifecycle.py:323` patches `_run_full_session_recomposition` which fires AFTER all derive tasks have completed. Inject failure into `_complete_deferred_derive` for one leaf while others are in-flight. (testing, confidence 88)
- **T-03** [manual] `src/opencortex/context/benchmark_ingest_service.py:516-518` — `RecompositionError` drain branch unreachable; existing test raises `RuntimeError` directly, bypassing the `RecompositionError` wrapper. Add a test that raises `RecompositionError(original=..., created_uris=[...])` and asserts the partial directory URIs are routed through cleanup. (testing, confidence 85)
- **M-01** [gated_auto] `src/opencortex/context/manager.py:1613-1637` — `benchmark_ingest_conversation` is a 7-line pure pass-through shim. Decision: inline into orchestrator facade (one fewer hop) or schedule for explicit removal. (maintainability, confidence 80)
- **ADV-U-001** [manual] `src/opencortex/context/manager.py:1381` — `_purge_torn_benchmark_run.load_directories()` does NOT pass tenant/user scope (every other repo call site does). Combined with `_delete_immediate_families` consuming the URI list directly, on torn-replay this could hard-delete another tenant's directory subtree if `meta.source_uri` collides as a string. Pair with REL-01. (adversarial, confidence 65)

### P2 (12 findings)

- **T-04** [manual] `src/opencortex/context/session_records.py:233-240` — Scroll-path overflow guard untested; only filter-fallback path covered. Add a `_ScrollingStorage` that always returns a non-None cursor. (testing, confidence 82)
- **T-05** [manual] Idempotent-hit + existing summary path untested. Every test hardcodes `include_session_summary=False`; load_summary never returns a record. (testing, confidence 80)
- **T-06** [manual] `src/opencortex/http/admin_routes.py:307-327` — HTTP 507 handler has zero test coverage. Add an integration test that injects a saturated scroll storage and asserts status_code=507 with structured detail. (testing, confidence 78)
- **M-02** [manual] `BenchmarkConversationIngestService` reaches into 18+ private members of `ContextManager` including two `manager._orchestrator._xxx` chains. Acknowledged in plan; tighten in a follow-up. (maintainability, confidence 85)
- **M-03** [manual] `src/opencortex/context/manager.py:268-283` — Lazy import of `BenchmarkConversationIngestService` inside `__init__` is a structural signal the module boundary is in the wrong place; static type checkers cannot resolve types. (maintainability, confidence 75)
- **M-04** [manual] `RecompositionError` and `_BenchmarkRunCleanup` should move to a shared module (e.g., `context/benchmark_types.py` alongside the existing `recomposition_types.py`). Eliminates the lazy imports inside service methods. (maintainability, confidence 75)
- **M-05** [manual] `src/opencortex/context/benchmark_ingest_service.py:102-104` — `ingest_shape` validation duplicates what `Literal['merged_recompose', 'direct_evidence']` on the Pydantic request model could enforce at the boundary. (maintainability, confidence 70)
- **PERF-01** [manual] `src/opencortex/context/manager.py:2699-2728` — `_generate_session_summary` issues two independent full-scroll passes (`load_directories` then `load_merged`) over identical session data. Add `load_all` repository method that returns the full record set and let the caller partition by layer. Hits both benchmark and production session_end. (performance, confidence 90)
- **PERF-02** [manual] `src/opencortex/context/session_records.py:254-271,295-301` — `source_uri` filtered in-memory after full scroll; should be pushed into Qdrant filter via indexed `meta.source_uri` payload field. (performance, confidence 85)
- **PERF-03** [manual] `src/opencortex/context/benchmark_ingest_service.py:640-655` — `_ingest_direct_evidence` N+1: one `_get_record_by_uri` per segment after `add()` to re-fetch as dict. Use the returned `Context` directly. 10-100ms per ingest call on critical path. (performance, confidence 88)
- **REL-02** [manual] `src/opencortex/context/benchmark_ingest_service.py:522-528` — `cleanup.summary_uri = manager._generate_session_summary(...)` has no try/except. Mid-call failure after primary `add()` succeeds creates an orphaned summary record. (reliability, confidence 75)
- **REL-03** [gated_auto] `src/opencortex/context/benchmark_ingest_service.py:678-688` — `_ingest_direct_evidence` cleanup uses `contextlib.suppress(Exception)`; second cancellation arriving during `_delete_immediate_families` escapes (CancelledError is BaseException since 3.8). The KP-09 fix consolidated the outer except to BaseException; consider doing the same for the inner suppress. (reliability, confidence 75)
- **REL-04** [gated_auto] `src/opencortex/http/admin_routes.py:281` — `BenchmarkConversationIngestResponse.model_validate(result)` is not in the surrounding try/except. Schema drift surfaces as unstructured 500 with no session_id context logged. (reliability, confidence 75)
- **AC-03** [gated_auto] `src/opencortex/http/models.py:407` + `src/opencortex/context/manager.py:1492` — `event_date` default empty-string vs null inconsistency. Tied to KP-01 (already applied). Decide whether `_export_memory_record` should emit `None` instead of `''` for absent dates. (api-contract, confidence 75)
- **AC-04** [gated_auto] `src/opencortex/http/models.py:408` — `msg_range: Optional[List[int]]` accepts arbitrary-length lists; contract is always length-2. Add `Field(min_length=2, max_length=2)` to surface storage corruption at the HTTP layer. (api-contract, confidence 72)
- **KP-05** [manual] Shape strings `"merged_recompose"` / `"direct_evidence"` are bare literals at 6+ sites across two modules. Module-level constants. (kieran-python, confidence 85)
- **KP-07** [manual] `src/opencortex/context/session_records.py:242,279` — `load_merged` and `load_directories` share identical 15-line filter+sort bodies. Extract `_load_layer_records(session_id, layer, ...)` helper. (kieran-python, confidence 75)
- **ADV-U-003** [manual] Cancellation timing — if `CancelledError` fires during `_persist_rendered_conversation_source` BEFORE entering the cleanup-tracker scope, the source URI is created but untracked. (adversarial, confidence 50)
- **ADV-U-005** [manual] Cascade with ADV-U-001 + `contextlib.suppress(Exception)` wrapping `_purge_torn_benchmark_run` — partial purge failure becomes invisible; next ingest writes new leaves on top, marks run_complete=True, every retry returns the polluted set as authoritative. (adversarial, confidence 60)
- **ADV-U-006** [manual] Second cancellation mid-`compensate` orphans the tail of `merged_uris`/`directory_uris`. (adversarial, confidence 55)

### P3 / Advisory (8 findings)

- **correctness-003** [advisory] `src/opencortex/http/models.py:1184-1227` — `BenchmarkConversationIngestResponse` doesn't declare `extra='forbid'`; `model_validate` silently drops unknown keys. Either add ConfigDict or soften the U5 docstring claim about "drift detection". (correctness, confidence 50)
- **AC-05** [advisory] `src/opencortex/http/admin_routes.py:244,281` — Double Pydantic validation (explicit `model_validate` + FastAPI `response_model`). Negligible overhead on admin endpoint. (api-contract, confidence 90)
- **AC-06** [gated_auto] `src/opencortex/http/models.py:431-434` — `status: str` could be `Literal["ok"]` for machine-readable success-only invariant. (api-contract, confidence 70)
- **KP-08** [manual] `src/opencortex/context/benchmark_ingest_service.py:396` — `_bounded_complete` nested closure → static method for testability. (kieran-python, confidence 75)
- **PS-002** [manual] `src/opencortex/context/session_records.py` — `getattr + callable()` capability detection deviates from project's stated `hasattr` convention. (project-standards, confidence 75)
- **PERF-04** [advisory] Fallback scroll path issues 50K-row single query for adapters without scroll. No production impact (Qdrant has scroll). (performance, confidence 80)
- **PERF-05** [advisory] `model_validate` re-validates dict on response path. Sub-millisecond overhead. (performance, confidence 75)
- **REL-05** [manual] `src/opencortex/context/manager.py:278-283` — `BenchmarkConversationIngestService` import inside `__init__` runs eagerly at construction (not lazy). Import failure crashes every `ContextManager` instantiation. (reliability, confidence 50)

## Notable Strengths (verified, not bugs)

From adversarial review (Q1, Q2, Q7, Q8, Q10):
- Lock keying by `SessionKey = (collection, tenant, user, session_id)` correctly avoids false cross-tenant serialization.
- Sibling-cancel pattern in `_write_merged_leaves` is correct under Python 3.10+ cooperative scheduling.
- `_delete_immediate_families` works on benchmark_evidence URIs because `remove_by_uri` deletes by URI prefix (no layer check).
- Lazy import of `BenchmarkConversationIngestService` is safe — module body fully loads before instance construction (no deadlock).
- `raise exc.original from exc` preserves original traceback correctly.

From agent-native review:
- `BenchmarkConversationIngestResponse` improves OpenAPI discoverability.
- 507 error body is structured (reason, session_id, method, count_at_stop, next_cursor, hint) — agent-parseable.
- Admin gate on benchmark endpoint is intentional and correct.
