# OpenCortex

OpenCortex is a memory storage and recall runtime for AI agents. It stores
memories, resources, and conversation sessions into a layered file tree plus a
Qdrant vector index, then exposes the result through HTTP APIs, Streamable HTTP
MCP, and a small web console.

The current codebase is the new `opencortex` runtime. Historical memory-chain
code has been removed from the active package; future features such as
insights, autophagy, skill engine, and self-upgrade are tracked as design
documents under `docs/design/`.

## What It Does

OpenCortex provides:

- Durable memory writes for user facts, preferences, events, and workflows.
- Resource writes for documents, parsed sections, and shared knowledge material.
- Session writes for conversation turns and session-end summaries.
- L0/L1/L2 storage layers:
  - `L0`: compact abstract
  - `L1`: overview
  - `L2`: full content
- Background side indexes for recall:
  - primary object records
  - anchor and fact indexes
  - entity index
  - reason-tree index
  - cone-expansion-ready relation signals
- Retrieval through probe, planner, executor, ranker, reason-tree selection, and
  optional cone expansion.
- Semantic forget by query or URI.
- JWT-protected API, admin token management, and MCP access.
- A React web console for token management and memory inspection.

## Current Architecture

```text
Client / Agent / MCP client
  -> Bearer JWT middleware
  -> FastAPI routes
     -> Store flows
        -> PrimaryRecordWriter
        -> CFS-backed CortexStorage
        -> QdrantVectorStore
        -> persistent SQLite-backed event queue
        -> background writers for semantic layers and side indexes
     -> Retrieval flow
        -> probe
        -> planner
        -> executor
        -> ranker
        -> CFS hydration
  -> Optional React console
```

The core split is intentional:

- `storage/` owns CFS and URI-tree file operations.
- `vector/` owns Qdrant payloads, vector storage, and retrieval.
- `store/` owns write flows, session handling, events, and writers.
- `console/` owns web-console management APIs and does not change MCP or the
  public memory API contract.
- `mcp/` exposes the same memory capabilities through Streamable HTTP MCP.

## Repository Layout

```text
src/opencortex/
  app.py                  FastAPI application factory and runtime wiring
  settings.py             OPENCORTEX_APP_* configuration
  auth/                   JWT generation, verification, and admin token APIs
  console/                Web-console-only management APIs
  core/                   request identity context and middleware
  llm/                    OpenAI-compatible LLM client
  mcp/                    Streamable HTTP MCP transport and tools
  parse/                  document parser adapters
  prompts/                write and retrieval prompts
  storage/                CFS, CortexStorage, persistent queue, URI namespace
  store/                  write/session/event flows and writers
  vector/                 Qdrant store, payload schemas, retrieval pipeline

web/                      React/Vite console
tests/opencortex/         Current runtime tests
docs/design/              Detailed feature and roadmap documents
```

## Requirements

- Python `>=3.10`
- `uv`
- Node.js `>=18` for the web console
- Qdrant can run embedded through `qdrant-client` local storage or as a server
  through `OPENCORTEX_APP_QDRANT_URL`.

## Install

```bash
uv sync
```

For parser extras:

```bash
uv sync --extra parsers
```

For web console dependencies:

```bash
cd web
npm install
```

## Configuration

Settings use the `OPENCORTEX_APP_` environment prefix and may also be loaded
from `.env`.

Common settings:

```bash
export OPENCORTEX_APP_DATA_ROOT=./data
export OPENCORTEX_APP_VECTOR_DIMENSION=1024

# Embedded local Qdrant if empty. Use server Qdrant for production-like runs.
export OPENCORTEX_APP_QDRANT_URL=
export OPENCORTEX_APP_QDRANT_API_KEY=

# OpenAI-compatible embedding endpoint.
export OPENCORTEX_APP_EMBEDDING_API_BASE=https://api.openai.com/v1
export OPENCORTEX_APP_EMBEDDING_API_KEY=<embedding-key>
export OPENCORTEX_APP_EMBEDDING_MODEL=text-embedding-3-small

# OpenAI-compatible or supported LLM endpoint.
export OPENCORTEX_APP_LLM_API_BASE=https://api.openai.com/v1
export OPENCORTEX_APP_LLM_API_KEY=<llm-key>
export OPENCORTEX_APP_LLM_MODEL=gpt-4o-mini
export OPENCORTEX_APP_LLM_API_STYLE=openai

# Background event worker concurrency.
export OPENCORTEX_APP_STORE_EVENT_WORKER_CONCURRENCY=4
```

The app creates:

- `data/auth_secret.key` for JWT signing
- `data/tokens.json` for issued token records
- `data/qdrant/` when using embedded Qdrant
- CFS content under the configured data root
- persistent event queue files under CFS-managed storage

Do not commit runtime `data*/` or secrets.

## Start The Backend

```bash
uv run opencortex-server --host 127.0.0.1 --port 8921
```

Equivalent entry point:

```bash
uv run opencortex --host 127.0.0.1 --port 8921
```

Development reload:

```bash
uv run opencortex-server --host 127.0.0.1 --port 8921 --reload
```

## Authentication

All `/api/*`, `/admin/*`, `/console/*`, and `/mcp` endpoints require:

```http
Authorization: Bearer <jwt>
```

JWT tokens identify a tenant and user. Project is treated as business metadata,
not as part of API-key creation.

Create a user token interactively:

```bash
uv run opencortex-token generate
```

List issued tokens:

```bash
uv run opencortex-token list
```

Revoke by prefix:

```bash
uv run opencortex-token revoke <token-prefix>
```

Admin token management is available through `/admin/v1/tokens`. In normal
deployments you can leave `OPENCORTEX_APP_ADMIN_API_TOKEN` unset; if no admin
record exists, OpenCortex creates a one-time `_system/_admin` token at startup
and prints it in the server logs. A pre-generated admin token can also be
supplied with:

```bash
export OPENCORTEX_APP_ADMIN_API_TOKEN=<signed-admin-jwt>
```

The configured token must be signed by the current `data/auth_secret.key`.

## HTTP API

### Store Memory Or Resource

`POST /api/v1/memory/store`

Memory example:

```bash
curl -sS http://127.0.0.1:8921/api/v1/memory/store \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "memory",
    "content": "Alice prefers concise technical summaries.",
    "category": "semantic",
    "metadata": {"entities": ["Alice"]},
    "source": {"kind": "manual"}
  }'
```

Resource example:

```bash
curl -sS http://127.0.0.1:8921/api/v1/memory/store \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "resource",
    "content": "# Qdrant Notes\n\nUse server Qdrant for production-like runs.",
    "category": "semantic",
    "metadata": {"title": "Qdrant Notes", "source_path": "/docs/qdrant.md"},
    "source": {"kind": "document", "path": "/docs/qdrant.md", "title": "Qdrant Notes"}
  }'
```

The synchronous request writes the primary record. Semantic derivation, L0/L1/L2
CFS materialization, and side indexes are handled by the background event
worker.

### Search

`POST /api/v1/memory/search`

```bash
curl -sS http://127.0.0.1:8921/api/v1/memory/search \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "What does Alice prefer?", "limit": 5}'
```

The public search request intentionally accepts only:

- `query`
- `limit`

Filtering and management controls belong to the console API, not to the public
memory recall contract.

### Forget

`POST /api/v1/memory/forget`

Semantic forget:

```bash
curl -sS http://127.0.0.1:8921/api/v1/memory/forget \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "Alice concise summaries"}'
```

Explicit URI forget:

```bash
curl -sS http://127.0.0.1:8921/api/v1/memory/forget \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"uri": "opencortex://tenant/user/memories/public/semantic/example"}'
```

### Session Message

`POST /api/v1/session/message`

```bash
curl -sS http://127.0.0.1:8921/api/v1/session/message \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "session-001",
    "turn_id": "turn-001",
    "messages": [
      {"role": "user", "content": "Remember that Alice prefers concise summaries."}
    ],
    "tool_calls": [],
    "cited_uris": []
  }'
```

Session writes can create immediate records and enqueue merge work. Merge
cleanup removes stale immediate records after merged records are written.

### Session End

`POST /api/v1/session/end`

```bash
curl -sS http://127.0.0.1:8921/api/v1/session/end \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "session-001"}'
```

Session end writes final session memory and, when content is structured, a
parsed final tree plus reason-tree side indexes.

### Auth

`GET /api/v1/auth/me`

```bash
curl -sS http://127.0.0.1:8921/api/v1/auth/me \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN"
```

## Admin API

Admin APIs are separate from user memory APIs and MCP.

### List Tokens

`GET /admin/v1/tokens`

Returns public token records only. Full token values are never returned from
list.

### Create Token

`POST /admin/v1/tokens`

```bash
curl -sS http://127.0.0.1:8921/admin/v1/tokens \
  -H "Authorization: Bearer $OPENCORTEX_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "tenant-a", "user_id": "alice"}'
```

The full token is returned once on create.

### Revoke Token

`DELETE /admin/v1/tokens`

```bash
curl -sS -X DELETE http://127.0.0.1:8921/admin/v1/tokens \
  -H "Authorization: Bearer $OPENCORTEX_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"token_prefix": "abcd1234"}'
```

## Console API

The web console uses `/console/v1/*`. These routes are management-facing and do
not change the public memory API or MCP contracts.

Current console routes:

- `GET /console/v1/stats`
- `GET /console/v1/memories`
- `POST /console/v1/memories/search`
- `GET /console/v1/memories/content?uri=...`
- `DELETE /console/v1/memories`

Admin users can provide tenant/user filters. Regular users are limited to their
own identity scope.

## Web Console

Start the backend first, then:

```bash
cd web
npm install
npm run dev
```

Open:

```text
http://127.0.0.1:5173
```

The Vite dev server proxies:

- `/api` to the backend
- `/admin` to the backend
- `/console` to the backend

The console currently includes:

- token-verified login
- dashboard stats
- memory search/list/detail/delete
- admin token management

## MCP

OpenCortex exposes a remote MCP server over Streamable HTTP. Configure your MCP client with:

```json
{
  "mcpServers": {
    "opencortex": {
      "type": "streamable-http",
      "url": "http://<host>:8921/mcp",
      "headers": {
        "Authorization": "Bearer <jwt>"
      }
    }
  }
}
```

Use the same Bearer token used by the HTTP API and Web console. Do not configure
OpenCortex as an SSE server; the endpoint implements the 2025-06-18 Streamable
HTTP transport.

Current MCP tools:

- `opencortex.search`
- `opencortex.store_memory`
- `opencortex.store_resource`
- `opencortex.forget`
- `opencortex.session_message`
- `opencortex.session_end`

For a quick transport check without an MCP client:

```bash
curl -sS http://127.0.0.1:8921/mcp \
  -H "Authorization: Bearer $OPENCORTEX_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

`GET /mcp` and `DELETE /mcp` currently return `405`; this runtime is stateless
Streamable HTTP JSON-RPC, not the older HTTP+SSE transport.

## Storage And Index Design

OpenCortex writes to two durable surfaces:

### CFS

CFS stores the URI tree and file layers:

```text
opencortex://<tenant>/<user>/<bucket>/<project>/<category>/<node>/
  content.md
  .abstract.md
  .overview.md
  .abstract.json
```

`CortexStorage` is the higher-level URI storage facade over CFS.

### Qdrant

Qdrant stores vector-searchable payloads. Main payload surfaces include:

- `l0_object`: primary memory/resource/session object
- `directory`: payload-only URI ancestor records
- `anchor_index`: anchor handles for locating related memories
- `fact_index`: fact points extracted from content
- `entity_index`: entity projections
- `reason_tree_index`: reason-tree nodes and summaries

The vector module owns payload schemas and retrieval logic. Storage does not own
Qdrant writes directly.

## Retrieval Pipeline

The recall path is:

```text
RetrievalRequest
  -> Probe
  -> Planner
  -> Executor
  -> Ranker
  -> Reason-tree selection
  -> Cone expansion
  -> CFS hydration
  -> RetrievalResponse
```

The public API keeps the input small (`query`, `limit`) while the internal plan
chooses surfaces, budgets, weights, depth, reason-tree usage, and cone expansion.

## Background Events

Primary writes emit events into a persistent queue. Worker actions then perform
side effects:

- semantic derivation through LLM
- CFS layer writes
- search index writes
- entity index writes
- reason-tree build and index writes
- session merge and cleanup
- check-update events for future mutation logic

The queue is persisted under the configured data root, so interrupted workers can
resume.

## Development Checks

Python:

```bash
uv run --group dev ruff format --check src/opencortex tests/opencortex
uv run --group dev ruff check src/opencortex tests/opencortex
uv run --group dev pytest tests/opencortex -q
```

Web:

```bash
cd web
npm run build
```

## Design Documents

Detailed current and planned behavior is documented in Chinese under:

- `docs/design/opencortex-functional-parity.md`
- `docs/design/opencortex-recall-design.md`
- `docs/design/insights-functional-detail.md`
- `docs/design/autophagy-functional-detail.md`
- `docs/design/skill-engine-functional-detail.md`
- `docs/design/self-upgrade-functional-detail.md`

## License

Apache-2.0
