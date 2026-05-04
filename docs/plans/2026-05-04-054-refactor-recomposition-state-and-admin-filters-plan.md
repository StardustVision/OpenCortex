---
status: completed
created: 2026-05-04
origin: user request
scope: session recomposition cleanup and admin filter expression cleanup
---

# Refactor Recomposition State and Admin Filters

## Problem

`src/opencortex/context/recomposition_engine.py` is still over 1000 lines after prior segmentation, task, and input extractions. It now mixes core recomposition orchestration with stateful buffer snapshot/restore, immediate-record cleanup, and storage purge details. That makes the remaining session recomposition path harder to read and keeps a large class as the main complexity sink.

Admin/list/stats paths also still build storage filter dictionaries by hand. These are not on the store/recall hot path, but they should use the same typed filter expression helper introduced for mainline recall/list behavior.

## Scope

In scope:

- Extract recomposition state/storage helpers from `SessionRecompositionEngine` into a focused service.
- Keep `SessionRecompositionEngine` compatibility wrappers so existing `ContextManager`, benchmark, and tests keep their current call surface.
- Preserve merge/full recomposition behavior, task lifecycle behavior, and storage/CortexFS cleanup semantics.
- Replace remaining admin/list/stats hand-written memory filter dicts with `FilterExpr` composition.

Out of scope:

- Changing merge segmentation, LLM derivation, directory creation, or session summary algorithms.
- Removing compatibility wrappers from `ContextManager` or `SessionRecompositionEngine`.
- Changing admin endpoint authorization, pagination, ordering, or visibility behavior.
- Broad cleanup of transport-specific metadata dictionaries.

## Current Patterns

- `src/opencortex/context/recomposition_input.py` owns recomposition input and record assembly.
- `src/opencortex/context/recomposition_segmentation.py` owns pure segmentation helpers.
- `src/opencortex/context/recomposition_tasks.py` owns async task lifecycle state.
- `src/opencortex/services/memory_filters.py` provides `FilterExpr` and shared memory ACL/filter builders.

## Implementation Units

### 1. Recomposition State Service

Create `src/opencortex/context/recomposition_state.py`.

Responsibilities:

- Deduplicate and purge records plus CortexFS subtrees by URI prefix.
- List current session immediate URIs for fallback cleanup.
- Load immediate records through `RecompositionInputService`.
- Read merge trigger threshold from config.
- Detach and restore `ConversationBuffer` snapshots under the merge lock.

`SessionRecompositionEngine` should compose this service and keep wrappers:

- `_purge_records_and_fs_subtree`
- `_list_immediate_uris`
- `_load_immediate_records`
- `_merge_trigger_threshold`
- `_take_merge_snapshot`
- `_restore_merge_snapshot`

Decision: keep URI construction and merge/full recomposition orchestration in the engine for this pass. Those are still part of session recomposition behavior and can be extracted later if the next review shows a clean boundary.

### 2. Admin Filter Expression Cleanup

Update:

- `src/opencortex/services/memory_query_service.py`
- `src/opencortex/services/memory_admin_stats_service.py`

Use `FilterExpr` from `src/opencortex/services/memory_filters.py` for the existing admin filter shapes:

- Always exclude `context_type=staging`.
- Add tenant/user/category/context type clauses only when current code already does.
- Preserve admin list ordering, offset, and returned fields.

Decision: do not use `memory_visibility_filter` for admin paths because admin list intentionally has no scope isolation and stats currently filter exact tenant/user without project visibility.

## Test Plan

Focused tests:

- `uv run --group dev pytest tests/test_conversation_merge.py tests/test_recomposition_input.py -q`
- `uv run --group dev pytest tests/test_context_manager.py -q`
- `uv run --group dev pytest tests/test_memory_filters.py tests/test_memory_admin_stats_service.py -q`

Static gates:

- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`

LFG gates:

- Run `ce-code-review mode:autofix plan:docs/plans/2026-05-04-054-refactor-recomposition-state-and-admin-filters-plan.md`.
- Persist any review autofixes before continuing.
- Run `ce-test-browser mode:pipeline`.
- Commit, push, and open/update PR.

## Risks

- Snapshot/restore ordering is behavior-sensitive because merge failures prepend the detached snapshot back before newer messages. Tests should keep this exact ordering.
- Purge failures are intentionally best-effort for CortexFS but storage removal remains authoritative. The extraction must keep that failure policy.
- Admin filters should only change construction style, not visibility semantics.
