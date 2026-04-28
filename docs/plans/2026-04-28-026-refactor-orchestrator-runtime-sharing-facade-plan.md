---
title: Refactor Orchestrator Runtime Sharing And Facade Organization
created: 2026-04-28
status: completed
type: refactor
origin: "$compound-engineering:lfg 抽 MemoryOrchestrator 的 embedder/rerank/bootstrap helper；抽 promote_to_shared sharing/admin mutation；整理 orchestrator facade wrapper 分组"
---

# Refactor Orchestrator Runtime Sharing And Facade Organization

## Problem Frame

`MemoryOrchestrator` has already shed most write, retrieval, session, trace, and
record projection domain logic, but it still owns a few unrelated runtime and
admin responsibilities:

- embedder fallback, hybrid wrapping, cache wrapping, and rerank config/client
  helpers;
- `promote_to_shared` sharing/admin mutation behavior;
- a long wrapper area whose compatibility delegates are hard to scan.

This phase should move the remaining domain logic into focused services while
keeping existing orchestrator methods as compatibility wrappers.

## Requirements

- R1: Extract embedder fallback, hybrid/cache wrapping, rerank config/client,
  and bootstrap-adjacent runtime helper behavior from `MemoryOrchestrator`.
- R2: Keep orchestrator compatibility wrappers for all moved runtime helper
  methods because tests and services patch or call them directly.
- R3: Update bootstrap ownership text so runtime wrapping no longer appears to
  be intentionally owned by `MemoryOrchestrator`.
- R4: Extract `promote_to_shared` sharing/admin mutation logic from
  `MemoryOrchestrator`.
- R5: Preserve `promote_to_shared` response shape, storage mutations,
  per-memory error handling, and ACL/visibility behavior.
- R6: Reorganize the orchestrator facade wrapper area into clearer groups
  without deleting compatibility methods.
- R7: Preserve public HTTP/client behavior and existing monkeypatch seams.
- R8: Run focused runtime, sharing, and memory facade tests plus style gates.

## Scope Boundaries

- Do not redesign embedder provider selection, model defaults, or rerank scoring.
- Do not move retrieval/search/list/index logic again in this phase.
- Do not remove compatibility wrappers from `MemoryOrchestrator`.
- Do not change HTTP route schemas or client method signatures.
- Do not add new sharing concepts beyond moving the current
  `promote_to_shared` behavior.
- Do not touch frontend code unless a backend change unexpectedly requires it.

## Current Code Evidence

- `src/opencortex/orchestrator.py` is over 1700 lines and still contains
  runtime helper implementations near the initialization section.
- `src/opencortex/lifecycle/bootstrapper.py` currently calls orchestrator
  `_wrap_with_hybrid` and `_wrap_with_cache` wrappers while documenting that
  those helpers are not bootstrapper-owned.
- `src/opencortex/orchestrator.py` implements `promote_to_shared` directly even
  though memory write/query/session/record responsibilities now live in
  services.
- `src/opencortex/http/server.py` calls `_orchestrator.promote_to_shared(...)`,
  so the orchestrator method must remain.
- Existing tests patch runtime wrappers such as `_wrap_with_cache`,
  `_get_immediate_fallback_embedder`, and `_build_rerank_config`.

## Key Technical Decisions

- Add `src/opencortex/services/model_runtime_service.py` as the owner for
  embedder fallback/wrapping and rerank runtime helpers.
- Keep `MemoryOrchestrator` runtime helper methods as thin delegates to the new
  runtime service.
- Preserve bootstrapper calls through orchestrator wrappers so monkeypatches on
  the orchestrator instance keep working.
- Add `src/opencortex/services/memory_sharing_service.py` as the owner for
  sharing/admin mutations, starting with `promote_to_shared`.
- Keep `MemoryOrchestrator.promote_to_shared(...)` as a wrapper for HTTP and
  client compatibility.
- Organize facade wrappers by responsibility with section headers rather than
  deleting or renaming public compatibility methods.

## Implementation Units

### U1. Extract model runtime helpers

**Goal:** Move embedder fallback/wrapping and rerank runtime helper bodies out
of `MemoryOrchestrator`.

**Files:**
- Add: `src/opencortex/services/model_runtime_service.py`
- Modify: `src/opencortex/orchestrator.py`
- Modify: `src/opencortex/lifecycle/bootstrapper.py`

**Approach:**
- Add a lazy `_model_runtime_service` property on `MemoryOrchestrator`.
- Move `_is_retryable_immediate_embed_exception`,
  `_create_immediate_fallback_embedder`, `_get_immediate_fallback_embedder`,
  `_wrap_with_hybrid`, `_wrap_with_cache`, `_get_or_create_rerank_client`, and
  `_build_rerank_config` implementations into `ModelRuntimeService`.
- Replace the orchestrator methods with wrappers that delegate to the service.
- Keep bootstrapper call sites routed through orchestrator wrappers.
- Update bootstrapper documentation to reflect the new runtime service owner.

**Test Scenarios:**
- Immediate fallback embedder behavior still handles retryable local embed
  failures.
- Embedder cache and hybrid wrapper tests can still monkeypatch orchestrator
  wrapper methods.
- Rerank config/client lifecycle tests still pass and reuse the same client
  cache semantics.

### U2. Extract sharing/admin mutation service

**Goal:** Move `promote_to_shared` out of `MemoryOrchestrator`.

**Files:**
- Add: `src/opencortex/services/memory_sharing_service.py`
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
- Add a lazy `_memory_sharing_service` property on `MemoryOrchestrator`.
- Move the current `promote_to_shared` implementation into
  `MemorySharingService`.
- Preserve lookup by `memory_ids`, project scoping, `shared_to` mutation,
  storage update, promoted count, total count, and per-item error collection.
- Leave `MemoryOrchestrator.promote_to_shared(...)` as a compatibility wrapper.

**Test Scenarios:**
- Existing HTTP route tests can still call the orchestrator method.
- Shared memory mutation preserves output keys: `status`, `promoted`, `total`,
  and `errors`.
- A failed memory update records an error without stopping later IDs.

### U3. Organize orchestrator facade wrappers

**Goal:** Make the remaining orchestrator facade easier to scan without
breaking compatibility.

**Files:**
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
- Group wrapper blocks under concise section headers:
  - bootstrap/runtime;
  - background/system;
  - memory write/query/retrieval;
  - scoring/session/trace/knowledge;
  - record/sharing/admin.
- Keep method names, signatures, and delegation targets unchanged except where
  U1/U2 intentionally move implementations.
- Avoid broad reorder if a small header cleanup is enough to make ownership
  clear.

**Test Scenarios:**
- Importing `MemoryOrchestrator` still works.
- Existing tests that patch or call orchestrator wrappers still find the same
  method names.
- Line count drops from moving real logic; remaining wrapper area is visibly
  grouped.

### U4. Validation, review, browser gate, and PR

**Goal:** Complete the LFG pipeline with focused verification and durable PR.

**Files:**
- No planned production files beyond U1-U3.

**Validation Commands:**
- `uv run --group dev pytest tests/test_perf_fixes.py tests/test_rerank_client_lifecycle.py tests/test_system_status_service.py tests/test_conversation_immediate.py -q`
- `uv run --group dev pytest tests/test_http_server.py -q`
- `uv run --group dev pytest tests/test_memory_service.py tests/test_e2e_phase1.py -q`
- `uv run --group dev ruff check .`
- `uv run --group dev ruff format --check .`

## Risks

| Risk | Mitigation |
|------|------------|
| Tests patch orchestrator runtime helpers | Keep wrappers and preserve bootstrapper calls through them |
| Rerank client cache semantics drift | Move body mechanically and run lifecycle/system status tests |
| Sharing mutation response shape changes | Move body mechanically and run HTTP tests |
| Service back-reference creates import cycle | Use `TYPE_CHECKING` and local imports only where needed |
| Facade cleanup accidentally removes compatibility | Restrict cleanup to grouping and comments, not method removal |

## Observed Results

- `MemoryOrchestrator`: 1617 lines, down from 1737.
- `ModelRuntimeService`: 151 lines.
- `MemorySharingService`: 69 lines.
- Code review found and fixed a moved-over `promote_to_shared` data-loss bug:
  storage upsert already updates the same point id, so deleting the old id after
  upsert could delete the promoted record.
- `uv run --group dev pytest tests/test_perf_fixes.py tests/test_rerank_client_lifecycle.py tests/test_system_status_service.py tests/test_conversation_immediate.py tests/test_memory_sharing_service.py -q`: 39 passed.
- `uv run --group dev pytest tests/test_http_server.py -q`: 20 passed.
- `uv run --group dev pytest tests/test_memory_service.py tests/test_e2e_phase1.py -q`: 54 passed; existing CortexFS background-task pending warnings printed after success.
- `uv run --group dev ruff check .`: pass.
- `uv run --group dev ruff format --check .`: pass.
