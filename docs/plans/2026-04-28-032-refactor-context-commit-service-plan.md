---
title: Extract Context Commit Service
created: 2026-04-28
status: active
type: refactor
origin: "$compound-engineering:lfg 抽离 ContextManager 主链路 commit 协调逻辑到 ContextCommitService：保留 ContextManager._commit 兼容 wrapper，把 observer record/fallback、cited reward、skill citation validation、immediate write、conversation buffer append、merge trigger 分段迁出，并保持 prepare/end 行为不变"
---

# Extract Context Commit Service

## Problem Frame

`ContextManager.handle()` is already a clean phase dispatcher and
`_prepare()` is mostly a readable orchestration flow. The main lifecycle
hotspot is `_commit()`: it mixes duplicate detection, observer recording,
fallback persistence, cited reward scheduling, skill citation validation,
immediate memory writes, conversation buffer mutation, and merge triggering in
one large method.

This phase extracts that commit coordination into a dedicated
`ContextCommitService` while preserving `ContextManager._commit()` as the
compatibility wrapper used by `handle()` and tests. `prepare` and `end` behavior
must remain unchanged.

## Requirements

- R1: Add `src/opencortex/context/commit_service.py` with a
  `ContextCommitService` that owns commit-phase coordination.
- R2: Keep `ContextManager._commit()` public/internal contract unchanged and
  delegate to the service.
- R3: Move these commit sub-steps out of `ContextManager._commit()`:
  observer record/fallback, duplicate turn handling, cited reward scheduling,
  skill citation validation/event append, immediate write fan-out, conversation
  buffer append, and merge trigger.
- R4: Preserve collection-scoped session key semantics and existing
  `_conversation_buffers` state shape so current tests and end flow keep
  working.
- R5: Do not change `prepare`, `end`, recomposition, benchmark ingest, or HTTP
  route behavior.
- R6: Keep the extracted service thinly coupled to `ContextManager` for this
  phase; do not introduce a broad state object migration yet.

## Scope Boundaries

- No behavioral changes to accepted commit payloads, fallback format, reward
  timing, merge thresholds, or duplicate response shape.
- No changes to `ContextManager._end()` except whatever import/property wiring
  is required.
- No database/storage schema changes.
- No test rewrites beyond focused coverage/import adjustments caused by the
  extraction.

## Current Code Evidence

- `src/opencortex/context/manager.py::_commit()` currently spans duplicate
  detection, observer write, reward scheduling, skill event handling, immediate
  writes, buffer mutation, and merge trigger.
- `tests/test_context_manager.py` and `tests/test_noise_reduction.py` inspect
  `ContextManager._conversation_buffers`, so the service must mutate the same
  manager-owned dictionaries.
- `ContextManager._end()` reads `_conversation_buffers` and waits merge tasks;
  commit extraction must keep those structures compatible.
- Session keys are collection-scoped `(collection, tenant_id, user_id,
  session_id)` and must continue to be built by `ContextManager._make_session_key`.

## Key Technical Decisions

- Use a service with a manager back-reference, matching existing extraction
  style used by `BenchmarkConversationIngestService` and recomposition helpers.
- Keep manager-owned state in place during this phase. Moving
  `_conversation_buffers` / `_committed_turns` into a separate state object can
  happen later once commit/end are both service-backed.
- Keep `_commit()` as a wrapper so callers do not need to know the service
  exists.
- Split commit service internals by business step rather than by tiny helper
  line-count reductions.

## Implementation Units

### U1. Add Commit Service Skeleton

**Goal:** Create the service and lazy manager property without changing
behavior.

**Files:**
- `src/opencortex/context/commit_service.py`
- `src/opencortex/context/manager.py`

**Approach:**
- Add `ContextCommitService(manager)` with `commit(...)`.
- Add `ContextManager._commit_service` lazy property.
- Change `ContextManager._commit()` to delegate to `self._commit_service.commit(...)`.

**Test Scenarios:**
- Existing context manager commit tests still call `_commit()` successfully.

### U2. Move Commit Sub-Steps

**Goal:** Move commit orchestration from manager into service helpers.

**Files:**
- `src/opencortex/context/commit_service.py`
- `src/opencortex/context/manager.py`

**Approach:**
- Move duplicate response handling into service.
- Move observer write + fallback call into `_record_observer_batch`.
- Move cited reward task scheduling into `_schedule_cited_rewards`.
- Move skill citation validation into `_record_valid_skill_citations`.
- Move immediate write item construction/fan-out/buffer append into helpers.
- Keep calls to existing manager helpers such as `_write_fallback`,
  `_decorate_message_text`, `_estimate_tokens`, `_spawn_merge_task`,
  `_append_skill_event`, and `_apply_cited_rewards`.

**Test Scenarios:**
- Duplicate commit returns same response shape.
- Observer failure still returns `write_status="fallback"` and writes fallback.
- Tool calls are still stored on assistant messages.
- Merge trigger behavior remains unchanged.

### U3. Verification and Review

**Goal:** Validate behavior and complete LFG gates.

**Validation Commands:**
- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`
- `uv run --group dev pytest tests/test_context_manager.py tests/test_noise_reduction.py -q`
- `uv run --group dev pytest tests/test_http_server.py tests/test_live_servers.py -q`

## Risks

| Risk | Mitigation |
|------|------------|
| Service loses manager state compatibility | Keep state dictionaries manager-owned and mutate them through the manager |
| Duplicate/observer fallback behavior changes | Preserve exact response shapes and run existing commit/fallback tests |
| Skill citation validation gets bypassed | Move logic verbatim and keep server-selected URI lookup unchanged |
| Merge timing changes | Keep threshold and `_spawn_merge_task` call in the same post-buffer-update position |

## Observed Results

- Added `src/opencortex/context/commit_service.py`.
- `ContextManager._commit()` now delegates to `ContextCommitService.commit(...)`
  and preserves the same call signature/response shape.
- Commit sub-steps moved out of `ContextManager`: duplicate handling, observer
  record/fallback, cited reward task scheduling, skill citation validation,
  immediate write fan-out, buffer append, and merge trigger.
- Immediate write preparation uses a frozen dataclass in the service rather
  than positional tuples, keeping the extracted workflow readable and
  Pythonic.
- `ContextManager` still owns session dictionaries and conversation buffers so
  prepare/end/tests keep the same state shape.
- Validation passed:
  - `uv run --group dev ruff format --check .`
  - `uv run --group dev ruff check .`
  - `uv run --group dev pytest tests/test_context_manager.py tests/test_noise_reduction.py -q`
  - `uv run --group dev pytest tests/test_http_server.py tests/test_live_servers.py -q`
