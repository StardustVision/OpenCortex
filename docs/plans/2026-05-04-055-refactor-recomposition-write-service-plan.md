---
status: completed
created: 2026-05-04
origin: user request
scope: session recomposition write/persistence extraction
---

# Refactor Recomposition Write Service

## Problem

`src/opencortex/context/recomposition_engine.py` is still the largest
session recomposition hotspot after extracting segmentation, input, task,
and state services. It now mixes recomposition orchestration with record
persistence details: stable URI construction, directory record writes,
session summary writes, Qdrant keyword patching, CortexFS writes, and
deferred derive task creation for merged leaves.

The user wants `SessionRecompositionEngine` to keep orchestration and
error handling while write/persistence mechanics move behind composition.

## Scope

In scope:

- Add `src/opencortex/context/recomposition_write.py` with
  `RecompositionWriteService`.
- Move stable recomposition URI construction for merged leaves and
  directories into the write service.
- Move directory record persistence, keywords patching, and CortexFS write
  behavior into the write service.
- Move session summary persistence, keywords patching, and CortexFS write
  behavior into the write service.
- Move merged leaf `orchestrator.add(... defer_derive=True)` and deferred
  derive task creation into the write service.
- Keep `SessionRecompositionEngine` compatibility wrappers for
  `_merged_leaf_uri` and `_directory_uri` if current tests or callers use them.
- Preserve existing `ContextManager` and `BenchmarkConversationIngestService`
  behavior.

Out of scope:

- Changing segmentation, clustering, LLM derive, eligibility selection, or
  error handling.
- Removing `ContextManager` or `SessionRecompositionEngine` compatibility
  wrappers.
- Reworking benchmark-only ingestion or cleanup tracking.
- Changing defer-derive concurrency semantics.

## Design

`SessionRecompositionEngine` remains the coordinator:

- It loads merged records, builds recomposition entries/segments, performs
  full/merge control flow, owns request identity context, and handles rollback
  errors.
- It calls `RecompositionWriteService` for persistence operations.

`RecompositionWriteService` owns:

- `merged_leaf_uri(...)`
- `directory_uri(...)`
- `write_directory_record(...) -> str`
- `write_session_summary(...) -> str`
- `write_merged_leaf(...) -> str`
- `track_deferred_derive(...)` through an injected callback or by accepting the
  current `SessionKey`.
- Private helpers for `patch_keywords(...)`, `write_fs_context(...)`, and
  bounded deferred derive execution.

Decision: the write service can take the `ContextManager` back-reference, as
other recomposition services already do. This keeps behavior stable and avoids
introducing a larger persistence abstraction during a cleanup pass.

Decision: `SessionRecompositionEngine` keeps wrapper methods for
`_merged_leaf_uri` and `_directory_uri` delegating to the write service. This
matches the current repo pattern of preserving private compatibility seams while
internals are being split.

## Files

Modify:

- `src/opencortex/context/recomposition_engine.py`

Create:

- `src/opencortex/context/recomposition_write.py`
- `tests/test_recomposition_write.py`

Potentially update:

- `tests/test_context_manager.py`
- `tests/test_conversation_merge.py`

## Test Plan

Focused tests:

- `uv run --group dev pytest tests/test_recomposition_write.py -q`
- `uv run --group dev pytest tests/test_context_manager.py tests/test_conversation_merge.py -q`
- `uv run --group dev pytest tests/test_recomposition_state.py tests/test_recomposition_input.py -q`

Static checks:

- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`

LFG checks:

- `ce-code-review mode:autofix plan:docs/plans/2026-05-04-055-refactor-recomposition-write-service-plan.md`
- `ce-test-browser mode:pipeline`
- Commit, push, and open PR.

## Risks

- Deferred derive task ownership is behavior-sensitive: the task must still be
  tracked under the same session key so `EndService` can wait for follow-up
  failures.
- Directory/session summary keyword patching currently happens after
  `orchestrator.add()`. The extraction must preserve that order because
  `add()` fast-path skips keywords for these records.
- CortexFS writes are paired with Qdrant upserts. The service split must not
  turn them into fire-and-forget operations or change existing exception
  propagation.
