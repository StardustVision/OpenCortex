# OpenCortex — Developer Guide

## Overview

OpenCortex is a memory and context management system for AI agents. It provides persistent, searchable, self-improving memory through three-layer summaries, reinforcement learning ranking, and self-learning skill extraction.

Core subsystems:
- **MemoryOrchestrator** — unified API layer (~1500 lines) wiring all components
- **CortexFS** — three-layer filesystem (L0 abstract / L1 overview / L2 content)
- **HierarchicalRetriever** — frontier-batching wave search with RL score fusion
- **IntentRouter** — 3-layer query analysis (keywords → LLM → memory triggers)
- **ACEngine** — self-learning loop (RuleExtractor → Skillbook)
- **SessionManager** — session lifecycle with LLM memory extraction
- **QdrantStorageAdapter** — embedded Qdrant with RL fields (reward, decay, protect)
- **RequestContextMiddleware** — per-request identity + ACE config via HTTP headers

## Tech Stack

- Python 3.10+, async-first (HTTP server backend)
- Node.js >= 18 (MCP server + plugin hooks, zero external deps)
- Vector store: Qdrant (embedded local mode, no separate process)
- Embedding: Volcengine doubao-embedding-vision (1024 dim) / OpenAI-compatible
- HTTP: FastAPI + uvicorn + httpx
- MCP: Node.js stdio proxy (25 tools → HTTP API)
- Tests: unittest (111+ Python) + node:test (8 Node.js MCP)

## Directory Structure

```
src/opencortex/
  config.py                      # CortexConfig dataclass + env overrides (server-only settings)
  orchestrator.py                # MemoryOrchestrator — top-level API
  http/
    server.py                    # FastAPI app + REST routes + RequestContextMiddleware
    request_context.py           # Per-request contextvars (identity + ACEConfig)
    client.py                    # OpenCortexClient (async HTTP client)
    models.py                    # Pydantic request models
    __main__.py                  # CLI entry point (opencortex-server)
  storage/
    vikingdb_interface.py        # Abstract interface (25 async methods)
    cortex_fs.py                 # CortexFS three-layer filesystem (formerly VikingFS)
    collection_schemas.py        # Collection schemas (includes RL fields)
    qdrant/
      adapter.py                 # QdrantStorageAdapter (standard + RL faces)
      filter_translator.py       # VikingDB DSL → Qdrant Filter translation
      rl_types.py                # Profile / DecayResult dataclasses
  retrieve/
    hierarchical_retriever.py    # Wave-based frontier batching + RL fusion
    intent_router.py             # IntentRouter (keyword + LLM + memory triggers)
    intent_analyzer.py           # LLM intent analysis → QueryPlan
    rerank_client.py             # RerankClient (API / LLM / disabled)
    types.py                     # TypedQuery / SearchIntent / FindResult / DetailLevel
  ace/
    engine.py                    # ACEngine (Skillbook + Reflector + SkillManager)
    skillbook.py                 # Skillbook CRUD + vector search + CortexFS persistence
    rule_extractor.py            # RuleExtractor — zero-LLM skill extraction
    reflector.py                 # LLM reflection (optional)
    skill_manager.py             # LLM strategy management (optional)
    types.py                     # Skill / Learning / UpdateOperation
  session/
    manager.py                   # SessionManager (begin/message/end)
    extractor.py                 # MemoryExtractor (LLM-driven)
    types.py                     # SessionContext / ExtractedMemory
  models/embedder/               # EmbedderBase / Dense / Sparse / Hybrid abstractions
  utils/
    uri.py                       # CortexURI tenant-isolated URI scheme

plugins/opencortex-memory/       # Claude Code plugin (pure Node.js)
  hooks/run.mjs                  # Hook unified entry point
  hooks/handlers/*.mjs           # 4 hook handlers (session-start, user-prompt-submit, stop, session-end)
  lib/common.mjs                 # Config discovery, state, uv/python detection
  lib/http-client.mjs            # Native fetch wrapper + buildClientHeaders()
  lib/transcript.mjs             # JSONL parsing
  lib/mcp-server.mjs             # MCP stdio server (25 tools)
  bin/oc-cli.mjs                 # CLI tool

tests/
  test_e2e_phase1.py             # 24 E2E tests
  test_mcp_server.mjs            # 8 MCP tests (Node.js)
  test_ace_phase1.py             # 21 ACE tests
  test_ace_phase2.py             # 17 ACE Phase 2 tests
  test_rule_extractor.py         # 20 rule extraction tests
  test_skill_search_fusion.py    # 11 skill fusion search tests
  test_integration_skill_pipeline.py  # 10 Qdrant integration tests
```

## Development Conventions

- All storage operations go through `VikingDBInterface` — every method is `async`
- URI format: `opencortex://{team}/user/{uid}/{type}/{category}/{node_id}`
- **Client-side config via HTTP headers**: identity and ACE skill sharing settings are NOT in server-side `CortexConfig`. They are sent per-request by the client (MCP plugin reads from `mcp.json`). `RequestContextMiddleware` parses headers → contextvars.
  - Identity: `X-Tenant-ID` / `X-User-ID` → `get_effective_identity()`
  - ACE: `X-Share-Skills-To-Team` / `X-Skill-Share-Mode` / `X-Skill-Share-Score-Threshold` / `X-ACE-Scope-Enforcement` → `get_effective_ace_config()`
- **Server config** (`CortexConfig`): only server-side settings — storage, embedding, LLM, rerank, HTTP bind. Loads from `server.json` or `~/.opencortex/server.json`.
- **Client config** (`mcp.json`): identity + ACE settings. Loads from `mcp.json` or `~/.opencortex/mcp.json`. Node.js `buildClientHeaders()` attaches them to every HTTP request.
- RL methods (`update_reward`, `get_profile`, `apply_decay`, `set_protected`) are not in the interface — detected via `hasattr` on the adapter
- Package management uses `uv` (not pip)
- VikingFS has been renamed to CortexFS; old name retained for backward compatibility

## Architecture

### Call Chains

```
MCP path:   Agent → node mcp-server.mjs (stdio) → fetch + headers → HTTP Server (FastAPI) → Orchestrator → Qdrant
Hooks path: Agent → node run.mjs <hook> → fetch + headers → HTTP Server

Headers:    mcp.json → buildClientHeaders() → X-Tenant-ID, X-User-ID, X-Share-Skills-To-Team, ...
            → RequestContextMiddleware → contextvars → get_effective_identity() / get_effective_ace_config()
```

### Self-Learning Loop

```
memory_store (add)     → RuleExtractor async-extracts skills → Skillbook persists
memory_search (search) → parallel search contexts + skillbooks → hybrid sort + return
memory_feedback        → update RL reward / Skillbook tag (helpful/harmful)
```

### Score Fusion Formula

```
final = beta * rerank_score + (1 - beta) * retrieval_score + rl_weight * reward_score
```

Where `rl_weight = 0.05` (conservative), `beta = 0.7` (rerank weight).

### Storage Dual-Write

Each memory is written to both:
1. **CortexFS**: `.abstract.md` (L0) + `.overview.md` (L1) + `content.md` (L2)
2. **Qdrant**: embedding vector + L0/L1 as payload fields + RL fields

Search returns L0/L1 from Qdrant (zero filesystem I/O). L2 requires a CortexFS read.

### MCP Server

Pure Node.js stdio proxy. Claude Code manages its lifecycle via `.mcp.json`. The server translates MCP tool calls into HTTP requests to the FastAPI server.

## HTTP Server

```bash
uv run opencortex-server --host 127.0.0.1 --port 8921
```

Or via Docker:

```bash
docker compose up -d
```

## Running Tests

```bash
# Python core tests (no external dependencies)
uv run python3 -m unittest tests.test_e2e_phase1 tests.test_ace_phase1 tests.test_ace_phase2 tests.test_rule_extractor tests.test_skill_search_fusion tests.test_integration_skill_pipeline -v

# Node.js MCP tests (requires running HTTP server)
node --test tests/test_mcp_server.mjs

# Full regression
uv run python3 -m unittest discover -s tests -v
```

## ACE Learned Strategies

<!-- ACE:START - Do not edit manually -->
<!-- ACE is disabled by default (ace_enabled: false). Enable in server.json to resume skill extraction. -->
<!-- ACE:END -->
