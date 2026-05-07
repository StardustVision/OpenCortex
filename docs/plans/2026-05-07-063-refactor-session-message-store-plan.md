# Refactor Session Message Store Plan

## Goal

Route `POST /api/v1/session/message` through a direct session message store
chain instead of the legacy `ContextManager.handle(phase="commit")` path.

The target synchronous chain is:

```text
SessionStore.message()
  -> build_immediate_record()
  -> embed()
  -> writer.write()
  -> buffer.append()
  -> signals.publish(session_turn_stored)
  -> if buffer.should_merge(): signals.publish(session_merge_requested)
```

## Scope

- Add `SessionBuffer` for per-session locks and immediate-message buffer state.
- Implement `SessionStore.message()` for immediate RAG primary-record writes.
- Keep `/api/v1/session/end` on the existing end/recomposition path.
- Keep merge/recomposition algorithms out of the message write chain.
- Keep duplicate turn id handling out of the first version; repeated calls may
  write repeated immediate records and later merge can consolidate.

## Non-Goals

- No `ContextManager` end/recomposition rewrite.
- No new anchor/fact/dedup logic in `/session/message`.
- No compatibility wrapper for the new message path.
- No migration of existing tests that target private legacy `_write_immediate`.

## Implementation Steps

1. Add session schemas for validated message input and result.
2. Add `SessionBuffer` backed by the existing `ContextManager` buffer state so
   `/session/end` can continue to flush/merge records written by the new path.
3. Add `SessionStore.message()` with the fixed chain above.
4. Extend `StoreSignals` with `session_turn_stored` and `session_merge_requested`.
5. Wire FastAPI dependency `get_session_store` and switch only
   `/api/v1/session/message`.
6. Register lightweight signal handlers for observer transcript and merge
   request side effects.
7. Add focused tests for the new session message chain and route behavior.

## Validation

- `uv run --group dev ruff check ...`
- `uv run --group dev ruff format --check ...`
- Targeted pytest for HTTP session message, session store, and existing end
  traceability smoke.
