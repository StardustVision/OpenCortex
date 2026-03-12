# OpenCortex — Developer Guide

## Overview

OpenCortex is a memory and context management system for AI agents. It provides persistent, searchable, self-improving memory through three-layer summaries, reinforcement learning ranking, and trace-based knowledge extraction.

Core subsystems:
- **MemoryOrchestrator** — unified API layer wiring all components
- **CortexFS** — three-layer filesystem (L0 abstract / L1 overview / L2 content)
- **HierarchicalRetriever** — frontier-batching wave search with RL score fusion
- **IntentRouter** — 3-layer query analysis (keywords → LLM → memory triggers)
- **ContextManager** — three-phase lifecycle for Memory Context Protocol (prepare/commit/end)
- **Observer** — real-time session transcript recording
- **TraceSplitter** — LLM-driven conversation → task trace decomposition
- **TraceStore** — persistent trace storage (Qdrant + CortexFS)
- **Archivist** — knowledge extraction from traces
- **Sandbox** — quality gate for knowledge candidates (stat + LLM verification)
- **KnowledgeStore** — approved knowledge persistence and search
- **QdrantStorageAdapter** — embedded Qdrant with RL fields (reward, decay, protect)
- **RequestContextMiddleware** — per-request identity via HTTP headers

## Tech Stack

- Python 3.10+, async-first (HTTP server backend)
- Node.js >= 18 (MCP server + plugin hooks, zero external deps)
- Vector store: Qdrant (embedded local mode, no separate process)
- Embedding: Local (multilingual-e5-large) / Volcengine / OpenAI-compatible
- Reranking: Local (jina-reranker-v2-base-multilingual) / API
- HTTP: FastAPI + uvicorn + httpx
- MCP: Node.js stdio proxy (9 tools → HTTP API)
- Tests: unittest (140+ Python) + node:test (8 Node.js MCP)

## Directory Structure

```
src/opencortex/
  config.py                      # CortexConfig dataclass + env overrides (server-only settings)
  orchestrator.py                # MemoryOrchestrator — top-level API
  http/
    server.py                    # FastAPI app + REST routes + RequestContextMiddleware
    request_context.py           # Per-request contextvars (identity)
    client.py                    # OpenCortexClient (async HTTP client)
    models.py                    # Pydantic request models
    __main__.py                  # CLI entry point (opencortex-server)
  context/
    manager.py                   # ContextManager — Memory Context Protocol lifecycle
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
    rerank_client.py             # RerankClient (API / local / LLM / disabled)
    types.py                     # TypedQuery / SearchIntent / FindResult / DetailLevel
  alpha/
    observer.py                  # Observer — real-time transcript recording
    trace_splitter.py            # TraceSplitter — conversation → task traces
    trace_store.py               # TraceStore — persistent trace storage
    archivist.py                 # Archivist — knowledge extraction from traces
    sandbox.py                   # Sandbox — quality gate for knowledge candidates
    knowledge_store.py           # KnowledgeStore — approved knowledge persistence
    types.py                     # Trace / KnowledgeItem / KnowledgeScope enums
  ingest/
    resolver.py                  # IngestModeResolver — three-mode routing (memory/document/conversation)
  parse/
    base.py                      # ParsedChunk dataclass, ParserConfig, estimate_tokens
    parsers/
      base_parser.py             # BaseParser ABC
      markdown.py                # MarkdownParser — heading-based chunking with hierarchy
      text.py                    # TextParser (delegates to MarkdownParser)
      word.py                    # WordParser (python-docx → markdown → MarkdownParser)
      excel.py                   # ExcelParser (openpyxl → markdown tables)
      powerpoint.py              # PowerPointParser (python-pptx → markdown)
      pdf.py                     # PDFParser (pdfplumber → markdown)
      epub.py                    # EPUBParser (ebooklib → markdown)
    registry.py                  # ParserRegistry — extension-based parser dispatch
  models/embedder/               # EmbedderBase / Dense / Sparse / Hybrid abstractions
  utils/
    uri.py                       # CortexURI tenant-isolated URI scheme

plugins/opencortex-memory/       # MCP plugin (pure Node.js, no hooks)
  lib/common.mjs                 # Config discovery, state, uv/python detection, server launcher
  lib/http-client.mjs            # Native fetch wrapper + buildClientHeaders()
  lib/transcript.mjs             # JSONL parsing (diagnostic utility)
  lib/mcp-server.mjs             # MCP stdio server (9 tools + session lifecycle)
  bin/oc-cli.mjs                 # CLI tool

tests/
  test_e2e_phase1.py             # 24 E2E tests
  test_ingestion_e2e.py          # Ingestion pipeline E2E (memory/document/conversation modes)
  test_mcp_server.mjs            # 9 MCP tests (Node.js)
  test_write_dedup.py            # Write dedup tests
  test_context_manager.py        # Memory Context Protocol tests (8 scenarios)
  test_alpha_*.py                # Cortex Alpha component tests
  test_parse_*.py                # Parser subsystem tests
  test_ingest_resolver.py        # IngestModeResolver routing tests
  test_batch_add_hierarchy.py    # batch_add directory tree tests
  test_intent_*.py               # IntentRouter optimization tests
  test_conversation_*.py         # Conversation mode tests
  test_document_mode.py          # Document mode tests
```

## Development Conventions

- All storage operations go through `VikingDBInterface` — every method is `async`
- URI format: `opencortex://{team}/user/{uid}/{type}/{category}/{node_id}`
- **Client-side config via HTTP headers**: identity settings are NOT in server-side `CortexConfig`. They are sent per-request by the client (MCP plugin reads from `mcp.json`). `RequestContextMiddleware` parses headers → contextvars.
  - Identity: `X-Tenant-ID` / `X-User-ID` → `get_effective_identity()`
- **Server config** (`CortexConfig`): only server-side settings — storage, embedding, LLM, rerank, HTTP bind. Loads from `server.json` or `~/.opencortex/server.json`.
- **Client config** (`mcp.json`): identity settings. Loads from `mcp.json` or `~/.opencortex/mcp.json`. Node.js `buildClientHeaders()` attaches them to every HTTP request.
- RL methods (`update_reward`, `get_profile`, `apply_decay`, `set_protected`) are not in the interface — detected via `hasattr` on the adapter
- Package management uses `uv` (not pip)
- VikingFS has been renamed to CortexFS; old name retained for backward compatibility

## Architecture

### Call Chains

```
MCP path:   Agent → node mcp-server.mjs (stdio) → fetch + headers → HTTP Server (FastAPI) → Orchestrator → Qdrant

Headers:    mcp.json → buildClientHeaders() → X-Tenant-ID, X-User-ID
            → RequestContextMiddleware → contextvars → get_effective_identity()
```

### Memory Context Protocol

```
recall (prepare)         → ContextManager → IntentRouter + search() + knowledge_search()
                           → returns { memory, knowledge, instructions }
add_message (commit)     → ContextManager → Observer.record_batch() + async RL reward
end (end)                → ContextManager → orchestrator.session_end()
                           → Observer.flush → TraceSplitter → TraceStore → Archivist
```

### Knowledge Pipeline (Cortex Alpha)

```
session_begin/message   → Observer records transcript
session_end             → TraceSplitter → traces → TraceStore
                        → Archivist (if threshold met) → knowledge candidates
                        → Sandbox quality gate → KnowledgeStore (approved)
knowledge_search        → vector search over approved knowledge
```

### Three-Mode Ingestion

```
add() → IngestModeResolver.resolve() → mode decision
  ├─ memory:       pass-through (short text, batch items) → single record
  ├─ document:     ParserRegistry → MarkdownParser → chunking → hierarchy → multiple records
  └─ conversation: _write_immediate (per-message, zero-LLM) + merge layer (~1000 tokens → LLM chunk)
```

**IngestModeResolver priority** (highest first):
1. Explicit `meta.ingest_mode` override
2. `meta.source == "batch:*"` or `meta.source_path` present → memory
3. `session_id` present → conversation
4. Dialog patterns (user:/assistant:) → conversation
5. Headings + content > 4000 tokens → document
6. Default → memory

**Document mode**: Large content parsed via `ParserRegistry` → `MarkdownParser` (heading-based chunking with parent-child hierarchy via `parent_index`). Each chunk gets LLM-derived abstract/overview/keywords. Chunks written as individual records with `parent_uri` linking.

**Conversation mode**: Two-layer approach:
- **Immediate layer**: `_write_immediate()` — per-message embed + Qdrant write, no LLM, instant searchability
- **Merge layer**: `ConversationBuffer` accumulates messages; at ~1000 tokens threshold, LLM derives a merged chunk with full three-layer summary

**batch_add**: When `scan_meta` present, builds directory tree from `meta.file_path` values (directory nodes with `is_leaf=False`), then assigns `parent_uri` to leaf items.

### IntentRouter Optimization

```
route(query, session_context=None)
  ├─ session_context is None → keyword-only (zero LLM) → single TypedQuery
  └─ session_context present → LLM IntentAnalyzer → multi-query concurrent retrieval
```

- LRU cache (128 entries, 60s TTL) avoids repeated LLM calls for identical queries
- Multi-query: LLM returns `queries[]` array → each becomes a separate `TypedQuery` for concurrent search

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

Pure Node.js stdio proxy with built-in session lifecycle management. The MCP client manages its lifecycle via `.mcp.json`. The server translates MCP tool calls into HTTP requests to the FastAPI server. Session state (recall/add_message/end) is managed internally — no hooks required.

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
uv run python3 -m unittest tests.test_e2e_phase1 tests.test_write_dedup tests.test_context_manager -v

# Node.js MCP tests (requires running HTTP server)
node --test tests/test_mcp_server.mjs

# Full regression
uv run python3 -m unittest discover -s tests -v
```
