---
title: "refactor: Benchmark ingest server-side design patterns (Service + Repository + DTO)"
type: refactor
status: active
date: 2026-04-25
---

# refactor: Benchmark ingest server-side design patterns (Service + Repository + DTO)

## Overview

Closes the still-open server-side design-pattern phases from
`.context/compound-engineering/ce-code-review/20260424-152926-6301c860/REVIEW.md`
section 25 — Phases 3 (Service Layer), 5 (Repository / Gateway), and 6
(DTO / Response Model). Phase 1 (Router Boundary) and Phase 2 (Facade)
were completed in PR #3 (commit `e50810b`); Phase 4 (Unit of Work) was
completed in PR #3+#4 (`_BenchmarkRunCleanup` + `RecompositionError`).
Phase 7 (Shared Adapter Helper) is intentionally out of scope — it lives
in `benchmarks/adapters/` and is orthogonal to the server-side Service /
Repository / DTO trio. A separate PR will pick it up.

The shared driver across all three phases: `ContextManager.benchmark_ingest_conversation`
is a ~390-line method holding 6 distinct responsibilities (source persist,
segment normalize, entry build, leaf write + derive scheduling, recompose
+ summary, response build). Today it reaches into private repository
helpers, returns a bare `Dict[str, Any]`, and shares cleanup conventions
with the production lifecycle through manual co-ordination. The refactor
makes those boundaries explicit so the next reviewer can follow data flow
without reading 390 lines of mixed concerns.

---

## Problem Frame

Three open structural debts from the prior review (closure tracker
`docs/residual-review-findings/2026-04-24-review-closure-tracker.md`):

- **R2-11 / F14**: SRP violation — `benchmark_ingest_conversation` packs
  six responsibilities. Over the four-PR repair cycle the method grew
  from ~100 to ~390 lines without changing shape; future incident
  triage and feature work hit O(method-size) reading cost.
- **R2-12 / R4-P2-2**: Cleanup ownership scattered across four layers
  (source, merged leaves, directory records via recomposition, summary).
  `_BenchmarkRunCleanup` plus `RecompositionError` (PR #3+#4) cover the
  data flow but the *ownership* of each URI lives in different methods
  on the same God-class. Lifting the orchestration into a Service
  centralises that ownership without disturbing the existing tracker.
- **R2-22 / R3-P-09**: Endpoint zero observability — no `StageTimingCollector`
  hooks, no per-phase metrics, no LLM-call counts. The right surface
  for these hooks is the Repository (query-time metrics) and the
  Service (phase-time metrics). Without those surfaces, observability
  has nowhere to land.

Plus three open contract debts:

- **R2-28 / R4-P2-10**: Bare `Dict[str, Any]` response. Adapters and
  future orchestration layers can't depend on the shape; drift will
  recur.
- **PE-2**: `_load_session_merged_records` / `_load_session_layer_counts`
  silently truncate at 10 000 rows. Pre-existing footgun unblocked by
  scope unification.
- **PE-6 / R3-RC-03**: `_load_session_merged_records` filter doesn't
  carry `(tenant, user)` — papered over today by always passing
  `source_uri` (which embeds tenant/user in the URI). Pre-existing.

This plan executes the §25 Behavior-Preserving Refactor for the
server-side surface, scoped tightly per the §25.2 Abstraction Guardrails
("only Service, Repository, DTO; no Strategy / Abstract Factory").

Origin documents:
- Source review: `.context/compound-engineering/ce-code-review/20260424-152926-6301c860/REVIEW.md` (§25 specifically)
- Closure tracker: `docs/residual-review-findings/2026-04-24-review-closure-tracker.md`
- Prior plan that landed Phases 1/2/4: `docs/plans/2026-04-25-003-fix-benchmark-conversation-ingest-review-fixes-plan.md`

---

## Requirements Trace

### From REVIEW.md §25.1 Behavior-Preserving Refactor table

- **R1 (Phase 5 Repository)**: Encapsulate `_load_session_merged_records`,
  `_load_session_directory_records`, `_session_layer_counts`, and the
  session_summary record lookup behind a single repository class.
  Verification (per §25.1): "same session 不同 source/tenant 不混；超过
  limit 不静默截断."
- **R2 (Phase 5 scope discipline)**: All repository queries carry
  `(tenant_id, user_id, source_uri)` scope where applicable. Closes
  PE-6 / R3-RC-03 directly; reduces the foot-gun surface PE-2 sits on.
- **R3 (Phase 5 limit handling)**: Replace `limit=10_000` silent
  truncation with explicit pagination (cursor-based) plus an overflow
  guard that surfaces a structured warning rather than dropping rows.
- **R4 (Phase 6 DTO)**: Define `BenchmarkConversationIngestResponse`
  and `BenchmarkConversationIngestRecord` Pydantic models in
  `src/opencortex/http/models.py`. Verification: "Pydantic model
  validation; adapter 只消费明确字段."
- **R5 (Phase 6 contract)**: DTO documents the `content` field's
  hydration contract (R3-RC-06 lineage — "in-memory map captured at
  write time, falls back to FS read") so future maintainers don't
  rediscover the race.
- **R6 (Phase 3 Service extraction)**: Pull `benchmark_ingest_conversation`
  out of `ContextManager` into `BenchmarkConversationIngestService`.
  Six responsibilities become six private methods on the service.
  Verification: "行为 golden test：相同输入幂等；相同 transcript 的
  records/msg_range 不变."
- **R7 (Phase 3 thin shim)**: `ContextManager.benchmark_ingest_conversation`
  remains as a thin delegating method (or is removed entirely if no
  internal caller depends on it). Choice resolved in U5.
- **R8 (Phase 3 cleanup ownership)**: All cleanup-tracker registration
  happens inside the Service. `RecompositionError` flow continues to
  surface partial directory URIs but they land in the Service-scoped
  tracker, not a `ContextManager`-scoped one.

### From §25.2 Abstraction Guardrails

- **R9**: Service Layer is justified only because the method has
  outgrown its host (391 lines, 6 responsibilities) — extraction
  threshold met per the guardrail's "only when … extraction is
  warranted."
- **R10**: Repository must serve ≥2 call sites — verified: 7 + 3 + 2
  call sites today across `manager.py`. Threshold met.
- **R11**: DTO fixes the endpoint contract only; does NOT pollute
  internal storage record shape (Repository methods continue returning
  `Dict[str, Any]` records as today, since storage adapters speak
  dicts).
- **R12**: No Strategy / Abstract Factory introduced. Service has no
  variants; Repository has no variants; DTO has no inheritance.

### From §25.1 Verification (carried into per-unit Test Scenarios)

- **R13**: Behavior golden test: same input → identical response.
  Same transcript hash → identical merged-leaf URIs and `msg_range`.
- **R14**: Production conversation lifecycle (`context_commit`,
  `context_end`) regression must pass. Repository touches helpers
  shared with `_run_full_session_recomposition` (called from
  `context_end` for merge follow-up).
- **R15**: Each phase committable independently — phases land in U1
  / (U2) / U3 / U4 / U5 commits.

---

## Scope Boundaries

- Do NOT modify production `context_commit`, `context_end`, or
  `_run_full_session_recomposition` semantics. Repository wraps
  shared helpers; calling sites in production paths are updated
  mechanically only.
- Do NOT introduce Strategy / Abstract Factory / Builder / Visitor
  patterns. Only Service + Repository + DTO. (§25.2 hard constraint.)
- Do NOT change endpoint URL, error codes, request shape, response
  field set, or response semantics. Behavior-preserving refactor.
- Do NOT add observability (StageTimingCollector, metrics) in this
  plan. The Service + Repository structure are the *surface* on
  which a future PR can hang those hooks; landing the surface is
  a separate concern from wiring the metrics. R2-22 stays open.
- Do NOT migrate `_benchmark_ingest_direct_evidence` to a separate
  Service — it dispatches from inside `benchmark_ingest_conversation`
  via the `ingest_shape` branch and stays in the same Service class
  with both code paths exposed as service methods.
- Do NOT touch the orchestrator `add_batch` deferral (R2-08 / U12);
  the Service uses `_orchestrator.add` per-leaf as today.

### Deferred to Follow-Up Work

- **§25 Phase 7** — Shared Adapter Helper (`benchmarks/adapters/conversation_mapping.py`):
  separate PR after this lands.
- **R2-22 Observability**: Phase 6 / 7 follow-up once `StageTimingCollector`
  conventions are decided across the wider orchestrator.
- **R2-08 / U12 add_batch**: Cross-cutting orchestrator change; needs
  its own plan after this refactor settles.

---

## Context & Research

### Relevant Code and Patterns

**Service Layer (Phase 3) target**:
- `src/opencortex/context/manager.py:1588-1978` — `benchmark_ingest_conversation`
  body. 391 lines, 6 responsibilities. The extraction target.
- `src/opencortex/context/manager.py:1979-2042` — `_benchmark_ingest_direct_evidence`.
  Sibling code path dispatched on `ingest_shape="direct_evidence"`. Move
  alongside.
- `src/opencortex/context/manager.py:81-150` — `_BenchmarkRunCleanup` dataclass.
  Already lives in this module; the Service uses it as-is.
- `src/opencortex/context/manager.py:81` — `RecompositionError`. Service
  catches it and drains `created_uris` into the tracker.

**Repository (Phase 5) call-site map**:
- `_load_session_merged_records`: 7 callers (lines 1669, 1894, 2233,
  2972, 3253, 3265 in `manager.py`, plus its own definition at 2148).
- `_load_session_directory_records`: 3 callers (1356, 3233, plus
  definition at 2178).
- `_session_layer_counts`: 2 callers (3913 in `_finalize_session_end`,
  plus definition at 2208). The benchmark response builder used to
  call it but R2-04 removed that.
- Session summary record lookup: currently inline `_orchestrator._get_record_by_uri(_session_summary_uri(...))`
  at `manager.py:1531-1534` (idempotent-hit branch). Promote to a
  named repository method.

**DTO (Phase 6) pattern reference**:
- `src/opencortex/http/models.py:116` — `MemorySearchResponse` (existing
  Pydantic response model with optional fields, list of structured
  result items, optional pipeline payload). Mirror this shape.
- `src/opencortex/http/models.py:325-329` — `MemorySearchResultItem`
  fields list (uri, abstract, context_type, score, overview, content,
  keywords, source_doc_id, etc.). Cherry-pick the fields the benchmark
  response actually returns.
- `src/opencortex/http/models.py:298-360` — existing
  `BenchmarkConversationIngestRequest` + `BenchmarkConversationMessage` +
  `BenchmarkConversationSegment` (the request side; same module).

**Existing facade conventions to mirror**:
- `src/opencortex/orchestrator.py` `MemoryOrchestrator.benchmark_conversation_ingest`
  (PR #3 / U1) — public facade pattern. Will delegate to Service via
  `self._context_manager._benchmark_ingest_service` (or similar
  attribute) instead of directly to `_context_manager.benchmark_ingest_conversation`.
- `src/opencortex/http/admin_routes.py` `admin_benchmark_conversation_ingest` —
  return type changes from `Dict[str, Any]` to the new DTO. FastAPI's
  built-in serialization handles the rest.

**Production lifecycle interactions**:
- `_run_full_session_recomposition` (manager.py around 2900) is called
  by both `context_end` (production) and the Service (benchmark). Its
  internal use of `_load_session_merged_records` becomes a Repository
  call after Phase 5 lands.
- `_finalize_session_end` (line 3913 area) calls `_session_layer_counts`
  for integrity logs. Repository's `session_layer_counts(...)` method
  serves both this and any future caller.

### Institutional Learnings

- `docs/solutions/best-practices/single-bucket-scoped-probe-2026-04-16.md` —
  Cited in plan 003. Repository scope discipline (`(tenant, user, source_uri)`
  always passed) directly aligns with this entry's "scoped miss stays
  scoped" principle.
- `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md` —
  DTO discipline aligns with this entry's "use phase-native envelopes,
  don't revive flat fields." Future benchmark response extensions go
  through the DTO, not bare dict additions.
- No existing entry for **Service / Repository / DTO extraction
  pattern**. Strong `/ce-compound` candidate after merge.

### External References

External research skipped — Repository / Service / DTO are well-known
patterns; project has multiple existing examples (orchestrator facade,
context manager, MemorySearchResponse) to mirror. Codebase uses
`ContextManager`, `MemoryOrchestrator`, and various `*Adapter` classes
already; the Service / Repository pattern is the natural extension.

---

## Key Technical Decisions

- **Service constructor takes orchestrator handle, not full ContextManager.**
  The Service needs `_orchestrator.add`, `_orchestrator._get_record_by_uri`,
  `_orchestrator.remove`, plus the shared semaphores
  (`_derive_semaphore`, `_directory_derive_semaphore`). It also needs
  `_run_full_session_recomposition` and a few small helpers
  (`_persist_rendered_conversation_source`, `_mark_source_run_complete`,
  `_purge_torn_benchmark_run`, `_benchmark_recomposition_entries`,
  `_aggregate_records_metadata`, `_merged_leaf_uri`, `_decorate_message_text`,
  etc.) which still live on `ContextManager`.

  Two options:
  - **(a)** Service stores a `ContextManager` reference and reaches in
    for the helpers. Pragmatic; keeps the helpers where they are; risk:
    perpetuates the God-class.
  - **(b)** Move the helpers to `ContextManager` -> Service incrementally
    in this plan. Cleaner; risk: balloons the diff.

  **Choice: (a)** for this PR. The Service holds a `manager: ContextManager`
  reference and calls into it for shared helpers. Promotion of helpers
  to the Service (or to a separate `BenchmarkRecomposeUtils` module) is
  a follow-up. Rationale: §25.2 says "extract Service Layer only when
  the host method has grown" — that justifies moving the orchestration,
  not chasing every helper.

- **Repository class location: `src/opencortex/context/session_records.py`.**
  Sibling module to `manager.py` and `recomposition_types.py`. Avoids
  circular import: Repository constructor takes a storage adapter and
  collection-name resolver, not `ContextManager` itself. Implementation
  details:
  - `SessionRecordsRepository.__init__(storage, collection_resolver)` —
    `collection_resolver` is a callable that returns the active collection
    name (i.e., `_orchestrator._get_collection`). Avoids capturing the
    whole orchestrator.
  - Methods: `load_merged(session_id, *, source_uri, tenant_id=None,
    user_id=None, limit=None, cursor=None)`,
    `load_directories(session_id, *, source_uri, tenant_id=None,
    user_id=None)`, `load_summary(tenant_id, user_id, session_id)`,
    `layer_counts(session_id, *, source_uri=None, tenant_id=None,
    user_id=None)`. Symmetric naming, all kwargs after `session_id`.

- **Limit handling (R3): cursor-based pagination + overflow flag.**
  Replace `limit=10_000` silent truncation with:
  - Default page size 1 000.
  - Repository methods that load "all rows for a session" (used in
    response build) implement an internal loop that pages until exhausted.
  - If the loop hits a configurable max-pages safety stop (default 50,
    i.e., 50 000 rows), raise a `SessionRecordOverflowError` with the
    cursor and current count, and let the caller decide. Benchmark
    Service surfaces this as HTTP 507 (or 500 with structured detail).
  - Pure overflow case is so rare on real benchmark data (LoCoMo
    typical 37 leaves / conversation) that the production cost is
    negligible; the limit-overflow guard is the correctness improvement.

- **DTO field set matches current response exactly.**
  Today's response (after Phase 1 work):
  ```
  status: "ok"
  session_id: str
  source_uri: str | None
  summary_uri: str | None
  records: list[ {uri, abstract, overview, content, meta, abstract_json,
                  session_id, speaker, event_date, msg_range,
                  recomposition_stage, source_uri} ]
  ```
  Plus `direct_evidence` path adds `ingest_shape: "direct_evidence"`.

  DTO mirrors this. `content` field carries a docstring documenting the
  hydration semantics (in-memory map → FS fallback). No fields added,
  no fields renamed, no fields removed in this PR.

- **Service does NOT extract `_BenchmarkRunCleanup` or `RecompositionError`.**
  Both already live as module-level classes in `manager.py:81-150` and
  are imported by anyone who needs them. The Service imports both and
  uses them unchanged. Moving them to the Service module is a cosmetic
  follow-up.

---

## Open Questions

### Resolved During Planning

- **Should ContextManager.benchmark_ingest_conversation become a thin shim or be removed?**
  → Thin shim (one-line delegate). Removing it would break any
  in-process test or maintenance script that bypasses the Service. The
  shim costs nothing.
- **Should the Service own a separate cleanup-tracker class or reuse `_BenchmarkRunCleanup`?**
  → Reuse. The tracker is well-tested, independent, and not coupled to
  ContextManager.
- **Should Repository methods return Pydantic objects or `Dict[str, Any]`?**
  → `Dict[str, Any]` (R11). Storage layer speaks dicts; Pydantic
  serialization happens at the HTTP boundary only (DTO).
- **Should Repository scope discipline (R2) apply to production callers
  of `_load_session_merged_records` (e.g., `_run_full_session_recomposition`)?**
  → Yes. Production callers all have `(tenant, user, source_uri)`
  available via `set_request_identity` contextvars + the `source_uri`
  parameter they already pass. Adding scope to the Repository call
  is a strict tightening with no behavior loss; in fact it closes
  PE-6 (cross-tenant `session_id` collision footgun) for production
  too.

### Deferred to Implementation

- Exact private method names inside the Service (e.g., `_persist_source`
  vs `_ensure_source_versioned` vs `_write_source`).
- Whether `SessionRecordOverflowError` deserves a dedicated exception
  class or can use `ValueError` with a structured message — defer to
  the implementer; either is acceptable.
- Page size constant (1 000) and max-pages safety stop (50) — pick
  during U2 based on what the test fixture exercises.
- Whether to inline the session_summary lookup in U1 or wait for U2 —
  the helper is one line today; keeping it inline in U1 is fine.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
                          +-----------------------------------+
                          | http/admin_routes.py              |
                          | admin_benchmark_conversation_     |
                          | ingest(req)                       |
                          | -> calls orch.benchmark_           |
                          |    conversation_ingest(...)        |
                          | -> returns                         |
                          |    BenchmarkConversationIngest    |
                          |    Response                        |
                          +-----------------+-----------------+
                                            |
                                            v
                  +-------------------------+--------------------------+
                  | orchestrator.MemoryOrchestrator                    |
                  | .benchmark_conversation_ingest(...)                |
                  | (existing public facade — PR #3 / U1)              |
                  | -> delegates to                                    |
                  |    self._context_manager._benchmark_ingest_service |
                  +-------------------------+--------------------------+
                                            |
                                            v
       +------------------------------------+-----------------------------------+
       | context/benchmark_ingest_service.py                                    |
       | class BenchmarkConversationIngestService                               |
       |   __init__(manager, repo)                                              |
       |   async def ingest(session_id, tid, uid, segments,                     |
       |                    include_session_summary, ingest_shape)              |
       |     -> BenchmarkConversationIngestResponse                             |
       |                                                                        |
       |   private:                                                             |
       |     _persist_source(...)         (uses manager helpers)               |
       |     _normalize_segments(...)                                           |
       |     _build_merged_leaf_entries(...)                                    |
       |     _write_merged_leaves_with_derive(...)  (uses cleanup tracker)     |
       |     _recompose_and_summarize(...)  (uses RecompositionError catch)    |
       |     _build_response(...)         (uses repo + DTO)                    |
       |     _ingest_direct_evidence(...) (alternate ingest_shape branch)      |
       +------+----------------------------+-------------------------+---------+
              |                            |                         |
              v                            v                         v
   +----------+----------+   +-------------+------------+   +--------+----------+
   | context/             |   | context/manager.py       |   | http/models.py    |
   | session_records.py   |   | _BenchmarkRunCleanup     |   | BenchmarkConv.    |
   | SessionRecords       |   | RecompositionError       |   | IngestResponse    |
   | Repository           |   | (existing helpers used   |   | + IngestRecord    |
   | .load_merged(...)    |   |  via manager param)       |   | (Pydantic)        |
   | .load_directories()  |   +---------------------------+   +-------------------+
   | .load_summary(...)   |
   | .layer_counts(...)   |
   +----------------------+
              ^
              |
              | (production callers also use this repo)
              |
   +----------+-----------+
   | context/manager.py    |
   | _run_full_session_    |
   |  recomposition(...)   |
   | _finalize_session_end |
   | (other 7-3-2 sites)   |
   +-----------------------+
```

Key boundary properties:
- Admin route only knows about: orchestrator facade + DTO.
- Orchestrator facade only knows about: Service.
- Service only knows about: Repository, DTO, manager (for shared
  helpers), `_orchestrator.add` / `.remove` / `_get_record_by_uri`,
  cleanup tracker classes.
- Repository only knows about: storage adapter, collection name.
- ContextManager retains all helpers it owns today; Service borrows
  them via the `manager` reference.

---

## Implementation Units

### Phase 5 — Repository / Gateway

- [x] U1. **Extract `SessionRecordsRepository` (mechanical move, behavior-preserving)**

**Goal:** Create `src/opencortex/context/session_records.py` housing
`SessionRecordsRepository`. Move `_load_session_merged_records`,
`_load_session_directory_records`, `_session_layer_counts`, and the
session_summary record lookup off `ContextManager` and onto the
repository class. Update all 12 call sites to use
`self._session_records.<method>(...)` — `ContextManager.__init__`
constructs a single repo instance once `_orchestrator` is set.

**Requirements:** R1, R10, R14.

**Dependencies:** None.

**Files:**
- Create: `src/opencortex/context/session_records.py`
- Modify: `src/opencortex/context/manager.py` (delete 4 helper bodies; add `self._session_records` attribute; rewrite ~12 call sites)
- Test: `tests/test_session_records_repository.py` (new — golden parity tests)
- Test: existing `tests/test_e2e_phase1.py`, `tests/test_context_manager.py`, `tests/test_http_server.py`, `tests/test_locomo_bench.py`, `tests/test_benchmark_ingest_lifecycle.py` all must pass unchanged

**Approach:**
- Repository constructor: `(storage, collection_resolver: Callable[[], str])`. The resolver is `lambda: orchestrator._get_collection()` — avoids capturing the orchestrator reference.
- Each method's body is the existing helper body, copy-pasted with `self._storage` / `self._collection()` substitutions. No filter-DSL changes in U1.
- ContextManager constructs the repo lazily on first access OR in `__init__` after orchestrator is set. (Ordering question — `_orchestrator` is set in `__init__` argument; verify by grep.)
- Call-site updates are mechanical: `await self._load_session_merged_records(session_id=..., source_uri=...)` becomes `await self._session_records.load_merged(session_id=..., source_uri=...)`. Method name change `_load_session_*` → `load_*`; underscore prefix dropped because the methods are now public on the repo.

**Execution note:** Add a behavior parity test (`tests/test_session_records_repository.py`) that asserts repo methods return the same rows as the legacy helpers for a fixture, before deleting the legacy helpers. This locks the move.

**Patterns to follow:**
- `ContextManager.__init__` attribute-set patterns for `self._derive_semaphore`, `self._directory_derive_semaphore` (instance-scoped state).
- `MemoryOrchestrator._get_collection` callable pattern.

**Test scenarios:**
- Happy path: `repo.load_merged(session_id="s1", source_uri="opencortex://t/u/session/conversations/s1/source")` returns the same records as the legacy `_load_session_merged_records` for the same fixture.
- Happy path: `repo.load_directories(session_id="s1", source_uri="...")` parity with legacy.
- Happy path: `repo.layer_counts(session_id="s1")` parity with legacy.
- Happy path: `repo.load_summary(tenant_id="t", user_id="u", session_id="s1")` returns the existing summary record or None.
- Edge case: empty session — all four methods return empty list / zero counts / None.
- Edge case: nonexistent session_id — same.
- Integration: production lifecycle — run `tests.test_e2e_phase1` and `tests.test_context_manager` to confirm `_run_full_session_recomposition`, `_finalize_session_end`, and any benchmark path that uses repo all behave identically to before.

**Verification:**
- `grep "_load_session_merged_records\|_load_session_directory_records\|_session_layer_counts" src/opencortex/context/manager.py` shows 0 hits (helpers fully removed).
- Repo test file passes; full lifecycle and benchmark suites pass with no regression.

---

- [x] U2. **Add scope discipline + pagination + overflow guard to Repository**

**Goal:** Each repository method enforces `(tenant_id, user_id, source_uri)`
filter discipline — closing PE-6 / R3-RC-03 — and replaces
`limit=10_000` silent truncation with explicit pagination plus a
`SessionRecordOverflowError` when results exceed a safety stop.
Production callers automatically benefit because they already have
identity + source available.

**Requirements:** R2, R3, R14.

**Dependencies:** U1.

**Files:**
- Modify: `src/opencortex/context/session_records.py` (extend method
  signatures with `tenant_id` / `user_id` kwargs + paging logic +
  overflow guard)
- Modify: `src/opencortex/context/manager.py` (call sites pass
  identity from `set_request_identity` context where available; pass
  None for legacy paths that genuinely don't have it)
- Modify: `src/opencortex/http/admin_routes.py` (catch new
  `SessionRecordOverflowError` and surface as HTTP 507 with structured
  detail)
- Modify: `src/opencortex/context/benchmark_ingest_service.py` (will
  exist after U4 — for now, the Service-side wiring lands in U4 too;
  in U2 only the manager-call-site updates land)
- Test: `tests/test_session_records_repository.py` (extend with scope
  + pagination + overflow scenarios)

**Approach:**
- Add `tenant_id: Optional[str] = None`, `user_id: Optional[str] = None`
  kwargs to `load_merged`, `load_directories`, `layer_counts`. When
  provided, filter narrows to that tenant/user. When `source_uri` is
  set, that already implies tenant/user (URI structure), so explicit
  tenant/user is redundant for those calls but harmless. For
  `source_uri=None` calls, tenant/user becomes the strict filter,
  closing PE-6.
- Pagination: internal `_paged_filter(...)` loops `storage.scroll`
  with default page size 1 000. Methods that conceptually return
  "all rows for this session" exhaust the scroll and assemble the full
  list, raising `SessionRecordOverflowError` after a safety stop
  (default 50 pages = 50 000 rows).
- Overflow class: `SessionRecordOverflowError(session_id, count, cursor)`
  carries enough context to debug. HTTP 507 (Insufficient Storage —
  closest semantically to "too much data to return") with detail
  body containing `{reason, session_id, count_at_stop, hint}`.
- Repo's `load_summary` doesn't paginate (always 1 row).

**Execution note:** Test scope discipline against an InMemoryStorage
fixture with two synthetic tenants having the same `session_id`. The
post-U2 repo must NOT mix them.

**Patterns to follow:**
- Existing `storage.scroll(...)` API (signature in
  `src/opencortex/storage/storage_interface.py` — verify during
  implementation).
- `RecompositionError` (`src/opencortex/context/manager.py:81`) as the
  named-exception precedent.
- 504 / 409 / 410 HTTP error envelope in `src/opencortex/http/admin_routes.py`
  for the new 507.

**Test scenarios:**
- Happy path: `repo.load_merged(session_id="s1", tenant_id="A", user_id="x")` returns only A/x records even if B/y has the same session_id.
- Happy path: `repo.load_merged(session_id="s1", source_uri="...")` works as before; explicit tenant/user kwargs harmless when source_uri is set.
- Happy path: 5 000-row fixture loads completely via internal pagination (5 page fetches, no truncation).
- Edge case: 0 rows — empty list returned, no overflow.
- Edge case: exactly at safety stop (50 000 rows) — returns all without raising.
- Error path: 50 001-row fixture raises `SessionRecordOverflowError` with `count == 50_000` and `cursor` set.
- Error path: HTTP layer maps `SessionRecordOverflowError` → 507 with structured detail.
- Integration: production `tests.test_context_manager` lifecycle test exercises a typical (not overflow) session — must pass identical to U1.

**Verification:**
- All repo methods accept `tenant_id` / `user_id` kwargs; default `None` preserves U1 behavior.
- Cross-tenant `session_id` collision test fails on legacy; passes on U2 repo.
- Overflow test fires when a synthetic high-cardinality session is loaded.

---

### Phase 6 — DTO / Response Model

- [x] U3. **Define `BenchmarkConversationIngestResponse` Pydantic models**

**Goal:** Add `BenchmarkConversationIngestResponse` and
`BenchmarkConversationIngestRecord` to `src/opencortex/http/models.py`.
Document the `content` field's hydration semantics (in-memory map →
FS fallback per R3-RC-06). Models are added but not yet wired — the
admin route still returns `Dict[str, Any]` until U5.

**Requirements:** R4, R5, R11.

**Dependencies:** None (independent of U1/U2; can land in any order
relative to them, but easiest after U1 to avoid context-switching).

**Files:**
- Modify: `src/opencortex/http/models.py` (append two new model classes
  near the existing `BenchmarkConversationIngestRequest` definition)
- Test: `tests/test_http_models.py` (new — model validation tests)
  OR extend `tests/test_http_server.py` with model-validation
  assertions

**Approach:**
- `BenchmarkConversationIngestRecord` mirrors today's per-record dict
  shape exactly: `uri`, `abstract`, `overview`, `content`, `meta`,
  `abstract_json`, `session_id`, `speaker`, `event_date`, `msg_range`,
  `recomposition_stage`, `source_uri`. Field types match what
  `_export_memory_record` returns today (`content: str` with hydration
  doc, `meta: Dict[str, Any]`, `msg_range: Optional[List[int]]`,
  `recomposition_stage: Optional[str]`, etc.).
- `BenchmarkConversationIngestResponse` mirrors today's top-level
  response: `status: Literal["ok"]`, `session_id: str`,
  `source_uri: Optional[str]`, `summary_uri: Optional[str]`,
  `records: List[BenchmarkConversationIngestRecord]`. The
  `direct_evidence` path adds `ingest_shape: Optional[Literal["direct_evidence"]] = None`
  — keep optional so default `None` covers the merged_recompose path.
- Field docstrings carry the contract: `content` notes the hydration
  rule; `summary_uri` notes that idempotent-hit returns the prior
  run's summary URI when present.

**Patterns to follow:**
- `src/opencortex/http/models.py:116` `MemorySearchResponse` (Pydantic
  response with optional fields).
- `src/opencortex/http/models.py:88` `MemorySearchResultItem` (per-row
  model with optional metadata fields).

**Test scenarios:**
- Happy path: a representative current response dict (from a passing
  `tests/test_http_server.test_04d` payload) validates against the new
  model without modification.
- Happy path: a `direct_evidence` response dict validates with
  `ingest_shape="direct_evidence"`.
- Edge case: empty records list validates.
- Edge case: `source_uri=None` and `summary_uri=None` both validate.
- Error path: response dict missing `status` field fails validation.
- Error path: `status="error"` (not in Literal) fails validation.

**Verification:**
- Models exist; no admin-route or service code changes yet; tests pass.

---

### Phase 3 — Service Layer

- [x] U4. **Extract `BenchmarkConversationIngestService`**

**Goal:** Pull the body of `ContextManager.benchmark_ingest_conversation`
(391 lines) and `_benchmark_ingest_direct_evidence` into a new
`BenchmarkConversationIngestService` class. Six responsibilities split
into private methods on the service. The Service depends on the
Repository (U1+U2) and uses the existing `_BenchmarkRunCleanup` /
`RecompositionError` classes unchanged.

**Requirements:** R6, R7, R8, R9, R12, R13.

**Dependencies:** U1 (repo for response build), U3 (DTO for return
type — though U4 still returns dict and U5 wires the DTO; alternative
is U4 returns DTO directly. See "Approach" for the call.).

**Files:**
- Create: `src/opencortex/context/benchmark_ingest_service.py` (new
  module housing `BenchmarkConversationIngestService`)
- Modify: `src/opencortex/context/manager.py` (replace
  `benchmark_ingest_conversation` body with a thin shim;
  `_benchmark_ingest_direct_evidence` becomes a thin shim too OR is
  deleted because only the Service-internal path calls it)
- Modify: `src/opencortex/context/manager.py` `__init__` to construct
  `self._benchmark_ingest_service` after orchestrator + repo are ready
- Test: `tests/test_benchmark_ingest_service.py` (new — golden parity:
  Service.ingest produces identical response to legacy method for the
  same input)
- Test: existing `tests/test_benchmark_ingest_lifecycle.py` and
  `tests/test_http_server.py` continue to pass without modification

**Approach:**
- Service constructor: `(manager: ContextManager, repo: SessionRecordsRepository)`.
  Takes the manager reference for the helpers that didn't move
  (`_benchmark_recomposition_entries`, `_aggregate_records_metadata`,
  `_merged_leaf_uri`, `_decorate_message_text`, `_persist_rendered_conversation_source`,
  `_mark_source_run_complete`, `_purge_torn_benchmark_run`,
  `_hydrate_record_contents`, `_export_memory_record`, etc.).
- Public method: `async def ingest(*, session_id, tenant_id, user_id,
  segments, include_session_summary, ingest_shape) -> Dict[str, Any]`.
  Returns dict for U4 (DTO wiring is U5); the dict shape is locked
  by the existing tests.
- Six private methods, one per responsibility:
  - `_persist_source(...)` — calls manager's
    `_persist_rendered_conversation_source(..., enforce_transcript_hash=True)`
  - `_normalize_segments(segments) -> List[List[Dict]]` — pure function
  - `_build_merged_leaf_entries(normalized_segments) -> List[entries]`
    — uses `manager._benchmark_recomposition_entries` and
    `manager._build_recomposition_segments`
  - `_write_merged_leaves_with_derive(segments, source_uri, cleanup,
     content_map)` — the per-leaf write loop + derive task scheduling
    + `await asyncio.gather(*tasks)` with sibling-cancel handler
  - `_recompose_and_summarize(session_id, source_uri,
     include_session_summary, cleanup)` — calls
    `manager._run_full_session_recomposition(..., return_created_uris=True)`,
    catches `RecompositionError` and drains URIs into cleanup,
    optionally calls `manager._generate_session_summary`
  - `_build_response(merged_records, source_uri, cleanup,
     content_map) -> Dict[str, Any]` — uses repo + manager helpers
- The dispatch on `ingest_shape == "direct_evidence"` lives in
  `Service.ingest`. The direct_evidence body becomes
  `_ingest_direct_evidence(session_id, tenant_id, user_id, source_uri,
  normalized_segments)` private method.
- Cleanup tracker (`_BenchmarkRunCleanup`) is constructed in
  `Service.ingest` and threaded through `_write_merged_leaves_*`
  and `_recompose_and_summarize`. Same compensation flow as today.
- The thin shim on `ContextManager.benchmark_ingest_conversation`
  becomes:
  ```
  return await self._benchmark_ingest_service.ingest(
      session_id=session_id, tenant_id=tenant_id, user_id=user_id,
      segments=segments,
      include_session_summary=include_session_summary,
      ingest_shape=ingest_shape,
  )
  ```

**Execution note:** Add `tests/test_benchmark_ingest_service.py` with
a golden parity test BEFORE deleting the legacy method body. The
test feeds the same payload to legacy and Service code and asserts
byte-equal response dicts. Land that test, then refactor.

**Patterns to follow:**
- `ContextManager.__init__` for the construction sequence.
- `MemoryOrchestrator.__init__` and its lazy-init pattern for the
  Service if construction order matters.
- `_BenchmarkRunCleanup.compensate` (`manager.py:103-141`) — the
  Service should call it in the same except handlers (CancelledError
  + Exception) currently in `benchmark_ingest_conversation`.

**Test scenarios:**
- Happy path: Service.ingest with a representative LoCoMo-shaped
  payload returns the same response (top-level keys, record count,
  per-record `msg_range`, `source_uri`, `summary_uri`) as the legacy
  ContextManager method for the same input.
- Happy path: idempotent replay (same transcript hash) returns
  identical response on second call (golden test for hash-versioning
  preservation).
- Happy path: 409 conflict path still raises `SourceConflictError`
  unchanged.
- Happy path: torn-replay path still purges and re-ingests (existing
  `test_torn_prior_run_is_not_treated_as_idempotent` must pass
  unchanged).
- Edge case: empty `normalized_segments` returns the same
  empty-records short-circuit as today.
- Error path: monkeypatch `_run_full_session_recomposition` to raise
  `RuntimeError` — Service catches via cleanup compensation, no
  orphan records (existing `test_recompose_failure_cleans_up_merged_leaves`
  must pass).
- Error path: `CancelledError` mid-derive-gather still triggers sibling
  cancellation + cleanup (existing
  `test_cancelled_error_propagates_after_cleanup` must pass).
- Integration: full `tests/test_benchmark_ingest_lifecycle.py` suite
  passes without modification.
- Integration: `tests/test_http_server.test_04d/04e/04f/04g/04h/04i`
  pass without modification (admin route still returns dict; DTO
  comes in U5).
- Integration: production `tests/test_context_manager.py` and
  `tests/test_e2e_phase1.py` pass — the manager-side helpers the
  Service borrows are untouched.

**Verification:**
- `wc -l src/opencortex/context/manager.py` drops by ~390 lines (the
  benchmark_ingest_conversation + _benchmark_ingest_direct_evidence
  bodies move into the Service).
- New service module is < 500 lines (with the 6 private methods +
  direct_evidence + class scaffolding).
- `ContextManager.benchmark_ingest_conversation` is a one-line
  delegate.
- All existing test suites pass identical to pre-U4 master.

---

### Phase 6 (continued) — Wire DTO through the public path

- [x] U5. **Wire `BenchmarkConversationIngestResponse` through Service + admin route**

**Goal:** `Service.ingest` returns `BenchmarkConversationIngestResponse`
instead of `Dict[str, Any]`. Admin route declares the DTO as the
response type. Orchestrator facade preserves the dict signature for
backward compat (returns `model.model_dump()` from the Service result)
or also returns the DTO — choose during implementation.

**Requirements:** R4, R7.

**Dependencies:** U3 (DTO defined), U4 (Service exists).

**Files:**
- Modify: `src/opencortex/context/benchmark_ingest_service.py` (wrap
  the current dict return in `BenchmarkConversationIngestResponse(**dict)`
  or build the model directly)
- Modify: `src/opencortex/orchestrator.py`
  `MemoryOrchestrator.benchmark_conversation_ingest` (return type
  annotation update; possibly call `.model_dump()` to preserve dict
  return for any non-HTTP caller — verify by grep that no in-process
  caller depends on the dict)
- Modify: `src/opencortex/http/admin_routes.py`
  `admin_benchmark_conversation_ingest` (return type annotation
  update from `Dict[str, Any]` → `BenchmarkConversationIngestResponse`;
  FastAPI handles serialization automatically)
- Modify: `src/opencortex/context/manager.py` thin shim returns the
  DTO too (same change)
- Test: existing tests should pass without modification — FastAPI
  serializes Pydantic to identical JSON the dict produced. If a test
  asserts on a field absent from the DTO, fix the DTO to include it.

**Approach:**
- Build the DTO once at the response construction site inside the
  Service (replaces today's `return {...}`). Map `_export_memory_record`
  output to `BenchmarkConversationIngestRecord(**row)` before placing
  in the records list.
- Admin route `-> BenchmarkConversationIngestResponse:` — FastAPI's
  default serializer emits identical JSON.
- Orchestrator facade decision (resolved during implementation): if
  no in-process caller exists (verified by grep), return the DTO. If
  any test or maintenance script expects a dict, return
  `model.model_dump()`. Document the decision inline.

**Patterns to follow:**
- `MemorySearchResponse` is returned directly from the search route
  (`src/opencortex/http/server.py` `memory_search` route). FastAPI
  serializes it.

**Test scenarios:**
- Happy path: HTTP `test_04d` response JSON byte-equal to pre-U5 (the
  DTO must round-trip the same field set).
- Happy path: HTTP `test_04e` (`direct_evidence`) JSON identical.
- Edge case: idempotent-hit response includes `summary_uri` correctly
  (Pydantic serializes `Optional[str]` with `None` as JSON `null`).
- Error path: Pydantic validation rejects a malformed Service-side
  build — surfaces as 500. (Defensive; should never fire if Service
  honors the contract.)
- Integration: full `tests/test_benchmark_ingest_lifecycle.py` and
  `tests/test_locomo_bench.py` pass with the DTO return type.

**Verification:**
- Admin route handler signature shows `-> BenchmarkConversationIngestResponse:`.
- Service `ingest()` return type is the DTO.
- All HTTP tests pass without modification (JSON byte-equivalence).
- A grep for `Dict[str, Any]` in the benchmark code paths returns
  zero hits except where genuinely needed (e.g., `meta` field type).

---

## System-Wide Impact

- **Interaction graph**: New module dependencies — `benchmark_ingest_service.py`
  depends on `manager.py` (helpers), `session_records.py` (repo),
  `http/models.py` (DTO), `_BenchmarkRunCleanup` + `RecompositionError`
  (already in manager.py). No new HTTP routes; no new MCP tools.
- **Error propagation**: New `SessionRecordOverflowError` raised by repo,
  caught at admin route → HTTP 507. `RecompositionError` still raised
  by `_run_full_session_recomposition`, now caught inside Service
  `_recompose_and_summarize` instead of inside the legacy method body.
  All existing exception types (`SourceConflictError`, `CancelledError`,
  generic `Exception`) preserved.
- **State lifecycle risks**: Cleanup tracker (`_BenchmarkRunCleanup`)
  is constructed once per `Service.ingest` call — same scope as
  today's `benchmark_ingest_conversation`. No new shared state. The
  Repository's pagination loop is in-memory; if a request is cancelled
  mid-load, the partial result is discarded — no orphan state.
- **API surface parity**: HTTP `POST /api/v1/admin/benchmark/conversation_ingest`
  response shape unchanged. New 507 status code only for the genuine
  overflow case (50 000+ records under one session — unrealistic on
  benchmark data). Admin route URL, methods, request shape, request
  validation all unchanged.
- **Integration coverage**: Production `context_commit` / `context_end`
  lifecycle uses Repository methods after U1+U2. Mandatory regression:
  `tests.test_e2e_phase1` + `tests.test_context_manager` (the
  conversation lifecycle suite). U4 doesn't add new integration risk
  — the Service is opt-in via `benchmark_conversation_ingest` only.
- **Unchanged invariants**:
  - `POST /api/v1/admin/benchmark/conversation_ingest` URL, request
    shape, response field set, response field semantics, error codes
    (403/409/422/504/410) — all unchanged.
  - `MemoryOrchestrator.benchmark_conversation_ingest` keyword-arg
    signature unchanged.
  - `ContextManager.benchmark_ingest_conversation` keyword-arg
    signature unchanged (thin shim).
  - Production `context_commit` / `context_end` lifecycle unchanged.
  - MCP tool surface (11 tools per agent-native check) unchanged.
  - All cleanup tracker compensation order unchanged.

---

## Risks & Dependencies

| Risk | Mitigation |
|---|---|
| **Repository scope discipline (U2) might break a production caller that doesn't have `tenant_id` / `user_id` available.** | Default kwargs to `None`; legacy callers preserve old behavior. Add scope explicitly only at known call sites. Production `context_end` flow has `set_request_identity` contextvars — tenant/user are always reachable via `get_effective_identity()`; that's the lookup path. |
| **Cursor-based pagination might surface a previously-hidden ordering bug** in the storage adapter. | Repo wraps `storage.scroll`; if scroll has ordering inconsistencies, U2 surfaces them as failing parity tests. Stop and fix at the storage layer if so. |
| **Service extraction (U4) might inadvertently change a response field.** | Golden parity test (`tests/test_benchmark_ingest_service.py`) asserts byte-equal response dict for representative payloads BEFORE the legacy method is replaced. Test must pass before the replacement lands. |
| **DTO wiring (U5) might break a downstream JSON consumer that depends on field-name typos or extra fields.** | `model_extra="allow"` (Pydantic default behavior) preserves any extra fields the response had; the DTO declares the documented set. Test parity is byte-equal JSON, not field-equal Python dict. If a typo escapes documentation, surface it during implementation. |
| **Helper coupling** — Service borrows ~10 helpers from ContextManager via a `manager` reference. Future ContextManager refactors might break the Service. | This is intentional per Key Technical Decisions choice (a). Promotion of helpers to the Service is an explicit follow-up in `Deferred to Follow-Up Work`. |
| **Refactor scope creep** — easy to start "while I'm in here, let me also fix R2-23 (duplicate fs.write_context) / PE-1 (rename _delete_immediate_families) / R3-RC-02 (cross-input-session merge)." | Plan scope is locked to §25 Phases 3+5+6. Additional fixes go in separate PRs per closure tracker action queue. |
| **Production lifecycle regression** — the Repository wraps helpers also called from `_run_full_session_recomposition`, `_finalize_session_end`, recall path. | U1 verification mandates the production lifecycle test suite passes. U2 verification ditto. Any regression is caught before merge. |

---

## Documentation / Operational Notes

- **No CHANGELOG entry** required — pure refactor, no observable behavior change. (HTTP 507 for genuine 50k+ overflow is technically new, but unreachable on real benchmark data; not worth a CHANGELOG line.)
- **Closure tracker update** after merge: flip §25 Phase 3, 5, 6 status to ✅ in `docs/residual-review-findings/2026-04-24-review-closure-tracker.md`. Include the closing PR number and merge SHA.
- **Follow-up plan candidate**: §25 Phase 7 (`benchmarks/adapters/conversation_mapping.py`) is the next logical refactor PR — closes R2-24, R2-33, R4-P2-8/9.
- **`/ce-compound` candidates** (per learnings researcher): document the Service / Repository / DTO extraction pattern after merge (no existing entry).

---

## Sources & References

- **Source review**: `.context/compound-engineering/ce-code-review/20260424-152926-6301c860/REVIEW.md` §25 (Design-Pattern Refactor Plan), §25.1 (Behavior-Preserving Refactor table), §25.2 (Abstraction Guardrails), §26.2 Phase 2 (related deferred items).
- **Closure tracker**: `docs/residual-review-findings/2026-04-24-review-closure-tracker.md` (canonical status of every REVIEW.md item).
- **Prior plans**:
  - `docs/plans/2026-04-25-003-fix-benchmark-conversation-ingest-review-fixes-plan.md` (landed Phases 1, 2, 4)
  - `docs/plans/2026-04-25-004-refactor-benchmark-ingest-p2-residuals-plan.md` (landed P2 polish; introduced `RecompositionEntry` TypedDict)
- **Related PRs (merged)**: #3 (Phase 1), #4 (should-address residuals), #5 (P2 polish), #6 (close defensive + version bump).
- **Branch**: `refactor/server-side-design-patterns` off `master @ 155b218` (post-PR #6 merge).
