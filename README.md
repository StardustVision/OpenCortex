<h1 align="center">OpenCortex</h1>
<p align="center"><strong>Persistent memory and context management for AI agents</strong></p>
<p align="center">
  <a href="#what-is-opencortex">What is it</a> &middot;
  <a href="#key-concepts">Key Concepts</a> &middot;
  <a href="#architecture">Architecture</a> &middot;
  <a href="#getting-started">Getting Started</a> &middot;
  <a href="#core-features">Features</a> &middot;
  <a href="#api-reference">API</a> &middot;
  <a href="README_CN.md">中文文档</a>
</p>

---

## What is OpenCortex

LLM-based agents operate within finite context windows. When a session ends, everything the agent learned &mdash; user preferences, debugging solutions, architectural decisions &mdash; is lost. The next session starts from zero.

OpenCortex solves this by giving agents a **persistent, searchable, self-improving memory**. Think of it as long-term memory for AI: the agent stores what it learns, recalls relevant context when needed, and surfaces the most useful memories first through reinforcement learning.

It is not a key-value store. It is a complete memory engine with layered summaries, semantic retrieval, intent-aware routing, reinforcement-driven ranking, and automatic knowledge extraction from conversations.

### What it does, concretely

- **Remembers** user preferences, coding conventions, and past decisions across sessions
- **Recalls** relevant context automatically when the agent processes a new prompt
- **Learns** from feedback &mdash; memories that prove useful rank higher; stale ones decay
- **Extracts** reusable knowledge from conversation traces via the Cortex Alpha pipeline
- **Isolates** data per tenant and user through URI-based namespaces
- **Portable** &mdash; a single `memory_context` MCP tool replaces platform-specific hooks

---

## Key Concepts

### Memory Layers: L0 / L1 / L2

OpenCortex stores each memory at three levels of detail to minimize token usage:

| Layer | What it contains | Token cost | When it is used |
|-------|-----------------|------------|-----------------|
| **L0** (Abstract) | A single sentence summary | ~20-50 | Vector search indexing, quick confirmations |
| **L1** (Overview) | A paragraph with reasoning and context | ~100-200 | Most retrieval scenarios (default) |
| **L2** (Content) | The complete original content | Unlimited | Deep analysis, auditing |

When you store a memory, L1 is generated automatically. When you search, the system returns only the layer you need &mdash; 90% of queries are served by L0 or L1.

### SONA (Self-Organizing Neural Attention)

The reinforcement learning system that ranks memories. When an agent gives positive feedback to a memory, its reward score increases and it surfaces higher in future searches. Unused memories decay over time. The formula:

```
final_score = beta * rerank_score + (1 - beta) * retrieval_score + rl_weight * reward_score
```

### Cortex Alpha

The knowledge extraction pipeline. When a session ends, Cortex Alpha automatically:

1. **Observer** records the conversation transcript in real time
2. **TraceSplitter** decomposes the transcript into discrete task traces via LLM
3. **TraceStore** persists traces to Qdrant for future retrieval
4. **Archivist** extracts reusable knowledge candidates from traces
5. **Sandbox** quality-gates each candidate (statistical + LLM verification)
6. **KnowledgeStore** persists approved knowledge for search

No manual curation needed. The agent's knowledge base grows automatically.

### Memory Context Protocol

A platform-agnostic three-phase lifecycle that replaces Claude Code hooks:

| Phase | When | What it does |
|-------|------|-------------|
| **prepare** | Before generating a response | Recalls relevant memories and knowledge, returns context to the agent |
| **commit** | After generating a response | Records the conversation turn, applies RL reward to cited memories |
| **end** | Session complete | Flushes transcripts, triggers trace splitting and knowledge extraction |

Any MCP-compatible client can use the single `memory_context` tool &mdash; no hooks required.

### MCP (Model Context Protocol)

An open standard that lets AI agents call external tools. OpenCortex exposes 9 MCP tools through a Node.js stdio server that Claude Code, Cursor, and other MCP-compatible clients can use directly.

### CortexFS

The filesystem abstraction that manages the three-layer storage. Each memory becomes a directory with `.abstract.md` (L0), `.overview.md` (L1), and `content.md` (L2) files. CortexFS handles reading, writing, and hierarchical traversal.

### Intent Router

Analyzes each search query to determine the optimal retrieval strategy. A quick yes/no question gets 3 results at L0; a deep analysis request gets 10 results at L2. Uses keyword matching first (zero LLM cost), then optional LLM classification for complex queries.

### Qdrant

An open-source vector database. OpenCortex uses Qdrant in **embedded mode** &mdash; it runs as an in-process library with no separate server process to manage. Data is persisted to local files automatically.

### Embedding

The process of converting text into a numerical vector that captures its semantic meaning. OpenCortex supports local embedding (multilingual-e5-large via FastEmbed), Volcengine (doubao-embedding), OpenAI, and other providers. Local reranking is also supported (jina-reranker-v2-base-multilingual).

### URI Namespace

Every memory has a unique address in the format:
```
opencortex://{tenant}/{user_id}/{type}/{category}/{node_id}
```
This ensures complete data isolation between tenants and users.

---

## Architecture

### System Overview

```
AI Agent (Claude Code / Cursor / Custom)
  |
  |--- MCP Protocol (stdio) ----> Node.js MCP Server ---- HTTP ----> FastAPI HTTP Server (:8921)
  |                                (9 tools)                              |
  |                                                                        v
  |                                                                  MemoryOrchestrator
  |                                                                  (unified API layer)
  |                                                                        |
  |                                                         +--------------+--------------+
  |                                                         |              |              |
  |                                                    IntentRouter   ContextManager   Observer
  |                                                         |         (prepare/       (transcript
  |                                                         v          commit/end)     recording)
  |                                                  HierarchicalRetriever     |
  |                                                         |                  v
  |                                                         v            TraceSplitter → Archivist
  |                                                  CortexFS + Qdrant       → KnowledgeStore
  |                                                  (L0/L1/L2)  (vectors + RL)
  |
  |    (Identity from JWT Bearer token → RequestContextMiddleware → contextvars)
```

### Data Flow: Store

```
Agent calls memory_store(abstract="User prefers dark theme", content="...")
  |
  v
MemoryOrchestrator.add()
  |-- Generate embedding vector (1024-dim)
  |-- Auto-generate L1 overview (short content reused; long content summarized)
  |-- Write to CortexFS:  .abstract.md / .overview.md / content.md
  |-- Write to Qdrant:    vector + metadata + RL fields (reward_score=0)
  |
  v
Returns: { uri, context_type, category, abstract }
```

### Data Flow: Search

```
Agent calls memory_search(query="What theme does the user prefer?")
  |
  v
IntentRouter (3-layer analysis)
  |-- Layer 1: Keyword extraction (zero LLM cost)
  |-- Layer 2: LLM classification (optional, for complex queries)
  |-- Layer 3: Memory triggers (auto-append category queries)
  |-- Output: intent_type=quick_lookup, top_k=3, detail_level=L0
  |
  v
HierarchicalRetriever
  |-- Embed query -> vector search in Qdrant
  |-- Frontier batching: wave-based parallel directory traversal
  |-- Score propagation: child_score = a * child + (1-a) * parent
  |-- RL fusion: final += rl_weight * reward_score
  |-- Optional rerank: final = b * rerank + (1-b) * retrieval
  |-- Convergence check: stop when top-K stable for 3 waves
  |
  v
Returns: { results: [{ uri, abstract, score, overview? }], total }
```

### Data Flow: Memory Context Protocol

```
Agent calls memory_context(phase="prepare", session_id="s1", turn_id="t1",
                           messages=[{role: "user", content: "..."}])
  |
  v
ContextManager._prepare()
  |-- Auto-create session (Observer.begin_session if not active)
  |-- IntentRouter.route(query) → SearchIntent
  |-- orchestrator.search() → memory items
  |-- orchestrator.knowledge_search() → knowledge items
  |-- Return { memory, knowledge, instructions, intent }
  |
  v  (Agent generates response)
  v
Agent calls memory_context(phase="commit", session_id="s1", turn_id="t1",
                           messages=[...], cited_uris=[...])
  |
  v
ContextManager._commit()
  |-- Observer.record_batch() → in-memory transcript buffer
  |-- Async: apply RL reward to cited_uris
  |-- Return { accepted: true }
  |
  v  (Session ends)
  v
Agent calls memory_context(phase="end", session_id="s1")
  |
  v
ContextManager._end()
  |-- Observer.flush() → TraceSplitter → TraceStore
  |-- Archivist → Sandbox → KnowledgeStore
  |-- Cleanup all session state
  |-- Return { status: "closed", traces, knowledge_candidates }
```

### Data Flow: Feedback Loop

```
Agent calls memory_feedback(uri="opencortex://...", reward=1.0)
  |
  v
Update Qdrant RL fields
  |-- reward_score += 1.0
  |-- positive_feedback_count += 1
  |
  v
Next search: this memory ranks higher (score + 0.05 * reward)
Over time: apply_decay() reduces unused memories (0.95x per cycle)
```

### Deployment Modes

| Mode | How it works | Best for |
|------|-------------|----------|
| **Local** (default) | SessionStart hook auto-starts HTTP server; MCP server managed by Claude Code | Solo development |
| **Remote** | Connect to a pre-deployed HTTP server; no Python needed on client | Team sharing, server deployment |
| **Docker** | `docker compose up` with volume-mounted config | Production deployment |

---

## Getting Started

### Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| **Python** | >= 3.10 | HTTP server backend |
| **Node.js** | >= 18 | MCP server and plugin hooks |
| **uv** | Latest | Python package manager ([install](https://docs.astral.sh/uv/getting-started/installation/)) |

### 1. Clone and install

```bash
git clone https://github.com/StardustVision/OpenCortex.git
cd OpenCortex
uv sync
```

`uv sync` creates a virtual environment, installs all dependencies, and sets up the `opencortex-server` command.

### 2. Configure

Create a configuration file. The system searches in this order:

1. `./server.json` (project-local)
2. `~/.opencortex/server.json` (global, auto-created if missing)

**With embedding (full semantic search):**

```json
{
  "embedding_provider": "volcengine",
  "embedding_model": "doubao-embedding-vision-250615",
  "embedding_api_key": "YOUR_API_KEY",
  "embedding_api_base": "https://ark.cn-beijing.volces.com/api/v3",
  "http_server_host": "127.0.0.1",
  "http_server_port": 8921
}
```

**With local embedding (no API key needed):**

```json
{
  "embedding_provider": "local",
  "http_server_port": 8921
}
```

All server fields can be overridden via environment variables with the `OPENCORTEX_` prefix:
```bash
export OPENCORTEX_EMBEDDING_API_KEY=sk-xxx
```

### 3. Generate a token

Identity (tenant + user) is embedded in a JWT token:

```bash
uv run opencortex-token generate
# Enter tenant_id and user_id when prompted
# Token is saved to {data_root}/tokens.json

# For Docker:
docker exec -it opencortex-server uv run opencortex-token generate
```

Manage tokens:
```bash
uv run opencortex-token list       # View issued tokens
uv run opencortex-token revoke <prefix>  # Revoke by prefix
```

### 4. Start the server

```bash
uv run opencortex-server --port 8921
```

Verify it is running:
```bash
curl http://localhost:8921/api/v1/memory/health
```

### 5. Install the Claude Code plugin

Inside Claude Code:

```
/plugin install
```

Select `opencortex-memory`. Then run the setup wizard:

```bash
npx opencortex-cli setup
```

The wizard will ask you to choose local or remote mode, enter the server URL and JWT token, write the config to `~/.opencortex/mcp.json`, and optionally register the MCP server at Claude Code user level (so it works across all projects).

### 6. Docker deployment

```bash
# Build and start
docker compose up -d

# Check logs
docker compose logs -f opencortex

# Verify
curl http://localhost:8921/api/v1/memory/health
```

To use a config file, uncomment the volume mount in `docker-compose.yml`:
```yaml
volumes:
  - ./server.json:/app/server.json:ro
```

### 7. Configure the MCP client

The setup wizard (step 5) handles this automatically. To configure manually, edit `~/.opencortex/mcp.json`:

```json
{
  "mode": "remote",
  "token": "<jwt-token>",
  "remote": { "http_url": "http://your-server:8921" }
}
```

For local mode:
```json
{
  "mode": "local",
  "token": "<jwt-token>",
  "local": { "http_port": 8921 }
}
```

Config search order: `./mcp.json` (project-local) > `~/.opencortex/mcp.json` (global). Environment variable overrides (`OPENCORTEX_MODE`, `OPENCORTEX_HTTP_URL`, `OPENCORTEX_TOKEN`) take highest priority.

---

## Core Features

### Three-Layer Summaries (L0 / L1 / L2)

Each memory is stored at three precision levels. The system automatically selects the cheapest layer that satisfies the query:

```
L0 Abstract  ->  "User prefers dark theme"                     ~30 tokens
L1 Overview  ->  "Consistent across 10+ sessions. Applies      ~150 tokens
                  to VS Code, terminal, and browser tools.
                  Expressed as a strong preference."
L2 Content   ->  [Full conversation excerpt where this          ~500+ tokens
                  preference was discussed]
```

`add()` auto-generates L1: short content is reused directly; long content is summarized by LLM (or truncated if no LLM is configured).

### Intent-Aware Retrieval

The Intent Router analyzes each query and selects the retrieval strategy automatically:

| Intent | Trigger | Top-K | Detail | Example |
|--------|---------|-------|--------|---------|
| `quick_lookup` | Short confirmatory query | 3 | L0 | "Does the user like dark theme?" |
| `recent_recall` | Temporal keywords | 5 | L1 | "What did we discuss last time?" |
| `deep_analysis` | Needs full context | 10 | L2 | "Review the auth system design in detail" |
| `summarize` | Aggregation keywords | 30 | L1 | "Summarize recent architecture changes" |

### SONA Reinforcement Ranking

Positive feedback boosts a memory's score; negative feedback suppresses it. Time decay ensures stale memories fade:

```
final_score = beta * rerank + (1-beta) * retrieval + rl_weight * reward

feedback(uri, reward=+1.0)  ->  +0.05 boost in future searches
feedback(uri, reward=-1.0)  ->  -0.05 penalty
decay()                     ->  reward *= 0.95 (protected: 0.99)
```

### Knowledge Extraction (Cortex Alpha)

When a session ends, the Alpha pipeline automatically extracts reusable knowledge:

```
Observer transcript → TraceSplitter (LLM) → task traces
                                              |
                                     Archivist (LLM) → knowledge candidates
                                              |
                                     Sandbox (quality gate) → approved knowledge
                                              |
                                     KnowledgeStore (vector search)
```

Knowledge is searchable via `knowledge_search` and surfaced alongside memories during `memory_context` prepare.

### Memory Context Protocol

A platform-agnostic lifecycle that works with any MCP client:

```python
# 1. Before generating a response — get relevant context
prepare = memory_context(phase="prepare", session_id="s1", turn_id="t1",
                         messages=[{"role": "user", "content": "..."}])
# Returns: { memory: [...], knowledge: [...], instructions: {...} }

# 2. After generating a response — record the turn
memory_context(phase="commit", session_id="s1", turn_id="t1",
               messages=[...user + assistant...], cited_uris=["opencortex://..."])
# Returns: { accepted: true }

# 3. When done — close the session
memory_context(phase="end", session_id="s1")
# Returns: { status: "closed", traces: 3, knowledge_candidates: 1 }
```

Features: idempotent by `(session_id, turn_id)`, idle session auto-close, fallback JSONL on failure, async RL reward for cited URIs.

### Multi-Tenant Isolation

```
opencortex://{tenant}/{uid}/{type}/{category}/{node_id}
```

Complete data isolation between tenants and users. Team-level resources can be shared; user-level memories remain private. Per-request identity is extracted from the JWT Bearer token claims (`tid`/`uid`).

---

## API Reference

### REST API (HTTP Server)

#### Core Memory

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/memory/store` | Store a memory (auto-generates L1, embedding, URI) |
| POST | `/api/v1/memory/batch_store` | Batch store multiple documents |
| POST | `/api/v1/memory/search` | Semantic search with intent routing and RL fusion |
| POST | `/api/v1/memory/feedback` | Submit RL reward (+1 = useful, -1 = not useful) |
| GET | `/api/v1/memory/stats` | Storage statistics and configuration |
| POST | `/api/v1/memory/decay` | Trigger global reward decay |
| GET | `/api/v1/memory/health` | Component health check |

#### Context Protocol

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/context` | Unified lifecycle: prepare / commit / end |

#### Session

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/session/begin` | Start a new session (Observer recording) |
| POST | `/api/v1/session/message` | Add a message to the session |
| POST | `/api/v1/session/end` | End session, trigger trace splitting and knowledge extraction |

#### Knowledge (Cortex Alpha)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/knowledge/search` | Search approved knowledge |
| POST | `/api/v1/knowledge/approve` | Approve a knowledge candidate |
| POST | `/api/v1/knowledge/reject` | Reject a knowledge candidate |
| GET | `/api/v1/knowledge/candidates` | List pending knowledge candidates |
| POST | `/api/v1/archivist/trigger` | Manually trigger knowledge extraction |

#### System

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/intent/should_recall` | Decide whether recall is needed for a query |
| GET | `/api/v1/system/status` | Unified health/stats/doctor status |

### MCP Tools (9 tools)

The MCP server exposes the same capabilities as the REST API:

- `store` / `batch_store` / `search` / `feedback` / `decay`
- `recall` (prepare → search → return context)
- `add_message` (commit turn to Observer)
- `end` (close session → trace splitting → knowledge extraction)
- `system_status`

### Python API

```python
from opencortex import MemoryOrchestrator, CortexConfig, init_config

init_config(CortexConfig())
orch = MemoryOrchestrator(embedder=my_embedder)
await orch.init()

# Store
ctx = await orch.add(
    abstract="User prefers dark theme",
    content="Use dark theme in VS Code, terminal, and browser tools.",
    category="preferences",
)

# Search (Intent Router auto-selects strategy)
result = await orch.search("What theme does the user prefer?")
for m in result:
    print(m.uri, m.abstract, m.score)

# Feedback + Decay
await orch.feedback(uri=ctx.uri, reward=1.0)
await orch.decay()

# Session lifecycle
await orch.session_begin(session_id="s1")
await orch.session_message("s1", "user", "Help me fix this bug")
await orch.session_message("s1", "assistant", "The issue is...")
await orch.session_end("s1", quality_score=0.9)

# Knowledge search
results = await orch.knowledge_search("deployment workflow")

await orch.close()
```

---

## Plugin System

The `plugins/opencortex-memory` plugin provides the MCP server (tool proxy for any MCP-compatible client). Implemented in pure Node.js with zero external dependencies.

```
plugins/opencortex-memory/
  lib/
    mcp-server.mjs               # MCP stdio server (9 tools -> HTTP)
    common.mjs                   # Config discovery, state, uv/python detection
    http-client.mjs              # Native fetch wrapper + Bearer token auth
    transcript.mjs               # JSONL parsing
  bin/oc-cli.mjs                 # CLI: health, status, recall, store
```

### Session Lifecycle

The MCP server manages the session lifecycle internally via `recall` / `add_message` / `end` tools:

```
recall (before response) → add_message (after response) → end (session done)
```

No hooks needed. Any MCP-compatible client works.

---

## Repository Layout

```
src/opencortex/
  orchestrator.py                # MemoryOrchestrator (unified API)
  config.py                      # CortexConfig (dataclass + env overrides)
  http/                          # FastAPI server + async client + request context
  retrieve/                      # IntentRouter + HierarchicalRetriever + Rerank
  context/                       # ContextManager (Memory Context Protocol)
  alpha/                         # Cortex Alpha: Observer, TraceSplitter, Archivist, KnowledgeStore
  storage/                       # VikingDBInterface + CortexFS + Qdrant adapter
  models/                        # Embedder abstractions (local/API) + LLM factory

plugins/opencortex-memory/       # Claude Code plugin (pure Node.js)

tests/                           # 140+ Python tests + 8 Node.js tests
```

---

## Testing

```bash
# Core tests (no external dependencies)
uv run python3 -m unittest tests.test_e2e_phase1 tests.test_write_dedup tests.test_context_manager -v

# MCP server tests (requires running HTTP server)
node --test tests/test_mcp_server.mjs

# Full regression
uv run python3 -m unittest discover -s tests -v
```

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python 3.10+, async-first |
| Plugin & MCP | Node.js >= 18, pure ESM, zero external deps |
| Vector Store | Qdrant (embedded local mode, no separate process) |
| Embedding | Local (multilingual-e5-large) / Volcengine / OpenAI-compatible |
| Reranking | Local (jina-reranker-v2-base-multilingual) / API |
| HTTP | FastAPI + uvicorn |
| Package Manager | uv |

## License

[Apache-2.0](LICENSE)

## Acknowledgements

OpenCortex is ported and evolved from:

- [OpenViking](https://github.com/volcengine/openviking) &mdash; CortexFS three-layer storage, hierarchical retrieval algorithm, VikingDBInterface storage abstraction
