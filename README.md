<p align="center">
  <h1 align="center">OpenCortex</h1>
  <p align="center">Memory and context management for AI agents</p>
  <p align="center">
    <a href="#quick-start">Quick Start</a> ·
    <a href="#architecture">Architecture</a> ·
    <a href="#plugin-system">Plugin</a> ·
    <a href="#mcp-tools">MCP Tools</a> ·
    <a href="#evaluation-and-testing">Evaluation</a>
  </p>
</p>

---

English documentation is in this file.

- Chinese version: [README_CN](README_CN.md)
- Architecture details: [docs/architecture.md](docs/architecture.md)
- MCP server docs: [docs/mcp-server.md](docs/mcp-server.md)
- ACE design docs: [docs/ace-design.md](docs/ace-design.md)

## Why OpenCortex

LLM agents have limited context windows and usually forget everything after a session ends.

OpenCortex adds persistent, retrievable, and evolving memory to agent workflows:

- Recall user preferences across sessions
- Reuse known fixes for repeated errors
- Keep architecture and code decisions available as context

It is not a simple key-value store. It is a complete memory engine with layered summaries, semantic retrieval, and reinforcement-driven ranking.

## Core Capabilities

### 1. Three-Layer Summaries (L0 / L1 / L2)

- L0: one-line abstract, optimized for vector retrieval
- L1: paragraph-level overview, optimized for low-token reasoning
- L2: full content, loaded only when needed

`add()` automatically generates L1 overview:

- short content: reused directly
- long content: summarized by LLM (or truncated fallback without LLM)

### 2. Intent-Aware Retrieval

Intent Router dynamically selects retrieval strategy (top-k, detail level, time scope):

- quick lookup: low-cost L0 retrieval
- recent recall: medium-depth L1 retrieval
- deep analysis: L2 retrieval with richer context
- summarize: larger top-k aggregation

### 3. SONA Reinforcement Ranking

Memory ranking integrates semantic similarity and RL feedback:

```text
fused_score = similarity + rl_weight * reward_score
```

- positive feedback: memory moves up
- stale/unused memory: decays over time

### 4. ACE Self-Learning Loop

OpenCortex includes ACE (Agentic Context Engine):

- RuleExtractor: zero-LLM extraction of reusable skills
- Skillbook: persistence + retrieval of operational skills
- Feedback loop: helpful/harmful tags improve future selection

### 5. Session Self-Iteration

On session end, hooks can automatically:

1. parse transcript
2. summarize the turn
3. store reusable memory

### 6. Tenant/User Isolation

URI namespace supports multi-tenant and per-user isolation:

```text
opencortex://{team}/user/{uid}/{type}/{category}/{node_id}
```

## Architecture

```text
Agent (Claude Code / Cursor / Custom)
  │
  ├─ MCP tools ──→ node mcp-server.mjs (stdio) ──→ fetch ──→ HTTP Server (FastAPI :8921)
  │                                                              │
  │                                                              v
  │                                                        MemoryOrchestrator
  │                                                        (add/search/feedback/decay/session)
  │                                                              │
  │                                                              v
  │                                                        IntentRouter + Retriever + ACE + SessionManager
  │                                                              │
  │                                                              v
  │                                                        CortexFS (L0/L1/L2) + Qdrant adapter
  │
  └─ Hooks ──→ node run.mjs <hook-name>
                  ├─ session-start   → start HTTP server (local) / health check (remote)
                  ├─ user-prompt-submit → inject memory recall prompt
                  ├─ stop            → ingest transcript turn via HTTP
                  └─ session-end     → store session summary, stop HTTP server
```

The plugin hooks and MCP server are pure Node.js (.mjs) with zero external dependencies. Only the HTTP server backend requires Python.

## Deployment Modes

- **Local** (default): SessionStart hook auto-starts the HTTP server; MCP server is managed by Claude Code via `.mcp.json`
- **Remote**: connect to a pre-deployed HTTP server; no Python needed on the client

Example `plugins/opencortex-memory/config.json`:

```json
{
  "mode": "local",
  "local": { "http_port": 8921 },
  "remote": { "http_url": "http://your-server:8921" }
}
```

## Quick Start

### 1. Install

```bash
git clone https://github.com/StardustVision/OpenCortex.git
cd OpenCortex
uv pip install -e .
```

### 2. Configure

Create `opencortex.json` in your project root (or `$HOME/.opencortex/opencortex.json` for global config):

```json
{
  "tenant_id": "my-team",
  "user_id": "my-name",
  "embedding_provider": "volcengine",
  "embedding_model": "doubao-embedding-vision-250615",
  "embedding_api_key": "YOUR_API_KEY",
  "embedding_api_base": "https://ark.cn-beijing.volces.com/api/v3",
  "http_server_host": "127.0.0.1",
  "http_server_port": 8921,
  "mcp_transport": "streamable-http",
  "mcp_port": 8920
}
```

No-embedding mode is also supported (filter/scroll fallback):

```json
{
  "tenant_id": "my-team",
  "user_id": "my-name",
  "embedding_provider": "none",
  "http_server_port": 8921
}
```

### 3. Install Claude Code Plugin

```bash
/plugin install
```

Select `opencortex-memory` from the plugin list. Claude Code automatically registers hooks from `hooks/hooks.json` and the MCP server from `.mcp.json`.

### 4. Optional Manual Startup

The plugin hooks auto-start the HTTP server when a Claude Code session begins. For manual startup:

```bash
# HTTP Server only (MCP server is managed by Claude Code)
uv run opencortex-server --port 8921
```

### 5. Claude Code Integration (Other Projects)

Add `.mcp.json` to your project root:

```json
{
  "mcpServers": {
    "opencortex": {
      "command": "node",
      "args": ["path/to/plugins/opencortex-memory/lib/mcp-server.mjs"]
    }
  }
}
```

## Plugin System

`plugins/opencortex-memory` combines hooks (passive memory), MCP server (tool proxy), and skills (active memory tools).

All hooks and the MCP server are implemented in pure Node.js (.mjs) — no bash, PowerShell, or Python dependencies.

```
plugins/opencortex-memory/
├── hooks/
│   ├── hooks.json                    # Hook registration
│   ├── run.mjs                       # Unified entry point
│   └── handlers/
│       ├── session-start.mjs         # Start HTTP server, init state
│       ├── user-prompt-submit.mjs    # Inject memory recall prompt
│       ├── stop.mjs                  # Ingest transcript turn
│       └── session-end.mjs           # Store summary, stop server
├── lib/
│   ├── common.mjs                    # Config, state, path resolution
│   ├── http-client.mjs               # Native fetch wrapper
│   ├── transcript.mjs                # JSONL parsing + summarization
│   └── mcp-server.mjs               # MCP stdio server (25 tools)
├── bin/
│   └── oc-cli.mjs                    # CLI: health, status, recall, store
├── skills/                           # Skill definitions
└── config.json                       # Mode config (local/remote)
```

| Hook | Handler | Purpose |
|------|---------|---------|
| SessionStart | `session-start.mjs` | Start HTTP server (local), verify connectivity (remote) |
| UserPromptSubmit | `user-prompt-submit.mjs` | Inject memory recall system message |
| Stop | `stop.mjs` | Parse transcript, ingest turn (async, fire-and-forget) |
| SessionEnd | `session-end.mjs` | Store session summary, kill HTTP server |

## MCP Tools

### Core Memory

- `memory_store`
- `memory_search`
- `memory_feedback`
- `memory_stats`
- `memory_decay`
- `memory_health`

### Session

- `session_begin`
- `session_message`
- `session_end`

### Hooks/Integration

- `memory_hooks_learn`
- `memory_hooks_remember`
- `memory_hooks_recall`
- `memory_hooks_stats`
- trajectory / error / integration endpoints

## HTTP Server REST API

### Core Memory

- `POST /api/v1/memory/store`
- `POST /api/v1/memory/search`
- `POST /api/v1/memory/feedback`
- `GET /api/v1/memory/stats`
- `POST /api/v1/memory/decay`
- `GET /api/v1/memory/health`

### Session

- `POST /api/v1/session/begin`
- `POST /api/v1/session/message`
- `POST /api/v1/session/end`

### Hooks and Integration

- `POST /api/v1/hooks/*`
- `POST/GET /api/v1/integration/*`

## Python API Example

```python
from opencortex import MemoryOrchestrator, CortexConfig, init_config

init_config(CortexConfig(tenant_id="myteam", user_id="alice"))
orch = MemoryOrchestrator(embedder=my_embedder)
await orch.init()

ctx = await orch.add(
    abstract="User prefers dark theme",
    content="Use dark theme in VS Code, terminal, and browser tools.",
    category="preferences",
)

result = await orch.search("What theme does the user prefer?")
for m in result.memories:
    print(m.uri, m.abstract, m.score)

await orch.feedback(uri=ctx.uri, reward=1.0)
await orch.decay()
await orch.close()
```

## Repository Layout

```text
src/opencortex/
  orchestrator.py          # top-level orchestration
  http/                    # FastAPI server and HTTP client
  retrieve/                # router, retriever, rerank
  session/                 # extraction and session lifecycle
  ace/                     # self-learning engine
  storage/                 # CortexFS + Qdrant adapter
  models/                  # embedders and llm factory
plugins/opencortex-memory/ # Claude Code plugin (Node.js hooks + MCP server + skills)
tests/                     # unit, integration, and live tests
```

## Evaluation and Testing

### Memory Retrieval Evaluation

Use the built-in evaluation script:

```bash
PYTHONPATH=src python3 scripts/eval_memory.py \
  --dataset examples/memory_eval_dataset.sample.json \
  --base-url http://127.0.0.1:8921 \
  --k 1,3,5 \
  --output _bmad-output/memory-eval-report.json
```

Reported metrics include:

- `recall@k`
- `precision@k`
- `accuracy@k` / `hit_rate@k`
- `mrr`
- token comparison (`tokens_with_memory` vs `tokens_without_memory`)

See full plan: [docs/memory-test-plan.md](docs/memory-test-plan.md)

### Run Tests

```bash
# full regression
uv run python3 -m unittest discover -s tests -v

# evaluation unit tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_memory_eval -v
```

## Tech Stack

- Python 3.10+ (HTTP server backend)
- Node.js >= 18 (MCP server + plugin hooks, zero external deps)
- FastAPI + uvicorn
- Qdrant (embedded local mode)
- Volcengine/OpenAI-compatible embedding + LLM backends
- `uv` for Python package management

## License

[Apache-2.0](LICENSE)

## Acknowledgements

OpenCortex is ported and evolved from these projects:

- [OpenViking](https://github.com/volcengine/openviking)
- [Agentic Context Engine (ACE)](https://github.com/kayba-ai/agentic-context-engine)
