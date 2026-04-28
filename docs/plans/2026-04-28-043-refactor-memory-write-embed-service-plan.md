---
status: active
created: 2026-04-28
task: refactor-memory-write-embed-service
---

# Refactor Memory Write Embed Service Plan

## Problem

`MemoryWriteService.add` still owns the write-path embedding mechanics directly:
checking for an embedder, offloading the synchronous embed call with
`run_in_executor`, measuring embed timing, assigning `ctx.vector`, and deriving
the sparse vector passed to persistence. Recent write-path refactors moved
derive, context assembly, dedup, and store persistence behind focused services;
embedding should follow the same composition boundary so `add` remains a
workflow coordinator.

## Scope

In scope:

- Add `src/opencortex/services/memory_write_embed_service.py`.
- Move normal `add` embedding mechanics into `MemoryWriteEmbedService`.
- Return a small structured result containing `embed_ms` and `sparse_vector`.
- Keep `MemoryWriteService.add` responsible for orchestration and timing log
  fields.
- Add focused unit tests for embedder-present, embedder-absent, dense-only, and
  sparse-vector behavior.
- Run targeted tests plus style checks for changed Python files.

Out of scope:

- Reworking `MemoryWriteService.update` embedding.
- Reworking parent directory record embedding in `_ensure_parent_records`.
- Changing `/api/v1/memory/store` behavior, vectorization text selection,
  dedup semantics, or store persistence payloads.

## Existing Patterns

- `src/opencortex/services/memory_write_derive_service.py` uses a small
  dataclass result and a service bound to `MemoryWriteService`.
- `src/opencortex/services/memory_write_context_builder.py` owns context and
  payload assembly while leaving `add` as coordinator.
- `src/opencortex/services/memory_store_record_service.py` accepts
  `sparse_vector` as a dependency rather than recomputing embedding.

## Implementation Units

1. `src/opencortex/services/memory_write_embed_service.py`
   - Define `MemoryWriteEmbedResult`.
   - Implement `embed_for_write(ctx: Context)` with no-op behavior when the
     orchestrator has no embedder.
   - Offload `orch._embedder.embed(ctx.get_vectorization_text())` through the
     event loop executor.
   - Assign `ctx.vector` from the dense result.
   - Return `embed_ms` and sparse vector, if present.

2. `src/opencortex/services/memory_write_service.py`
   - Add the lazy `_write_embed_service` property.
   - Replace the inline embed block in `add` with
     `await self._write_embed_service.embed_for_write(ctx)`.
   - Pass the returned sparse vector into `MemoryStoreRecordService`.
   - Keep existing log fields and dedup checks unchanged.

3. `tests/test_memory_write_embed_service.py`
   - Verify no embedder returns `embed_ms == 0`, no sparse vector, and leaves
     `ctx.vector` unset.
   - Verify a dense embedder receives `ctx.get_vectorization_text()` and assigns
     `ctx.vector`.
   - Verify sparse vectors are returned for store persistence.
   - Verify timing is reported as an integer.

## Validation

- `uv run --group dev pytest tests/test_memory_write_embed_service.py -q`
- `uv run --group dev pytest tests/test_memory_write_context_builder.py tests/test_memory_store_record_service.py tests/test_vectorization_expansion.py -q`
- `uv run --group dev ruff format --check src/opencortex/services/memory_write_service.py src/opencortex/services/memory_write_embed_service.py tests/test_memory_write_embed_service.py`
- `uv run --group dev ruff check src/opencortex/services/memory_write_service.py src/opencortex/services/memory_write_embed_service.py tests/test_memory_write_embed_service.py`

## Risks

- The sparse vector must continue to be passed only when the embed result
  actually has one.
- The dedup path depends on `ctx.vector`; the new service must assign it before
  dedup runs.
- Timing logs should preserve the existing `embed` field semantics in
  milliseconds.
