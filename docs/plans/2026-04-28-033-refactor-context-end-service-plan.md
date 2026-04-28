---
title: Extract Context End Service
created: 2026-04-28
status: active
type: refactor
origin: "$compound-engineering:lfg 抽离 ContextManager 主链路 end 协调逻辑到 ContextEndService：保留 ContextManager._end 兼容 wrapper，把 merge wait/flush、immediate cleanup、persist conversation source、orchestrator.session_end、autophagy task、full recomposition wait、session summary、layer integrity/fail_fast 处理分段迁出，并保持 prepare/commit 行为不变"
---

# Extract Context End Service

## Problem Frame

`ContextManager._commit()` now delegates to `ContextCommitService`, but
`ContextManager._end()` still mixes every end-phase concern in one method:
merge failure handling, final buffer flush, immediate cleanup, conversation
source persistence, `orchestrator.session_end`, autophagy task scheduling,
full recomposition wait, session summary generation, layer integrity checks,
fail-fast policy, logging, project context, and session cleanup.

This phase extracts that end-phase coordination to `ContextEndService` while
leaving `ContextManager._end()` as a compatibility wrapper. `prepare` and
`commit` behavior must remain unchanged.

## Requirements

- R1: Add `src/opencortex/context/end_service.py` with a
  `ContextEndService` composed into `ContextManager`.
- R2: Keep `ContextManager._end(session_id, tenant_id, user_id, config)` call
  signature and response shape unchanged.
- R3: Move end-phase sub-steps out of `ContextManager._end()`:
  merge wait/flush, immediate cleanup, persist conversation source,
  `orchestrator.session_end`, autophagy scheduling, merge follow-up wait,
  full recomposition wait, session summary generation, layer integrity checks,
  fail-fast handling, duration logging, and final cleanup.
- R4: Preserve collection-scoped session key semantics:
  `(collection, tenant_id, user_id, session_id)`.
- R5: Preserve manager-owned state dictionaries for this phase so idle close,
  commit service, recomposition engine, and existing tests continue to share
  the same state.
- R6: Do not change prepare, commit, benchmark ingest, recomposition internals,
  or HTTP route behavior.

## Scope Boundaries

- No behavior changes to fail-fast vs partial end semantics.
- No changes to full recomposition timeout value.
- No changes to session cleanup timing; cleanup still runs in `finally`.
- No storage schema or record-layer changes.
- No broad rename of manager helper methods; the service may call existing
  manager helpers.

## Current Code Evidence

- `src/opencortex/context/manager.py::_end()` currently owns all end
  orchestration in a single method.
- `tests/test_context_manager.py` covers fail-fast end behavior, session
  summary generation, restored merge buffer flushing, and cleanup assumptions.
- `tests/test_http_server.py` and `tests/test_live_servers.py` cover HTTP
  lifecycle behavior around context/session routes.
- Session bookkeeping must remain collection-scoped per existing repo learning.

## Key Technical Decisions

- Use composition: `ContextManager` lazily owns a `ContextEndService`, matching
  the existing `ContextCommitService` pattern.
- Keep state in `ContextManager` and pass through manager helper methods. This
  avoids a high-risk state migration while still making the main lifecycle
  facade smaller.
- Use a small dataclass for mutable end-run state instead of many `nonlocal`
  variables.
- Keep `ContextManager._end()` as wrapper only, so callers and tests do not
  need to know the service exists.

## Implementation Units

### U1. Add End Service Skeleton

**Goal:** Add service wiring without changing behavior.

**Files:**
- `src/opencortex/context/end_service.py`
- `src/opencortex/context/manager.py`

**Approach:**
- Add `ContextEndService(manager)` with `end(...)`.
- Add `ContextManager._end_service` lazy property.
- Change `_end()` to delegate to `self._end_service.end(...)`.

**Test Scenarios:**
- Existing `_end()` callers still pass.
- Idle close path still calls through `_end()`.

### U2. Move End Sub-Steps

**Goal:** Move end orchestration into readable service helpers.

**Files:**
- `src/opencortex/context/end_service.py`
- `src/opencortex/context/manager.py`

**Approach:**
- Introduce `EndRunState` dataclass for start time, status, source URI, trace
  counts, and fail-fast mode.
- Move fail-fast/partial handling into a service helper.
- Move merge wait/flush, immediate cleanup, session_end, autophagy scheduling,
  recomposition wait, summary generation, and integrity checks into separate
  service methods.
- Preserve final `_cleanup_session(sk)` and `reset_request_project_id(...)`
  behavior in service `finally` blocks.

**Test Scenarios:**
- Fail-fast tests still raise where expected.
- Non-fail-fast degraded end still returns partial status.
- Session summary and recomposition tests still pass.
- Idle close and HTTP context route tests still pass.

### U3. Verification and Review

**Goal:** Complete LFG validation and PR.

**Validation Commands:**
- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`
- `uv run --group dev pytest tests/test_context_manager.py tests/test_noise_reduction.py -q`
- `uv run --group dev pytest tests/test_http_server.py tests/test_live_servers.py -q`

## Risks

| Risk | Mitigation |
|------|------------|
| Session state cleanup changes | Keep cleanup in service `finally` exactly as manager did |
| Project context leaks | Preserve `set_request_project_id` / `reset_request_project_id` scope |
| Fail-fast semantics drift | Keep one fail-fast handler helper and run existing fail-fast tests |
| Recomposition wait behavior changes | Preserve spawn/wait/timeout/cancel order |
| Commit service and end service diverge on state ownership | Keep state dictionaries manager-owned |

## Observed Results

- Added `ContextEndService` and `EndRunState` in
  `src/opencortex/context/end_service.py`.
- Kept `ContextManager._end(...)` as a compatibility wrapper delegating to the
  end service.
- Preserved manager-owned session state and collection-scoped session keys.
- Validation:
  - `uv run --group dev ruff format --check .`
  - `uv run --group dev ruff check .`
  - `uv run --group dev pytest tests/test_context_manager.py -q`
  - `uv run --group dev pytest tests/test_noise_reduction.py tests/test_http_server.py tests/test_live_servers.py -q`
  - `uv run --group dev pytest -q`
