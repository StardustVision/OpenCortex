# Autophagy

## Why This Exists

Autophagy is the recall-and-record lifecycle that turns memory from a stateless
search API into a session-aware protocol. The `memory_context` flow coordinates
planning, retrieval, and session bookkeeping so the system can (1) decide when
to recall, (2) return context plus guidance, and (3) persist the turn with
buffered conversation writes and downstream trace handling.

## Core Components

- `ContextManager`: owns the `memory_context` prepare / commit / end lifecycle,
  caches prepare results, tracks per-session state, and manages conversation
  buffering.
- `MemoryOrchestrator.plan_recall()`: resolves recall intent and emits a
  `RecallPlan` using `IntentRouter` and `RecallPlanner`.
- `RecallPlanner`: converts `SearchIntent` into an explicit `RecallPlan`
  (surfaces, limits, detail level, cone flag).
- `IntentRouter`: three-layer intent analysis (keywords, optional LLM, memory
  triggers) producing `SearchIntent` and typed queries.
- `RecallPlan` / `RecallSurface`: explicit plan structure used to gate memory
  and knowledge retrieval.
- Observer + Alpha pipeline: `ContextManager._commit()` records the transcript,
  and `ContextManager._end()` delegates to `orchestrator.session_end()` for
  trace splitting, storage, and optional knowledge candidate generation.

## Prepare Flow

1. `POST /api/v1/context` with `phase="prepare"` dispatches to
   `ContextManager._prepare()`.
2. The call is idempotent per `(tenant, user, session, turn)` via the prepare
   cache; the first call creates the observer session.
3. The latest user message becomes the query. If there is no user query,
   prepare returns an empty result with `should_recall=false`.
4. `MemoryOrchestrator.plan_recall()` is called (unless `recall_mode="never"`):
   `IntentRouter` produces `SearchIntent`, then `RecallPlanner` emits a
   `RecallPlan`. This step has a timeout and falls back to a local plan on
   failure.
5. Retrieval runs in parallel based on the plan:
   - Memory search uses `orchestrator.search()` with the plan’s detail level,
     limits, and optional `context_type` / `category` filters.
   - Knowledge search uses `orchestrator.knowledge_search()` when
     `include_knowledge` is enabled and the plan allows knowledge.
6. The response bundles `intent` (including `intent.recall_plan`), memory items,
   knowledge items, and instructions (confidence-guided citation hints). The
   empty-prepare fallback returns a reduced shape (no `recall_plan`).

## Commit Flow

1. `phase="commit"` validates that the turn includes at least two messages and
   is idempotent by `turn_id`.
2. The observer records the full turn (including `tool_calls`). If it fails, a
   fallback JSONL entry is written.
3. Cited URIs receive async reward updates. Skill citation validation only
   occurs when the skill event store is enabled and the server has tracked
   selected skill URIs during prepare.
4. Conversation buffering:
   - Each message is immediately written via `_write_immediate()` for fast
     recall (`meta.layer="immediate"`).
   - Messages and tool calls are appended to the per-session buffer.
   - Once the buffer exceeds the token threshold, it is merged into a higher
     quality chunk via `orchestrator.add()` and the immediate records are
     deleted.

## End Flow

1. `phase="end"` flushes any remaining buffered conversation into a merged
   record and removes leftover immediate records.
2. `orchestrator.session_end()` runs the Alpha pipeline (observer flush, trace
   splitting and storage). Archivist work may be triggered asynchronously
   depending on configuration and path. The result payload is centered on trace
   counts; `knowledge_candidates` is effectively reported as `0` on this path
   today.
3. Session state, caches, and turn tracking are cleaned up. Idle sessions are
   auto-closed by a background sweeper using the same end flow.

## Relationship to Search and Knowledge Recall

Autophagy is not a thin search wrapper. Plain `search()` is a stateless call
that still uses `plan_recall()`, but it does not handle lifecycle state,
observer recording, or conversation buffering. `memory_context` prepares a
recall plan with session context, runs memory and optional knowledge recall in
parallel, and returns instructions for the agent; commit/end then record and
stabilize the session.

Knowledge recall is a separate surface that can be enabled by server-side
configuration and the recall plan. The `ContextManager` can also apply internal
filters (for example `context_type` or `category`) when invoking memory search,
but these are not part of a guaranteed public `/api/v1/context` request surface.
Knowledge recall is bounded (`knowledge_limit` is capped) and uses the Alpha
knowledge store, so it is optional and can be disabled without affecting memory
recall. Session buffering ensures newly produced conversation turns are
searchable immediately, then consolidated later for higher-quality recall.

## Constraints and Tradeoffs

- Adds statefulness (session caches, idempotency tracking, buffering) compared
  to a stateless search API.
- Recall planning is best-effort: `IntentRouter` skips the LLM path when session
  context or LLMs are unavailable, and prepare falls back to a local plan on
  timeouts or failures.
- Immediate writes make new turns searchable fast but require merge/cleanup
  work later and introduce eventual consistency between immediate and merged
  layers.
- The protocol centralizes lifecycle orchestration, but it also introduces more
  moving parts (observer availability, async reward tasks, background cleanup).

## Current State

Autophagy-level behavior is implemented today through `ContextManager` and the
`/api/v1/context` endpoint. Recall planning is explicit (`RecallPlan` from
`RecallPlanner`), and `IntentRouter` drives intent classification. The lifecycle
already covers memory recall, optional knowledge recall, and session buffering,
but there is no separate `Autophagy` module; it is a coordination layer across
`ContextManager`, `MemoryOrchestrator`, and retrieval components. `RecallSurface`
defines `TRACE`, but the current prepare path does not execute trace recall.
