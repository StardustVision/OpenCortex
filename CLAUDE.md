# OpenCortex — Developer Guide

## Overview

OpenCortex is a memory and recall runtime for AI agents. It writes memories,
resources, and conversation turns into a layered CFS file tree plus a Qdrant
vector index, and exposes the result through three transports:

- REST under `/api/v1/*` and `/admin/v1/*`
- Streamable HTTP MCP at `POST /mcp`
- Web console APIs under `/console/v1/*`

A single FastAPI app (`opencortex.app:create_app`) owns all three. The package
is intentionally narrow — historical subsystems (MemoryOrchestrator, Cortex
Alpha pipeline, IntentRouter, Insights, Autophagy, Skill Engine) have been
removed from `src/`; only design docs under `docs/design/` remain.

## Tech Stack

- Python 3.10+, async-first
- FastAPI + uvicorn + httpx
- Qdrant (embedded local mode by default; remote URL also supported)
- OpenAI-compatible embedding + LLM endpoints (`llm_api_style: openai|anthropic`)
- Optional rerank: `llm` / `litellm` / `hosted_vllm` / `openai`
- JWT (PyJWT), Pydantic v2, structlog, orjson
- Package management: `uv`

## Repository Layout

```
src/opencortex/
  __main__.py             # CLI entry: `opencortex-server`, uvicorn factory
  app.py                  # FastAPI factory, lifespan, router wiring, admin bootstrap
  runtime.py              # AppRuntime — Qdrant + embedder + LLM + CortexStorage + queue
  settings.py             # Settings (env prefix OPENCORTEX_APP_, .env file)
  logging.py              # structlog configuration

  auth/
    routes.py             # /api/v1/auth/me, /admin/v1/tokens (create/list/revoke)
    token.py              # JWT mint + verify, token record persistence
    __main__.py           # CLI: `opencortex-token`

  console/
    routes.py             # /console/v1/{memories, memories/search, memories/content,
                          #              memories (DELETE), stats}

  core/
    identity.py           # IdentityProfile + ContextVar (tenant_id/user_id/project_id/role/session_id/collection)
    middlewares.py        # WriteRequestContextMiddleware — Bearer JWT → IdentityProfile

  llm/
    client.py             # LLMCompletion (OpenAI/Anthropic-compatible)

  mcp/
    routes.py             # POST /mcp — JSON-RPC dispatcher (initialize/ping/tools.*/prompts.*)
    tools.py              # 6 MCP tools + McpToolbox
    schemas.py            # JSON-RPC + MCP protocol models, error codes
    instructions.py       # MCP_INSTRUCTIONS + MCP_CLIENT_RULES (served via prompts/get)

  parse/
    base.py               # ParsedChunk, ParserConfig
    registry.py           # ParserRegistry — extension dispatch
    parsers/              # markdown / text / word / excel / powerpoint / pdf / epub

  prompts/
    write.py              # write-path LLM prompts (derive, merge, etc.)
    retrieval.py          # retrieval-path LLM prompts (probe, rerank, reason_tree)
    schemas.py            # shared prompt response schemas

  storage/
    cfs.py                # CFS — local filesystem with safe path resolution
    cfs_queue.py          # CFSQueue — SQLite-backed persistent FIFO with retry/backoff
    cortex_storage.py     # CortexStorage — opencortex:// URI tree on top of CFS
                          # (.abstract.md / .overview.md / .abstract.json / content.md / .relations.json)
    namespace.py          # CortexNamespace — URI templating per tenant/user/project

  store/
    routes.py             # /api/v1/memory/{store,search,forget}, /api/v1/session/{message,end}
    dependencies.py       # FastAPI dependency providers for store flows
    store.py              # MemoryStore, ResourceStore
    forget.py             # MemoryForgetter
    derive.py             # Semantic derive helpers (LLM → abstract/overview/keywords/entities)
    document_tree.py      # DocumentParser + DocumentTreeWriter (resource chunking)
    common.py             # Cross-cutting helpers (abstract_json builder, slug, etc.)
    types.py              # ContextType, EventName, StoreRecordType, MemoryCategory,
                          # SessionRecordLayer, ...
    embedder.py           # Context-aware embedder adapter

    schemas/              # Pydantic models — store requests/inputs/records
      records.py
      raw_records.py
      store.py

    session/
      buffer.py           # SessionBuffer — in-memory accumulator keyed by IdentityProfile
      store.py            # SessionStore — per-message immediate write (sync embed + Qdrant + CFS)
      merger.py           # SessionMerger — LLM-derived merged chunks
      ender.py            # SessionEnder — final session record + cleanup

    event/
      events.py           # MemoryEventManager + event Pydantic models (MemoryStored,
                          # CheckUpdate, SessionTurnStored, SessionMerged, SessionEnded)
      actions.py          # 9 EventAction implementations (see "Event Worker" below)
      worker.py           # EventWorker — consumes CFSQueue with ordered locks + retry
      failure.py          # Failure classification (retry vs drop)

    writer/
      primary_record_writer.py    # The shared write step (Qdrant upsert + CFS write + event emit)
      semantic_derive_writer.py   # LLM-driven L0/L1/keywords/entities derivation
      search_index_writer.py      # Anchor + Fact secondary indexes
      entity_index_writer.py      # Entity index
      reason_tree_index_writer.py # Reason-tree projections
      reason_tree_build_writer.py # LLM-enhanced reason-tree nodes
      cortex_storage_writer.py    # CFS persistence side
      session_cleanup_writer.py   # Drop immediates after merge
      event_payload.py            # Event payload helpers

  utils/
    json_parse.py
    text.py
    uri.py                # opencortex:// helpers

  vector/
    qdrant_store.py       # QdrantVectorStore — async wrapper, embedded or remote
    embedder.py           # OpenAIEmbeddingClient
    payloads/             # Qdrant payload schemas (primary, search, reason_tree)
    retrieval/
      retriever.py        # MemoryRetriever — orchestrates the pipeline
      probe.py            # RetrievalProbe — LLM probe builds query plan inputs
      planner.py          # RetrievalPlanner — composes the QueryPlan
      executor.py         # RetrievalExecutor — fanned-out Qdrant queries
      ranker.py           # RetrievalRanker + payload merge
      reason_tree.py      # ReasonTreeRunner — LLM picks starting URIs
      cone.py             # ConeExpander — graph expansion via relation signals
      reranker.py         # RecallReranker + build_rerank_client
      filters.py          # Qdrant filter builders (tenant/uid/project scoping)
      records.py          # Hit/Plan record types
      schemas.py          # RetrievalRequest/Response, MatchedMemory, DetailLevel, surfaces

tests/opencortex/
  test_app.py              # FastAPI app + middleware
  test_cfs_queue.py        # SQLite-backed queue invariants
  test_cortex_storage.py   # CortexStorage URI ops
  test_events.py           # MemoryEventManager + EventWorker
  test_llm_client.py       # LLMCompletion adapter
  test_qdrant_vector_store.py
  test_session_message.py  # Session immediate write

docs/
  design/                  # forward-looking design notes (not all implemented)
  plans/                   # implementation plans
```

## Entry Points

```bash
# Server (uvicorn factory, defaults: 127.0.0.1:8921)
uv run opencortex-server --host 0.0.0.0 --port 8921

# Token CLI
uv run opencortex-token <subcommand>

# Docker (Qdrant + vLLM embedding + vLLM reranker)
docker compose up -d
```

`opencortex-server` runs `opencortex.app:create_app` via `uvicorn ... factory=True`.

## Configuration

All runtime settings come from `Settings` (`settings.py`), env prefix
**`OPENCORTEX_APP_`**, optional `.env`. Key fields:

| Setting | Default | Purpose |
|---------|---------|---------|
| `data_root` | `./data` | CFS root + token store + Qdrant embedded path |
| `vector_dimension` | 1024 | Dense vector size |
| `qdrant_url` / `qdrant_api_key` / `qdrant_timeout` | `""` / `""` / 30s | Empty `qdrant_url` → embedded local Qdrant at `{data_root}/qdrant` |
| `embedding_api_base` / `embedding_api_key` / `embedding_model` | OpenAI / `""` / `text-embedding-3-small` | Falls back to `llm_api_key` |
| `llm_api_base` / `llm_api_key` / `llm_model` / `llm_api_style` | OpenAI / `""` / `gpt-4o-mini` / `openai` | `openai` or `anthropic` |
| `conversation_merge_token_budget` | 6144 | Triggers `SessionMerger` |
| `session_idle_ttl` | 1800s | `SessionBuffer` prune horizon |
| `immediate_event_ttl_hours` / `merged_event_ttl_hours` | 24 / 168 | Session-record TTL (Qdrant payload) |
| `store_event_worker_concurrency` | 4 | `EventWorker` consumers |
| `retrieval_rerank_enabled` / `_provider` / `_model` / `_api_base` / `_api_key` | true / `llm` / `""` / `""` / `""` | `provider=llm` reuses `llm_completion` |
| `retrieval_rerank_seed_limit` / `_final_limit` | 30 / 30 | Rerank candidate caps |
| `identity_context_enabled` | true | Disable to bypass identity middleware |
| `admin_api_token` | `""` | Pre-issued admin JWT; empty → bootstrap one-time `_system/_admin` token |

## HTTP Surface

All `/api/`, `/admin/`, `/console/`, and `POST /mcp` require a Bearer JWT (the
middleware short-circuits with 401 otherwise). `/admin/v1/*` additionally
requires `role=admin`.

```
GET    /api/v1/auth/me
POST   /api/v1/memory/store          { type: memory|resource, content, category, meta, source }
POST   /api/v1/memory/search         RetrievalRequest
POST   /api/v1/memory/forget         { query | uri }
POST   /api/v1/session/message       { session_id, turn_id, messages[], tool_calls[] }
POST   /api/v1/session/end           { session_id }
GET    /admin/v1/tokens
POST   /admin/v1/tokens              { tenant_id, user_id, role }
DELETE /admin/v1/tokens              { token_prefix }
GET    /console/v1/memories
POST   /console/v1/memories/search
GET    /console/v1/memories/content
DELETE /console/v1/memories
GET    /console/v1/stats
POST   /mcp                          # JSON-RPC: initialize, ping, tools/list, tools/call,
                                     # prompts/list, prompts/get
```

MCP tools: `opencortex.search`, `opencortex.store_memory`,
`opencortex.store_resource`, `opencortex.forget`, `opencortex.session_message`,
`opencortex.session_end` (also accessible via underscore aliases).

## Identity

JWT claims `tid` / `uid` / `pid` / `role` → `IdentityProfile` set on a
`ContextVar` by `WriteRequestContextMiddleware`. Read via
`get_identity_profile()`. The optional `X-Collection` header overrides the
Qdrant collection name (default `context`).

## Write Pipeline

```
HTTP request
  → MemoryStore / ResourceStore / SessionStore
    → CortexNamespace.resolve()           # opencortex://{tenant}/{user}/{bucket}/{project}/{category}/{node}
    → PrimaryRecordWriter.write()         # Qdrant upsert + CFS write + emit event
      → MemoryEventManager.publish()
        → EventWorker.enqueue() → CFSQueue (SQLite, persistent, retryable)
```

`PrimaryRecordWriter` is the only path that mutates the primary record. It
returns synchronously after the Qdrant upsert and the initial CFS write. All
LLM-driven enrichment happens asynchronously in the worker.

**Memory** (short text): `RawPrimaryRecord` created without abstract/overview;
`derive_status="pending"`. The semantic derive action fills L0/L1/keywords/entities later.

**Resource** (document): When the content parses into a tree (Markdown
headings, etc.), the root record is stored as `chunk_role="document_root"` and
`DocumentTreeWriter` writes child sections with `parent_uri` linking.

**Session message**: Each message is written **immediately retrieval-ready** —
`SessionStore.prepare_immediate_ready_payload()` embeds synchronously, sets
`retrieval_ready=true`, `retrieval_surface="l0_object"`, and uses the raw
message text as the abstract. Immediate records carry
`immediate_event_ttl_hours` TTL as a safety net.

## Session Three-Layer Flow

```
session/message  →  SessionStore                  → immediate records (synchronous, retrieval-ready)
                    SessionBuffer.append()        → in-memory accumulator
                    SessionBuffer.freeze_ready_chunks(): when token budget reached
                       → emit SessionTurnStoredEvent (merge_requested=true)

EventWorker      →  SessionMergeAction
                    SessionMerger.merge_unmerged() → LLM-derived merged record
                       → emit SessionMergedEvent
                    SessionCleanupAction          → delete immediates for that range

session/end      →  SessionEnder.end()
                    load merged records           → final session record
                    cleanup any remaining immediates by session_id
                       → SessionEndedEvent
```

Buffer state is keyed by `(tenant_id, user_id, project_id, session_id)` and
serialized via per-key `asyncio.Lock`s in the worker. `session_idle_ttl` prunes
stale buffers.

URIs:
- immediate: `…/events/{session}/immediate/{uuid}`
- merged: `…/events/{session}/merged/{start}-{end}`
- final: `…/events/{session}/final`

## Event Worker

`EventWorker` consumes `CFSQueue` (SQLite-backed, persisted under
`{data_root}/queues/`) with `store_event_worker_concurrency` consumers.
Per-key locks (`ordering_key()`) serialize work on the same record or session.
Failures classified by `classify_event_failure()` → retry with backoff or drop.

Registered actions (`build_event_worker` in `app.py`):

| Action | Triggered by | Effect |
|--------|--------------|--------|
| `SemanticDeriveAction` | `MemoryStoredEvent` (non-ready) | LLM derives abstract/overview/keywords/entities; updates primary record |
| `SearchIndexAction` | `MemoryStoredEvent` | Writes anchor + fact secondary indexes |
| `EntityIndexAction` | `MemoryStoredEvent` | Writes entity index |
| `CortexStorageAction` | `MemoryStoredEvent` | Writes/refreshes `.abstract.md`, `.overview.md`, `.abstract.json`, `content.md` |
| `SessionMergeAction` | `SessionTurnStoredEvent` | Drains buffer into merged records |
| `SessionCleanupAction` | `SessionMergedEvent` | Removes immediates included in the merge |
| `ReasonTreeIndexAction` | `MemoryStoredEvent` | Writes reason-tree projections |
| `ReasonTreeBuildAction` | `MemoryStoredEvent` | LLM-enhanced reason-tree nodes |
| `CheckUpdateAction` | `CheckUpdateEvent` | Placeholder for future mutation detection |

## Retrieval Pipeline

`MemoryRetriever.search()`:

```
RetrievalProbe.probe()            # optional LLM probe to seed query plan
RetrievalPlanner.plan()           # build QueryPlan from request + probe
ReasonTreeRunner.select()         # LLM picks starting URIs (if reason-tree available)
RetrievalExecutor.execute()       # parallel Qdrant searches across surfaces
                                  #   (l0_object, anchor_index, fact_index,
                                  #    entity_index, reason_tree_index)
RetrievalRanker.rank()            # fuse scores, dedup by source_uri
RecallReranker.rerank()           # optional seed rerank (if enabled)
ConeExpander.expand()             # graph expansion via .relations.json
RetrievalRanker.rank()            # re-rank fused (raw + cone)
RecallReranker.rerank()           # optional final rerank
load_primary_records()            # hydrate full payloads by uri filter
to_matched_memory()               # project to API response, with optional
                                  # CFS read for overview/content if not in payload
```

`DetailLevel` (`L0` / `L1` / `L2`) controls how much is returned. `L0` returns
abstract only; `L2` fetches `.overview.md` and `content.md` from CFS if not
already on the Qdrant payload.

## Storage Layout

CFS root: `{data_root}/` mirrors the URI tree.

```
opencortex://{tenant}/{user}/{bucket}/{project}/{category}/{node}
                                       bucket ∈ {memories, resources}

Per node directory:
  content.md          # L2 raw content
  .abstract.md        # L0 abstract
  .overview.md        # L1 overview
  .abstract.json      # machine-readable L0 payload (anchor/fact/entities/keywords)
  .relations.json     # link table → other URIs (used by ConeExpander)
```

Qdrant collection: `context` (default; overridable per request via
`X-Collection`). Primary records and all secondary projections live in the same
collection, distinguished by payload `retrieval_surface`.

## Conventions

- All storage and vector operations are `async`.
- Identity is **never** in `Settings` — always derived from the Bearer JWT.
- `PrimaryRecordWriter` is the single chokepoint for primary mutations. Don't
  bypass it from outside `store/`.
- Heavy work (LLM derive, secondary indexes, session merge) belongs in
  `EventAction`s, not in request handlers.
- Tests live under `tests/opencortex/`. Run with
  `uv run python -m pytest tests/opencortex -v`.
- When adding a new event-driven side effect: define an event type in
  `store/event/events.py`, register the type in
  `store/event/worker.py:EVENT_TYPES`, add an action in
  `store/event/actions.py`, wire it into `build_event_worker` in `app.py`.

## Running Tests

```bash
uv run python -m pytest tests/opencortex -v
```

There are seven test modules covering the FastAPI app, CFS queue, CortexStorage,
events, LLM client, Qdrant vector store, and session message flow.
