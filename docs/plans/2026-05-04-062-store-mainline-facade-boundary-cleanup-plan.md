---
status: completed
created: 2026-05-04
origin: user request
scope: store mainline CortexMemory facade boundary cleanup
---

# Store Mainline Facade Boundary Cleanup

## Problem

The normal `/api/v1/memory/store` path now has a clear staged flow in
`MemoryWriteService.add`, but several write-path helper services still reach
through `CortexMemory` for storage, filesystem, embedding, record projection,
signals, and URI helpers. That makes the store mainline look like it still
depends on the top-level compatibility facade rather than on the write domain
boundary.

The goal is to let the store mainline depend on `MemoryWriteService` as its
local boundary. `CortexMemory` should keep compatibility wrappers, but the
normal store helpers should stop calling `self._write_service._orch` directly.

## Scope

In scope:

- Add narrow dependency accessors on
  `src/opencortex/services/memory_write_service.py` for store helpers:
  storage, collection, filesystem, embedder, config, signal bus, entity index,
  record/URI helpers, and derived projection sync.
- Update normal store-path helper services to call the write-service boundary
  instead of `CortexMemory`:
  - `src/opencortex/services/memory_write_context_builder.py`
  - `src/opencortex/services/memory_write_derive_service.py`
  - `src/opencortex/services/memory_write_embed_service.py`
  - `src/opencortex/services/memory_write_dedup_service.py`
  - `src/opencortex/services/memory_directory_record_service.py`
  - `src/opencortex/services/memory_store_record_service.py`
- Remove `_orch` properties from those helper services when no longer needed.
- Preserve `CortexMemory` public and compatibility wrappers.
- Preserve `/api/v1/memory/store` behavior, dedup behavior, projection sync,
  parent directory records, signals, and CortexFS fire-and-forget semantics.

Out of scope:

- Update/remove mutation cleanup in `MemoryMutationService`.
- Document ingest cleanup in `MemoryDocumentWriteService`.
- Batch/document benchmark paths.
- Deleting `CortexMemory` compatibility methods.
- Changing storage filter, TTL, projection, or entity-index semantics.

## Implementation Units

### 1. Add Write-Service Boundary Methods

Add focused accessors/delegates to `MemoryWriteService` for exactly the
dependencies needed by the normal store helpers:

- initialization and collection lookup
- storage and filesystem
- embedder and config
- memory signal bus and entity index
- URI/category helpers
- abstract/object payload helpers
- derive helpers used by write derive
- record loading for explicit URI/dedup
- TTL and anchor projection sync

These delegates may still call `CortexMemory` internally. The cleanup target is
that store helper services depend on `MemoryWriteService`, not the top-level
facade.

### 2. Move Store Helpers Off `_orch`

Change the normal store helper services listed in scope to call the new
write-service boundary. Keep argument order and result payloads unchanged.

### 3. Update Tests to Assert the New Boundary

Adjust focused tests such as `tests/test_memory_store_record_service.py` so the
test double represents `MemoryWriteService` directly instead of a nested
`_orch` object. Existing behavioral assertions should remain the same.

## Test Plan

Focused tests:

- `uv run --group dev pytest tests/test_memory_store_record_service.py tests/test_memory_write_context_builder.py tests/test_memory_write_derive_service.py tests/test_memory_write_embed_service.py tests/test_memory_write_dedup_service.py tests/test_memory_directory_record_service.py -q`
- `uv run --group dev pytest tests/test_write_dedup.py tests/test_http_server.py -q`

Static checks:

- `uv run --group dev ruff format --check src/opencortex/services/memory_write_service.py src/opencortex/services/memory_write_context_builder.py src/opencortex/services/memory_write_derive_service.py src/opencortex/services/memory_write_embed_service.py src/opencortex/services/memory_write_dedup_service.py src/opencortex/services/memory_directory_record_service.py src/opencortex/services/memory_store_record_service.py tests/test_memory_store_record_service.py`
- `uv run --group dev ruff check src/opencortex/services/memory_write_service.py src/opencortex/services/memory_write_context_builder.py src/opencortex/services/memory_write_derive_service.py src/opencortex/services/memory_write_embed_service.py src/opencortex/services/memory_write_dedup_service.py src/opencortex/services/memory_directory_record_service.py src/opencortex/services/memory_store_record_service.py tests/test_memory_store_record_service.py`

## Risks

- Some focused tests may intentionally construct helper services with
  `SimpleNamespace(_orch=...)`; update those tests only where the helper's
  boundary changes.
- Dedup and parent-directory writes share storage and embedding behavior with
  normal store. Preserve collection names, filter shapes, and best-effort
  filesystem behavior.
- This is a dependency-boundary cleanup, not a semantics refactor. Do not
  change scoring, derive output, TTL values, projection payloads, or signal
  payloads.
