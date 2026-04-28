---
status: active
created: 2026-04-28
task: refactor-memory-directory-record-service
---

# Refactor Memory Directory Record Service Plan

## Problem

`MemoryWriteService.add()` is mostly a coordinator after the recent derive,
context-builder, embed, dedup, and store-record extractions. The remaining
write-path support block inside `MemoryWriteService` is
`_ensure_parent_records()`, which still owns parent-directory ancestor walking,
directory existence checks, directory `Context` construction, directory-name
embedding, scope/source metadata population, and vector-store upserts.

That logic is domain-specific record construction and persistence. Keeping it in
`MemoryWriteService` makes the write service carry both top-level orchestration
and a separate directory-record persistence path.

## Scope

In scope:

- Add `src/opencortex/services/memory_directory_record_service.py`.
- Move `_ensure_parent_records()` domain logic into
  `MemoryDirectoryRecordService.ensure_parent_records(parent_uri)`.
- Preserve the existing `MemoryWriteService._ensure_parent_records()` wrapper so
  `MemoryService._ensure_parent_records()` remains compatible.
- Keep `MemoryWriteService.add()` behavior unchanged: it only calls the
  compatibility wrapper when `is_leaf and parent_uri`.
- Add focused unit tests for ancestor walking, existing-directory short-circuit,
  directory context payload fields, dense/sparse embedding, and invalid URI
  handling.

Out of scope:

- Changing URI semantics or parent derivation.
- Changing normal leaf record persistence in `MemoryStoreRecordService`.
- Changing update/remove/document/batch write flows.
- Changing public HTTP/API behavior.

## Existing Patterns

- `src/opencortex/services/memory_write_embed_service.py` wraps write-path
  embedding behind a small service while preserving `ctx.vector` semantics.
- `src/opencortex/services/memory_store_record_service.py` owns record assembly
  and persistence details for normal store records.
- `src/opencortex/services/memory_write_service.py` already uses lazy properties
  for focused collaborator services.

## Implementation Units

1. `src/opencortex/services/memory_directory_record_service.py`
   - Define `MemoryDirectoryRecordService`.
   - Bind it to `MemoryWriteService` to match the existing service composition
     pattern.
   - Implement `ensure_parent_records(parent_uri)` by moving the existing
     ancestor walk, storage filter, directory `Context` creation, directory-name
     embed, record field population, and upsert logic.
   - Use `asyncio.get_running_loop().run_in_executor(...)` for embedding.
   - Preserve existing logging message and no-op behavior for invalid URI
     ancestors.

2. `src/opencortex/services/memory_write_service.py`
   - Add a lazy `_directory_record_service` property.
   - Replace `_ensure_parent_records()` body with a compatibility delegate.
   - Remove imports that are only needed by the moved implementation.

3. `tests/test_memory_directory_record_service.py`
   - Verify missing ancestor directories are created from root-to-leaf order.
   - Verify an existing directory short-circuits traversal and only creates
     missing descendants.
   - Verify directory records include vector/sparse vector when the embedder
     returns them.
   - Verify no embedder still creates directory records with scope/source fields.
   - Verify invalid parent URIs do not upsert records.

## Validation

- `uv run --group dev pytest tests/test_memory_directory_record_service.py -q`
- `uv run --group dev pytest tests/test_memory_write_embed_service.py tests/test_memory_store_record_service.py tests/test_vectorization_expansion.py -q`
- `uv run --group dev pytest tests/test_http_server.py::TestHTTPServer::test_02_store -q`
- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`

## Risks

- Parent records must still be created top-down so child `parent_uri` links
  remain valid.
- Scope and source identity fields must stay present or retrieval filters can
  hide directory records.
- The compatibility wrapper must remain because `MemoryService` and tests still
  call `_ensure_parent_records()` through the facade.
