# Truncation Elimination Design Spec

> **Goal:** Remove all hard character-level truncation from the data pipeline. Content must be split at paragraph/sentence boundaries. Hash/ID must use full length for new data.

## Scope

Two tracks executed in one plan:

- **Track A** — Content truncation: 6 sites across prompts, orchestrator, trace_splitter, admin_routes, rerank_client
- **Track B** — Hash/ID truncation: 10 sites across orchestrator, uri, semantic_name, cortex_fs, user_id, archivist, trace_splitter

Out of scope: log-only truncation (llm_factory error logs, intent_analyzer debug logs, migration scripts already executed).

## Track A: Content Truncation → Paragraph-Aware Splitting

### New Utility: `smart_truncate`

**File:** `src/opencortex/utils/text.py` (new)

```python
def smart_truncate(text: str, max_chars: int) -> str:
    """Truncate text at the nearest paragraph or sentence boundary.

    Priority: paragraph boundary (\n\n) > line boundary (\n) > sentence boundary (. ! ?)
    Returns text up to max_chars, never cutting mid-paragraph.
    If a single paragraph exceeds max_chars, falls back to sentence boundary.
    If a single sentence exceeds max_chars, returns it whole (never mid-word cut).
    """
```

```python
def smart_split(text: str, max_chars: int) -> list[str]:
    """Split text into chunks, each ≤ max_chars, at paragraph boundaries.

    Each chunk is a complete paragraph or group of paragraphs.
    Used for chunked LLM processing of oversized content.
    """
```

### New Utility: `chunked_llm_derive`

**File:** `src/opencortex/utils/text.py`

```python
async def chunked_llm_derive(
    content: str,
    prompt_builder: Callable[[str], str],
    llm_fn: Callable[[str], Awaitable[str]],
    max_chars_per_chunk: int = 3000,
) -> dict:
    """Split content into chunks, call LLM on each, merge results.

    Returns merged {abstract, overview, keywords}:
    - abstract: from first chunk (most representative)
    - overview: concatenated from all chunks, deduplicated
    - keywords: union of all chunks, deduplicated
    """
```

### Site-by-Site Changes

#### A1. `prompts.py:166` — `build_doc_summarization_prompt`

**Before:** `content[:3000]` hardcoded in prompt template.

**After:** Remove truncation from prompt. The caller (`_generate_abstract_overview`) is responsible for ensuring content fits. If content > 3000 chars, caller uses `chunked_llm_derive` with `build_doc_summarization_prompt` as the prompt builder.

Prompt text changes from `Content (first 3000 chars):` to `Content:`.

#### A2. `prompts.py:191` — `build_layer_derivation_prompt`

**Before:** `content[:4000]` hardcoded in prompt template.

**After:** Remove truncation. Caller (`_derive_layers` in orchestrator) checks content length. If > 4000 chars, uses `chunked_llm_derive` with `build_layer_derivation_prompt` as builder.

Prompt text changes from `Content:` (no label change needed, just remove `[:4000]`).

#### A3. `orchestrator.py:2507-2520` — `_generate_abstract_overview` fallback

**Before:** `content[:500]` as overview when no LLM.

**After:** `smart_truncate(content, 500)` — takes complete paragraphs up to 500 chars. If content is shorter, returns it whole. The function signature and return type remain `(abstract, overview)`.

#### A4. `trace_splitter.py:45` — `_transcript_to_text`

**Before:** `content[:2000] + "... [truncated]"`

**After:** `smart_truncate(content, 2000)`. If truncated, append `f" [... {len(content) - len(truncated)} chars omitted]"` to preserve awareness of omitted content.

#### A5. `admin_routes.py:177` — admin search debug

**Before:** `abstract[:80]`

**After:** Return full `abstract` field. No truncation. The admin UI/client is responsible for display-level truncation if needed.

#### A6. `rerank_client.py:269` — LLM rerank prompt

**Before:** `doc[:500]` per document in rerank prompt.

**After:** `smart_truncate(doc, 500)` — paragraph-aware truncation per document. This keeps LLM prompt size bounded while preserving semantic completeness.

## Track B: Hash/ID → Full Length (New Data Only)

### Backward Compatibility

Old data retains original short-hash URIs. New data uses full-length hashes. All queries are exact URI matches, so mixed lengths coexist without issues. No migration needed.

### Site-by-Site Changes

#### B1. `orchestrator.py:582` — Event memory node ID

**Before:** `uuid4().hex[:12]` (12 chars, 48 bits)

**After:** `uuid4().hex` (32 chars, 128 bits)

#### B2. `orchestrator.py:656` — Source document ID

**Before:** `hashlib.sha256(...).hexdigest()[:16]` (16 chars, 64 bits)

**After:** `hashlib.sha256(...).hexdigest()` (64 chars, 256 bits)

#### B3. `orchestrator.py:658` — Fallback source document ID

**Before:** `uuid4().hex[:16]`

**After:** `uuid4().hex`

#### B4. `orchestrator.py:2541` — Auto-URI fallback node name

**Before:** `uuid4().hex[:12]`

**After:** `uuid4().hex`

#### B5. `uri.py:346` — Semantic node name length cap

**Before:** `safe[:50]` hard character cut.

**After:** `smart_truncate(safe, 80)` — truncate at word boundary. Increase limit to 80 chars (still bounded for filesystem safety). If truncated, no suffix added (hash suffix already provides uniqueness).

Note: `smart_truncate` works on word boundaries here since node names don't have paragraph structure.

#### B6. `uri.py:355` — Fallback temp ID

**Before:** `uuid4().hex[:6]` (6 chars, 24 bits — collision-prone)

**After:** `uuid4().hex[:16]` (16 chars, 64 bits). Full 32 unnecessary for temp fallback.

#### B7. `semantic_name.py:38` — Hash suffix

**Before:** `hashlib.sha256(...).hexdigest()[:8]` (8 chars, 32 bits)

**After:** `hashlib.sha256(...).hexdigest()[:16]` (16 chars, 64 bits). Full 64 makes URIs unnecessarily long.

#### B8. `cortex_fs.py:654` — Filesystem component hash

**Before:** `hashlib.sha256(...).hexdigest()[:8]`

**After:** `hashlib.sha256(...).hexdigest()[:16]`

#### B9. `user_id.py:68` — Space name hash

**Before:** `md5[:8]`

**After:** `hashlib.md5(...).hexdigest()` (full 32 chars). This function has no callers currently but should be correct if activated.

#### B10. `archivist.py:158` — Knowledge ID

**Before:** `uuid4().hex[:12]`

**After:** `uuid4().hex`

#### B11. `trace_splitter.py:141` — Trace ID

**Before:** `uuid4().hex[:12]`

**After:** `uuid4().hex`

## Testing Strategy

### Track A Tests

- `test_smart_truncate`: Paragraph boundary splitting, sentence fallback, single-paragraph case, empty input, content shorter than max
- `test_smart_split`: Multi-chunk splitting, chunk size verification, no content loss
- `test_chunked_llm_derive`: Mock LLM, verify keywords merged, abstract from first chunk, overview concatenated
- Update `test_context_manager` / `test_noise_reduction`: Verify no truncation in stored records for content > threshold
- Update existing prompt tests if any

### Track B Tests

- Update `test_semantic_name.py`: Verify hash suffix length changed from 8 to 16
- Add test: verify `uuid4().hex` (32 chars) used in new event/knowledge/trace IDs
- Add test: verify `smart_truncate` used for URI semantic names instead of hard `[:50]`

## Non-Goals

- Migrating existing stored data to new ID lengths
- Changing log-level truncation (debug messages, error messages)
- Changing migration scripts that have already been executed
- Changing `cortex_fs.py:997` (`parts[0][:26]`) — this is datetime parsing, not content truncation
- Changing `retriever.py:1437` (`parts[:5]`) — this is URI path splitting, not content truncation
