<h1 align="center">OpenCortex</h1>
<p align="center"><strong>Persistent memory and context infrastructure for AI agents</strong></p>
<p align="center">
  <a href="#what-is-opencortex">Overview</a> &middot;
  <a href="#key-concepts">Key Concepts</a> &middot;
  <a href="#architecture-overview">Architecture</a> &middot;
  <a href="#quick-start">Quick Start</a> &middot;
  <a href="#core-features">Features</a> &middot;
  <a href="#api-overview">API</a> &middot;
  <a href="#repository-layout">Repository</a> &middot;
  <a href="README_CN.md">中文文档</a>
</p>

---

## What is OpenCortex

LLM agents forget. Session context, user preferences, design decisions, debugging history, and reusable workflows disappear unless they are stored outside the model window.

OpenCortex is the persistence layer for that problem. It combines layered memory storage, intent-aware recall planning, and retrieval tuned for agent workflows, then exposes the result through one HTTP backend and one MCP package.

In practice, OpenCortex is built for:

- cross-session memory and project context
- document and conversation ingestion
- retrieval that balances relevance, recency, feedback, and structure
- optional knowledge, insights, and skill-oriented services on the same substrate
- multi-tenant and project-scoped isolation via JWT-backed identity

## Key Concepts

### Three-layer memory

Each record is stored at multiple levels of detail:

| Layer | Role |
|---|---|
| `L0` | Small abstract for cheap indexing and quick confirmation |
| `L1` | Structured overview for most recall responses |
| `L2` | Full content for deep inspection and audits |

### Explicit recall planning

OpenCortex does not treat every query as a generic vector search. Queries are classified, routed, and turned into a recall plan that decides whether recall should run, which context to search, and how much detail to return.

### Retrieval beyond embeddings

Search combines multiple signals instead of a single vector score. Depending on configuration and query type, ranking can use semantic search, lexical weighting, rerank gating, explicit feedback, hotness, and cone-style expansion around shared entities.

### Context lifecycle

The central lifecycle endpoint is `/api/v1/context`. It drives three phases:

- `prepare`: plan recall and return memory or knowledge context
- `commit`: record the turn and feedback signals
- `end`: flush session state and optional post-processing

### Shared memory substrate

Core memory, optional knowledge extraction, insights reporting, and the skill engine share the same storage, identity, and retrieval base instead of standing up separate systems.

## Architecture Overview

```text
AI client
  -> MCP package (plugins/opencortex-memory/)
  -> FastAPI server
  -> MemoryOrchestrator
     -> ingest pipelines for memory / document / conversation
     -> recall planning and retrieval
     -> CortexFS + embedded Qdrant storage
     -> optional knowledge / insights / skill services
  -> optional web console at /console
```

At a high level, agents talk to the MCP package, the MCP package talks to the FastAPI backend, and the backend coordinates storage, recall, context lifecycle, and optional higher-level analysis services.

## Quick Start

### Requirements

- Python `>=3.10`
- Node.js `>=18`
- `uv`

### 1. Install

```bash
git clone --recurse-submodules https://github.com/StardustVision/OpenCortex.git
cd OpenCortex
uv sync
```

### 2. Start the backend

```bash
uv run opencortex-server --host 127.0.0.1 --port 8921
```

Generate or inspect tokens when needed:

```bash
uv run opencortex-token generate
uv run opencortex-token list
```

### 3. Connect an MCP client

Claude Code:

```bash
claude mcp add opencortex -- npx -y opencortex-memory
```

Codex CLI:

```bash
codex mcp add opencortex -- npx -y opencortex-memory
```

Gemini CLI:

```bash
gemini mcp add opencortex -- npx -y opencortex-memory
```

Then run the setup wizard:

```bash
npx opencortex-cli setup
```

The MCP config lives in `./mcp.json` or `~/.opencortex/mcp.json`, depending on mode and scope.

### 4. Docker option

```bash
docker compose up -d
docker compose logs -f
```

If built frontend assets are present, the console is served at `http://127.0.0.1:8921/console`.

## Core Features

OpenCortex centers on one memory substrate that handles short facts, documents, and conversations; explicit lifecycle handling through `/api/v1/context`; retrieval that can mix semantic, lexical, feedback, recency, and structure-aware signals; and optional knowledge, insights, and skill workflows on the same backend under request-scoped isolation.

## API Overview

OpenCortex exposes a broader API than this landing page lists. The most important areas are:

- Memory: persistent storage and retrieval for memories, documents, and conversations
- Context and session: the agent lifecycle centered on `/api/v1/context`
- Content and observability: layered content reads plus health or diagnostics surfaces
- Knowledge / insights / skills: optional higher-level workflows built on the same backend
- Auth / admin: identity, tokens, diagnostics, and administrative maintenance

Concrete next stops for route-level details:

- `src/opencortex/http/`
- `src/opencortex/skill_engine/`
- `src/opencortex/insights/`

## Repository Layout

At the top level, the repository is organized around the core backend in `src/opencortex/`, the optional console in `web/`, the MCP integration layer in `plugins/opencortex-memory/`, automated verification in `tests/`, and supporting material under `docs/`, `scripts/`, and `examples/`.

## Deep Dives

- [Three-layer storage](docs/architecture/three-layer-storage.md)
- [Cone retrieval](docs/architecture/cone-retrieval.md)
- [Autophagy and context lifecycle](docs/architecture/autophagy.md)
- [Skill engine](docs/architecture/skill-engine.md)

## Testing

```bash
uv run --group dev pytest
```

The MCP package under `plugins/opencortex-memory/` also carries its own Node.js test suite.

## Tech Stack

OpenCortex uses a Python/FastAPI backend, CortexFS plus embedded Qdrant for storage, a Node.js MCP package for client integration, and React/Vite for the optional console.

## License

Apache-2.0
