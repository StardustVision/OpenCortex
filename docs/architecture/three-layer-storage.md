# Three-Layer Storage

## Why This Exists

OpenCortex needs fast recall for most queries and full-fidelity content for the few cases that require deep inspection. The three-layer model separates those concerns so search can stay in Qdrant with minimal payloads, while the filesystem keeps canonical content and richer structure.

## Core Model

In the normal (steady-state) model, each record is stored as a CortexFS directory with three files. The exception is conversation immediate-layer records, which are written directly to Qdrant and bypass CortexFS until they are merged.

- `L0` abstract: `.abstract.md`
- `L1` overview: `.overview.md`
- `L2` full content: `content.md`

Qdrant stores vectors plus the payload used at retrieval time (URI, abstract, overview, metadata, keywords, and related fields). `L0` is the default vectorization text; `L1` is the default retrieval detail level; `L2` is fetched only for explicit deep reads or when the planner/router escalates retrieval to `l2`.

## Write Path

Normal writes go through `MemoryOrchestrator.add()`:

1. Resolve ingest mode (`memory`, `document`, `conversation`) via `IngestModeResolver`.
2. If content is present and this is a leaf record, attempt to derive `L0`/`L1`/keywords from `L2` using `_derive_layers()`. This is best-effort and falls back to whatever the caller provided (or empty overview) when no LLM is configured or derivation fails.
3. Embed (default text is `abstract`, optionally `abstract + keywords`).
4. Upsert into Qdrant with the full payload and vector(s).
5. Fire-and-forget `CortexFS.write_context()` to persist `content.md`, `.abstract.md`, and `.overview.md`.

The synchronous path is Qdrant; CortexFS is intentionally asynchronous, so a successful add always makes the record searchable even if filesystem writing lags or fails.

## Read Path

Retrieval is detail-level driven:

- `L0`: abstract only (typically from Qdrant payload, with CortexFS consulted for relation/parent enrichment).
- `L1`: abstract + overview (typically from Qdrant payload, with CortexFS consulted for relation/parent enrichment).
- `L2`: abstract + overview from Qdrant, plus `content.md` loaded from CortexFS.

The default detail level is `L1`, so most queries avoid direct `content.md` reads. `L2` is read on demand when detail is explicitly requested or when the planner/router escalates the detail level to `l2`, and when direct content read endpoints load `content.md`.

## Relationship Between CortexFS and Qdrant

This is a dual-write system:

- **Qdrant** is the fast retrieval surface: vectors and hot payload fields live here.
- **CortexFS** is the canonical content surface: full text, layered summaries, and relation files live here.

Writes are Qdrant-first, CortexFS-after. This keeps retrieval reliable even if filesystem writes are delayed or fail, at the cost of eventual consistency for `L2`.

## Document and Conversation Implications

Document mode parses input into hierarchical chunks (`ParserRegistry`). Each chunk becomes its own record and is stored through the same `add()` flow, so each chunk gets `L0`/`L1` in Qdrant plus `L2` in CortexFS. Document metadata (source doc id/title, section path, chunk role) is stored in the Qdrant payload to support document-scoped retrieval.

Conversation mode uses a two-layer flow:

- **Immediate layer**: each message is embedded and written directly to Qdrant (`meta.layer = "immediate"`). This bypasses LLM derivation and CortexFS entirely for fast, low-latency recall.
- **Merged layer**: after a token threshold, buffered messages are merged and written via `add()` (`meta.layer = "merged"`), which restores the full three-layer write (L0/L1/L2). The immediate records are then deleted.

This means conversations are instantly searchable at `L0`, but `L2` exists only after merge.

## Constraints and Tradeoffs

- Dual-write adds operational complexity and requires accepting eventual consistency between Qdrant and CortexFS.
- Defaulting to `L1` avoids filesystem I/O and token-heavy payloads, but full content requires explicit deep reads or `l2` escalation by the planner/router.
- Document parsing and conversation merging keep the retrieval model uniform, but make the write path more complex and more dependent on background work.

## Current State

Three-layer storage is the core persistence model used by memory, document ingestion, and conversation ingestion. The default retrieval detail level is `L1`, and `L2` is only fetched on demand. The system is optimized for fast Qdrant-first recall with CortexFS as the canonical content substrate rather than a synchronous dependency.
