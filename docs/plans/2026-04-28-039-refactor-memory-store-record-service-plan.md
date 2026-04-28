---
title: "refactor: Extract memory store record service"
type: refactor
status: completed
date: 2026-04-28
origin: user request
---

# refactor: Extract memory store record service

## Overview

Extract the record assembly and persistence portion of `MemoryWriteService.add`
into a focused service. `MemoryWriteService.add` should continue to orchestrate
ingest mode selection, derivation, embedding, and dedup decisions. The new
service should own the work that happens after a `Context` and its
`abstract_json` payload are ready: record payload construction, scope/source
fields, TTL, Qdrant upsert, anchor projection, `memory_stored` signal publish,
entity index sync, and non-blocking CortexFS write.

## Problem Frame

`src/opencortex/services/memory_write_service.py` is still large, and its
`add()` method mixes two different responsibilities:

- write-flow orchestration: ingest mode, URI/parent resolution, derive, embed,
  semantic dedup, and timing/logging;
- persistence mechanics: build a storage record, enrich top-level metadata,
  compute TTL, upsert into Qdrant, sync anchor projections, publish lifecycle
  signals, sync entity index, and schedule CortexFS writes.

Those persistence mechanics are stable domain operations that can be composed
behind the write service. Moving them out makes the `store` chain easier to
read without changing `/api/v1/memory/store` behavior.

## Requirements Trace

- R1. Add a focused store record service for post-`Context` record assembly and
  persistence.
- R2. Move record payload assembly after `Context` construction out of
  `MemoryWriteService.add`.
- R3. Move scope/source/session/project fields, document/conversation flattened
  fields, and TTL calculation into the new service.
- R4. Move Qdrant upsert, anchor projection sync, `memory_stored` signal
  publication, entity index sync, and CortexFS fire-and-forget write into the
  new service.
- R5. Keep `MemoryWriteService.add` responsible for ingest mode, derive/embed,
  dedup decision, and add timing/logging.
- R6. Keep dedup merge behavior and `memory_stored` merge signal behavior
  unchanged.
- R7. Preserve `/api/v1/memory/store` response shape and existing tests.

## Scope Boundaries

- Do not change ingest mode resolver behavior.
- Do not change derivation, embedding, semantic dedup, or merge scoring.
- Do not alter record field names, URI generation, TTL values, anchor
  projection payloads, signal payload fields, or CortexFS write semantics.
- Do not touch recall/search behavior.
- Do not merge this with document ingest extraction; document write stays with
  `MemoryDocumentWriteService`.

## Current Code References

- `src/opencortex/services/memory_write_service.py`
  - `add()` currently builds and persists records inline.
  - `_ensure_parent_records()` already owns parent directory creation.
  - `_merge_into()` remains dedup merge behavior.
- `src/opencortex/services/memory_signals.py`
  - `MemoryStoredSignal` is already the plugin boundary for store side effects.
- `tests/test_memory_signal_integration.py`
  - Verifies store emits `memory_stored`.
- `tests/test_http_server.py`, `tests/test_eval_contract.py`, and
  `tests/test_document_async_derive.py`
  - Exercise public store behavior.
- `tests/test_e2e_phase1.py`
  - Covers TTL and broader memory write contracts.

## Key Technical Decisions

- Name the new service `MemoryStoreRecordService`. It is narrower than a full
  store pipeline because the write pipeline orchestration remains in
  `MemoryWriteService.add`.
- Bind the service to `MemoryWriteService`, matching existing composition style
  (`MemoryDocumentWriteService`, query/scoring services).
- Return a small dataclass result from persistence containing the stored record
  and `upsert_ms`, so `MemoryWriteService.add` can keep existing timing logs.
- Keep parent directory creation outside the new service for this PR. It is
  still pre-persistence flow control and already has a dedicated helper.
- Keep dedup merge signal handling in `MemoryWriteService.add`; the request
  explicitly targets the Context-built normal persistence block.

## Implementation Units

- U1. **Create MemoryStoreRecordService**

Goal: Move normal store record assembly and persistence into a focused service.

Requirements: R1, R2, R3, R4

Files:
- Create: `src/opencortex/services/memory_store_record_service.py`
- Modify: `src/opencortex/services/memory_write_service.py`
- Test: `tests/test_memory_store_record_service.py`

Approach:
- Add `StoredRecordResult(record: Dict[str, Any], upsert_ms: int)`.
- Add `persist_context_record(...)` with explicit parameters for `ctx`,
  `content`, `abstract_json`, `object_payload`, `effective_category`,
  `keywords`, `entities`, `meta`, `context_type`, `session_id`, `tenant_id`,
  `user_id`, `sparse_vector`, and `is_leaf`.
- Build the record from `ctx.to_dict()`.
- Preserve vector/sparse vector handling.
- Populate the same scope/source/session/project/category/keywords/entities
  fields as today.
- Preserve flattened fields: `source_doc_id`, `source_doc_title`,
  `source_section_path`, `chunk_role`, `speaker`, and `event_date`.
- Preserve TTL rules for staging and merged event records.
- Upsert, sync anchor projection, publish `MemoryStoredSignal`, sync entity
  index when available, and schedule the same fire-and-forget CortexFS write.

Test scenarios:
- Persisted record contains scope, source tenant/user, project, category,
  session, keywords, entities, abstract_json, object payload, and flattened
  fields.
- Staging records get immediate TTL.
- Merged event memory records get merged-event TTL.
- Signal payload matches the stored record.
- CortexFS write is scheduled without blocking.

Verification:
- `uv run --group dev pytest tests/test_memory_store_record_service.py -q`

- U2. **Wire MemoryWriteService.add to the new service**

Goal: Keep `add()` as orchestration and delegate normal persistence.

Requirements: R5, R6, R7

Files:
- Modify: `src/opencortex/services/memory_write_service.py`
- Test: `tests/test_memory_signal_integration.py`
- Test: `tests/test_http_server.py`
- Test: `tests/test_eval_contract.py`

Approach:
- Add a lazy `_store_record_service` property on `MemoryWriteService`.
- Replace the normal record assembly/upsert/signal/fs block with
  `persist_context_record(...)`.
- Keep `_ensure_parent_records()` before persistence.
- Keep dedup merge and its signal/logging path unchanged.
- Preserve `ctx.meta["dedup_action"] = "created"` and timing log shape.

Test scenarios:
- Store still returns `dedup_action=created`.
- Signal integration test still receives one `memory_stored` signal.
- HTTP store/search contract tests remain unchanged.

Verification:
- `uv run --group dev pytest tests/test_memory_signal_integration.py tests/test_http_server.py tests/test_eval_contract.py -q`

## Verification Plan

- `uv run --group dev pytest tests/test_memory_store_record_service.py tests/test_memory_signal_integration.py -q`
- `uv run --group dev pytest tests/test_http_server.py tests/test_eval_contract.py tests/test_document_async_derive.py -q`
- `uv run --group dev pytest tests/test_context_manager.py tests/test_e2e_phase1.py -q`
- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`
