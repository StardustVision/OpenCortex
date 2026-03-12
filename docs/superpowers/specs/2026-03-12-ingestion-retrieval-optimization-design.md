# OpenCortex Ingestion & Retrieval Pipeline Optimization — Design Spec

## 1. Problem Statement

OpenCortex currently suffers from three core issues when handling complex long-form content:

1. **Zero retrieval hits on QA benchmarks**: Conversations and documents are stored as coarse single records. Vector density is too low to match fine-grained factual queries. LoCoMo benchmark shows 0 memories retrieved across all 8 completed conversations.
2. **Slow retrieval pipeline (8-22s)**: The IntentRouter invokes LLM on every query, even trivial ones. The underlying Qdrant search takes only tens of milliseconds.
3. **Flat document storage**: `batch_store` / `oc-scan` stores each file as an independent record with no directory hierarchy, making structural navigation impossible.

### Root Causes Identified

- **HierarchicalRetriever directory filter bug**: Directory nodes have `category=""` but wave search applies full `metadata_filter` (including `category=X`), filtering out all `is_leaf=False` nodes needed for tree traversal.
- **Single-record ingestion**: No chunking for conversations or documents — one input becomes one record regardless of length.
- **Vectorization text too narrow**: `Context.get_vectorization_text()` only returns `abstract`, missing keywords signal.
- **No session awareness in IntentRouter**: Every query triggers LLM analysis, no fast-path for simple lookups.

## 2. Design Goals

1. **Three-mode smart ingestion**: Memory (short facts), Document (long docs + code), Conversation (multi-turn dialog) — automatically routed.
2. **Two-layer incremental chunking for conversations**: Every message immediately searchable; merged into high-quality chunks at token threshold.
3. **Hierarchical document parsing**: Port OpenViking's multi-format parser suite, build parent-child trees in Qdrant.
4. **Code repository support**: `oc-scan` + `batch_add` produces hierarchical tree from file paths.
5. **Retrieval performance**: Fix directory filter bug, expand vectorization text, optimize IntentRouter with multi-query concurrency.
6. **API compatibility**: Existing `store`, `batch_store`, `search`, `recall` APIs unchanged.

## 3. Architecture Overview

```
                        ┌─────────────────────┐
                        │   store / batch_store │
                        │   add_message         │
                        └──────────┬────────────┘
                                   │
                        ┌──────────▼────────────┐
                        │   IngestModeResolver   │
                        │  (route to 3 modes)    │
                        └──┬───────┬─────────┬───┘
                           │       │         │
                    ┌──────▼──┐ ┌──▼──────┐ ┌▼──────────┐
                    │ Memory  │ │Document │ │Conversation│
                    │  Mode   │ │  Mode   │ │   Mode     │
                    │(direct) │ │(parser  │ │(2-layer    │
                    │         │ │+chunk)  │ │ incremental│
                    └────┬────┘ └────┬────┘ └─────┬──────┘
                         │          │             │
                    ┌────▼──────────▼─────────────▼──────┐
                    │         Qdrant Storage              │
                    │   (parent-child tree structure)     │
                    └────────────────┬────────────────────┘
                                    │
                    ┌───────────────▼────────────────────┐
                    │      HierarchicalRetriever          │
                    │  (wave search + dir filter fix)     │
                    └───────────────┬────────────────────┘
                                    │
                    ┌───────────────▼────────────────────┐
                    │      IntentRouter (optimized)       │
                    │  (keywords → multi-query → cache)   │
                    └────────────────────────────────────┘
```

## 4. Detailed Design

### 4.1 IngestModeResolver

Lightweight routing logic added inside `MemoryOrchestrator.add()` and `batch_add()`.

**Resolution order (explicit signals first)**:

| Priority | Signal | Mode |
|----------|--------|------|
| 1 | `meta.ingest_mode = "memory"/"document"/"conversation"` | Forced |
| 2 | `batch_store` call or `source_path`/`scan_meta` present | Document |
| 3 | `session_id` present (via `add_message`) | Conversation |
| 4 | Content contains dialog patterns (`User:...`, `Assistant:...`) | Conversation |
| 5 | Content has heading structure + length > 4000 tokens | Document |
| 6 | Default | Memory |

**Dedup policy**: Unified OFF for all three modes. The `add()` method's `dedup` parameter default changes from `True` to `False`. This is a **behavioral change** — existing callers that relied on automatic dedup will need to pass `dedup=True` explicitly if they still want it.

### 4.2 Conversation Mode — Two-Layer Incremental Chunking

#### 4.2.1 Immediate Layer

On every `add_message` call, `ContextManager._commit()` invokes a new lightweight orchestrator method:

```python
# New method on MemoryOrchestrator (bypasses full add() pipeline)
async def _write_immediate(self, session_id: str, msg_index: int, text: str) -> str:
    """Write a single message for immediate searchability. No LLM, no CortexFS."""
    uri = self._auto_uri(category="events", context_type="memory")
    vector = await self._embed(text)
    record = {
        "id": uri_to_point_id(uri),
        "uri": uri,
        "vector": vector,
        "abstract": text[:500],
        "content": text,
        "parent_uri": f"opencortex://.../{session_id}",
        "is_leaf": True,
        "category": "events",
        "meta": {"layer": "immediate", "msg_index": msg_index, "session_id": session_id},
    }
    await self._storage.upsert("context", [record])
    return uri
```

Key decisions:
- **Bypasses `add()`**: No LLM derivation, no CortexFS dual-write, no dedup check.
- **Direct `_storage.upsert`**: Writes only to Qdrant (no `.abstract.md` / `.overview.md` files).
- **URI generation**: Reuses `_auto_uri` with `category="events"` for consistent URI scheme.
- **CortexFS skipped**: Immediate records are transient — they get replaced by merged chunks, so filesystem persistence is unnecessary.

**Accumulate buffer**: `ContextManager` maintains a per-session `ConversationBuffer`:

```python
@dataclass
class ConversationBuffer:
    messages: List[str]          # Raw message texts
    token_count: int = 0         # Accumulated token estimate
    start_msg_index: int = 0     # First message index in buffer
    immediate_uris: List[str] = field(default_factory=list)  # URIs for deletion at merge
```

Token estimation reuses OpenViking's method: CJK chars * 0.7 + other chars * 0.3.

**Result**: Every individual message is immediately searchable via vector retrieval.

#### 4.2.2 Merge Layer

When the buffer accumulates ~1000 tokens (checked at end of each `_commit()`):

1. **Collect buffer messages**: All messages since last merge.
2. **LLM derivation**: Parallel generate `abstract` + `overview` + `keywords` (reuse existing `_derive_structured` prompt).
3. **Write merged chunk**: Via full `orchestrator.add()` pipeline — new `is_leaf=True` record:
   - `abstract` / `overview` / `keywords` = LLM-generated
   - `content` = concatenated raw dialog text
   - `parent_uri` = session root
   - `meta.layer` = `"merged"`
   - `meta.msg_range` = `[start_idx, end_idx]`
4. **Replace immediate records**: Delete by URI list (`ConversationBuffer.immediate_uris`) — no schema query needed, in-memory tracking is sufficient.
5. **Reset buffer**.

The merge runs as a fire-and-forget `asyncio.create_task` so it does not block the `_commit()` response. Immediate records remain searchable until the merge completes and deletes them.

#### 4.2.3 Session End

On `session_end`:

1. **Flush remaining buffer**: Force merge even if < 1000 tokens.
2. **Create session parent node**: Summarize all merged chunks into one `is_leaf=False` directory record.
3. **Trigger Alpha Pipeline**: Observer.flush → TraceSplitter → TraceStore → Archivist (existing flow).

#### 4.2.4 Data Flow

```
add_message(msg_1)
  → embed(msg_1) → write immediate #1        [searchable]
  → buffer: [msg_1], tokens: 200

add_message(msg_2)
  → embed(msg_2) → write immediate #2        [searchable]
  → buffer: [msg_1, msg_2], tokens: 450

add_message(msg_3)
  → embed(msg_3) → write immediate #3        [searchable]
  → buffer: [msg_1..3], tokens: 1050         [threshold exceeded]
  → Merge:
      LLM derive(msg_1+msg_2+msg_3) → abstract/overview/keywords
      write merged chunk (msg_range: [1,3])
      delete immediate #1, #2, #3
  → buffer reset

add_message(msg_4)
  → embed(msg_4) → write immediate #4        [searchable]
  → buffer: [msg_4], tokens: 180

session_end()
  → flush: merge [msg_4] → merged chunk
  → create session parent (summarize all chunks)
  → Alpha pipeline
```

### 4.3 Document Mode — Multi-Format Hierarchical Parsing

#### 4.3.1 Parser Architecture (Ported from OpenViking)

All parsers follow a single pattern: convert to Markdown → delegate to MarkdownParser for structural splitting.

**Ported parsers (Phase 1 + Code)**:

| Parser | Formats | Dependency | Notes |
|--------|---------|------------|-------|
| MarkdownParser | .md | none | Core: structural split by headings |
| TextParser | .txt | none | Delegates to MarkdownParser |
| WordParser | .docx | `python-docx` | Convert to markdown → delegate |
| ExcelParser | .xlsx/.xls/.xlsm | `openpyxl` | Sheet → markdown table → delegate |
| PowerPointParser | .pptx | `python-pptx` | Slide → markdown → delegate |
| PDFParser | .pdf | `pdfplumber` | Bookmark/font heading detection → markdown → delegate |
| EPubParser | .epub | `ebooklib` (optional) | HTML extraction → markdown → delegate |
| CodeRepositoryParser | git/zip/local dir | git CLI | Directory walk, per-file chunks |

**Deferred (Phase 2+)**: HTMLParser (URL fetching, readabilipy/markdownify/bs4), Media parsers.

#### 4.3.2 Output Format

Parsers do NOT write to VikingFS (OpenViking dependency removed). Instead they return a flat list:

```python
@dataclass
class ParsedChunk:
    content: str           # Raw text content
    title: str             # Section title (empty string if none)
    level: int             # Hierarchy depth (0=root)
    parent_index: int      # Parent chunk index in list (-1=none)
    source_format: str     # Original format (markdown/docx/pdf/code...)
    meta: dict             # Additional metadata (file_path, file_type, etc.)
```

#### 4.3.3 Chunking Parameters (from OpenViking)

| Parameter | Value | Description |
|-----------|-------|-------------|
| MAX_SECTION_SIZE | 1024 tokens | Maximum tokens per chunk |
| MIN_SECTION_TOKENS | 512 tokens | Below this, merge with adjacent section |
| Small document threshold | 4000 tokens | Below this, no chunking (Memory mode) |
| Token estimation | CJK: 0.7 token/char, other: 0.3 token/char | Multilingual-aware |

#### 4.3.4 Splitting Strategy (Priority Order)

1. **Small documents** (< 4000 tokens): No split, store as single Memory record.
2. **Heading-based split**: Detect markdown headings (`#` ~ `######`), split by heading hierarchy recursively.
3. **Small section merge**: Adjacent sections < 512 tokens → merge into one chunk (staying ≤ 1024 tokens).
4. **Large section without subheadings**: > 1024 tokens, no sub-headings → smart split by paragraphs (`\n\n`).
5. **No-heading long text**: Split entirely by paragraphs, each ≤ 1024 tokens.

#### 4.3.5 Code Repository Handling

Code repos do NOT go through MarkdownParser structural splitting. Instead:

```
oc-scan (client-side)
  → scan local directory, read files
  → send {items: [{content, meta.file_path}], source_path, scan_meta}
  → batch_store API

batch_add (server-side, enhanced)
  → detect scan_meta → Document mode
  → build directory tree from meta.file_path relative paths:
      src/                          → is_leaf=False directory node
      src/opencortex/               → is_leaf=False directory node
      src/opencortex/orchestrator.py → is_leaf=True file node
  → large files (> MAX_SECTION_SIZE) → split by function/class (AST if available) or by lines
  → Phase 1: process all leaf nodes concurrently (asyncio.Semaphore(5) for LLM calls)
      → file nodes: LLM derive abstract/overview/keywords from content
  → Phase 2: process directory nodes bottom-up (children must complete first)
      → directory nodes: LLM summarize child file abstracts
```

**Concurrency model**: Leaf nodes have no dependencies and can be processed in parallel with a semaphore (default 5 concurrent LLM calls). Directory nodes depend on child abstracts being available, so they are processed bottom-up after all children complete. Errors are collected per-item and returned in the response (same as current `batch_add` behavior).

**Directory tree example**:

```
project-root (is_leaf=False, summary of entire project)
├── src/ (is_leaf=False, summary of src contents)
│   ├── opencortex/ (is_leaf=False, summary)
│   │   ├── orchestrator.py (is_leaf=True, abstract + content)
│   │   ├── config.py (is_leaf=True)
│   │   └── http/ (is_leaf=False)
│   │       ├── server.py (is_leaf=True)
│   │       └── client.py (is_leaf=True)
├── tests/ (is_leaf=False)
│   └── test_e2e.py (is_leaf=True)
└── README.md (is_leaf=True)
```

#### 4.3.6 OpenViking Dependencies — Decoupling Plan

| OpenViking Dependency | OpenCortex Replacement |
|-----------------------|----------------------|
| `openviking.parse.base` (ParseResult, ResourceNode, NodeType) | New `opencortex.parse.base` with `ParsedChunk` + `format_table_to_markdown` + `lazy_import` |
| `openviking.parse.parsers.base_parser.BaseParser` | New `BaseParser` returning `List[ParsedChunk]` |
| `openviking_cli.utils.config.parser_config.ParserConfig` | Simplified `@dataclass ParserConfig(max_section_size, min_section_tokens)` |
| `openviking_cli.utils.logger` | Standard `logging.getLogger(__name__)` |
| `openviking.storage.viking_fs` | Removed — parsers return chunk lists, no filesystem writes |
| `openviking.parse.parsers.upload_utils` | Keep `should_skip_file`, `should_skip_directory`, `detect_and_convert_encoding` utilities |
| `openviking.parse.parsers.constants` | Copy `IGNORE_DIRS`, `IGNORE_EXTENSIONS`, `CODE_EXTENSIONS`, etc. |
| `openviking_cli.utils.config.get_openviking_config` | Remove — hardcode `github_domains` defaults |
| `openviking.utils.is_github_url` / `parse_code_hosting_url` | Inline simplified versions |

#### 4.3.7 Processing Flow (Complete)

```
store(content, ingest_mode="document", meta={source_path: "report.pdf"})
  → IngestModeResolver → "document"
  → ParserRegistry.get_parser_for_file(".pdf") → PDFParser
  → PDFParser.parse("report.pdf")
    → pdfplumber → markdown string
    → MarkdownParser.split(markdown) → List[ParsedChunk]
  → for each chunk (parallel):
      LLM derive → abstract / overview / keywords
  → write parent node (is_leaf=False, summary of all chunks)
  → write chunk_1..N (is_leaf=True, parent_uri=parent)
```

### 4.4 Memory Mode — Pass-Through

Unchanged from current behavior:

- Single input → single record.
- LLM derivation via `_derive_structured` (abstract + overview + keywords).
- Dedup: OFF (default changed from `True` to `False`).
- No chunking, no tree building.

### 4.5 Retrieval Optimizations

#### 4.5.1 Vectorization Text Expansion

The `Context` class has no `keywords` attribute. Keywords are derived by `_derive_layers()` in the orchestrator and stored as a Qdrant payload field, but never set on the `Context` object. The `Vectorize` object is initialized at construction time with `abstract` only.

**Implementation approach**: Modify the orchestrator's `add()` flow to update the vectorization text after LLM derivation completes but before embedding:

```python
# In orchestrator.add(), after _derive_layers() returns (around line ~739):
abstract, overview, keywords = await self._derive_layers(content, ...)

# Before embed() call (around line ~766), update vectorization text:
vectorize_text = f"{abstract} {keywords}" if keywords else abstract
ctx.vectorize = Vectorize(vectorize_text)

# Then embed() uses the expanded text:
vector = await self._embed(ctx.get_vectorization_text())
```

This keeps `Context` unchanged — the fix is entirely in the orchestrator's `add()` method. No new attributes needed on `Context`.

Keywords provide high-density retrieval signal. Overview excluded to avoid diluting vector semantics.

Applies to all modes (Memory, Document, Conversation) uniformly.

#### 4.5.2 HierarchicalRetriever Directory Filter Fix

**Bug**: Directory nodes (`is_leaf=False`) have `category=""`. When `metadata_filter` includes `category=X`, directory nodes are excluded from wave search, breaking tree traversal entirely.

**Status**: The `dir_friendly` OR-filter wrapping pattern has already been applied at 4 locations in `hierarchical_retriever.py` (frontier batch, compensation query, tiny queries, `_recursive_search`). These fixes need to be **verified** via LoCoMo benchmark re-run.

**Remaining gap**: `_global_vector_search` (line ~294) is a directory-only method (already forces `is_leaf=False`). The dir-friendly OR-wrapper used at the other 4 locations is **wrong** here — it would cause the method to return leaf nodes. Instead, the fix is to strip content-level filters (like `category`) from the metadata_filter at the call site, since directory nodes have `category=""` and never match category filters:

```python
# At the call site (~line 294), pass None for metadata_filter:
# Directory nodes don't carry category metadata, so content-level
# filters are irrelevant. Tenant/user scope filters are already
# applied separately via scope_filter.
global_results = await self._global_vector_search(
    collection=collection,
    query_vector=query_vector,
    sparse_query_vector=sparse_query_vector,
    limit=self.GLOBAL_SEARCH_TOPK,
    filter=None,  # Strip content-level filters for directory search
    text_query=text_query,
)
```

Phase 1 task: verify existing 4 fixes work correctly, apply fix to `_global_vector_search` call site, and re-run LoCoMo benchmark.

#### 4.5.3 Intent Router Optimization

Three-pronged optimization inspired by OpenViking's IntentAnalyzer:

**A. Session-aware routing (from OpenViking)**

The primary performance lever. OpenViking's key insight: only invoke LLM when session context exists.

```
Has session context (summaries + recent messages)?
  ├─ YES → LLM IntentAnalyzer → multi-query
  └─ NO  → Zero-LLM direct query construction
```

For direct API `search()` calls (no session), construct a default `TypedQuery` immediately — zero LLM cost. This alone eliminates the 8-22s latency for the majority of search calls.

**B. Multi-query concurrent retrieval (from OpenViking)**

When LLM analysis IS triggered (session context present), generate multiple `TypedQuery` objects with different angles:

```python
# Current: single query
query → IntentRouter → single search()

# New: multi-query (OpenViking pattern)
query + session_context → IntentAnalyzer → [TypedQuery_1, TypedQuery_2, TypedQuery_3]
  → asyncio.gather(search(tq1), search(tq2), search(tq3))
  → merge + deduplicate results
```

Each TypedQuery can target different `context_type` (memory/resource) and use different query reformulations. This dramatically improves recall for ambiguous queries.

**C. LRU intent cache**

Cache recent intent analysis results (TTL 60s, maxsize 128). Similar queries within a short window reuse the cached QueryPlan without re-invoking LLM.

## 5. Implementation Phases

### Phase 1: Foundation (No Dependencies)

- [ ] IngestModeResolver (routing logic in orchestrator)
- [ ] Vectorization text expansion in orchestrator `add()` flow (set `Vectorize(abstract + keywords)` after `_derive_layers`)
- [ ] Verify existing HierarchicalRetriever directory filter fixes + apply fix to `_global_vector_search`
- [ ] Dedup default to OFF (`add()` parameter default change)

**Verification**: Re-run LoCoMo benchmark — expect non-zero retrieval hits.

### Phase 2: Conversation Mode (Depends on P1)

- [ ] Immediate layer: per-message embed + write in `add_message`
- [ ] Merge layer: token threshold trigger, LLM derive, replace immediate records
- [ ] Session end: flush buffer, create parent node, Alpha pipeline
- [ ] Buffer management in Observer (token counting, merge trigger)

**Verification**: Multi-turn conversation → search for specific message content → hit.

### Phase 3: Document Mode (Depends on P1, parallel with P2)

- [ ] Port parser base classes (`ParsedChunk`, `BaseParser`, `ParserConfig`)
- [ ] Port parsers: Markdown, Text, Word, Excel, PowerPoint, PDF, EPUB
- [ ] Port CodeRepositoryParser + constants + upload_utils
- [ ] `ParserRegistry` with extension-based dispatch
- [ ] `batch_add` enhancement: build directory tree from `meta.file_path`
- [ ] Large file chunking in batch_add
- [ ] Add optional dependencies to `pyproject.toml`: `python-docx`, `openpyxl`, `python-pptx`, `pdfplumber`, `ebooklib`

**Verification**: Ingest a PDF/docx → search for specific section content → hit with correct hierarchy.

### Phase 4: Intent Router Optimization (Depends on P1, parallel with P2/P3)

- [ ] Session-aware routing (skip LLM when no session context — primary performance lever)
- [ ] Multi-query concurrent retrieval (TypedQuery + asyncio.gather)
- [ ] LRU intent cache (TTL 60s, maxsize 128)
- [ ] Prompt slimming (reduce output fields)

**Verification**: Measure average retrieval latency — expect < 2s for non-session queries (zero LLM).

## 6. Key Files

| File | Operation |
|------|-----------|
| `src/opencortex/orchestrator.py` | Add IngestModeResolver, modify `add()`, enhance `batch_add()` |
| `src/opencortex/orchestrator.py` (add flow) | Set `Vectorize(abstract + keywords)` after `_derive_layers()`, before `embed()` |
| `src/opencortex/retrieve/hierarchical_retriever.py` | Fix directory filter (4 locations) |
| `src/opencortex/retrieve/intent_router.py` | Session-aware routing, multi-query, LRU cache |
| `src/opencortex/context/manager.py` | Wire immediate layer in `commit()`, merge trigger |
| `src/opencortex/alpha/observer.py` | Token buffer management, merge trigger callback |
| `src/opencortex/parse/` | **New directory** — ported parsers |
| `src/opencortex/parse/base.py` | New — `ParsedChunk`, `format_table_to_markdown`, `lazy_import` |
| `src/opencortex/parse/registry.py` | New — `ParserRegistry` |
| `src/opencortex/parse/parsers/` | New — markdown, text, word, excel, pptx, pdf, epub, code |
| `src/opencortex/parse/parsers/constants.py` | New — ported from OpenViking |
| `pyproject.toml` | Add optional deps: python-docx, openpyxl, python-pptx, pdfplumber, ebooklib |

## 7. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Merge layer deletes immediate records while search is in progress | Stale results for brief window | Accept eventual consistency; merge is async background task |
| LLM derivation latency blocks merge | Delayed chunk quality upgrade | Merge runs async, immediate records serve until complete |
| Parser port introduces OpenViking coupling | Maintenance burden | Clean decoupling via `ParsedChunk` interface; no VikingFS dependency |
| Large code repos produce thousands of chunks | Qdrant storage pressure, slow batch_add | Concurrency limit on LLM derive (semaphore), progress reporting |
| Multi-query concurrent retrieval increases Qdrant load | Higher QPS on vector store | Qdrant embedded handles well; limit to 3-5 concurrent queries |
| Intent cache returns stale results | Wrong search parameters | Short TTL (60s); cache key includes full query text |

## 8. Success Metrics

1. **LoCoMo QA hit rate**: From 0% to > 50% retrieval hits.
2. **Average retrieval latency**: From 8-22s to < 2s for non-session queries (zero LLM via session-aware routing).
3. **Code search accuracy**: Query "authentication middleware" → returns relevant code file with correct file path.
4. **Hierarchical navigation**: Query "what's in src/opencortex/" → returns directory summary.
5. **Per-message searchability**: Within active session, last message is searchable within 1s of `add_message`.
