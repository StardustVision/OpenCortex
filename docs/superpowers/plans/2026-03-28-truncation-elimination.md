# Truncation Elimination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate all hard character-level content truncation (7 sites) and collision-prone hash/ID truncation (8 sites) from the data pipeline, replacing with paragraph-aware splitting and longer IDs.

**Architecture:** Two independent tracks. Track A adds `smart_truncate` / `smart_split` / `chunked_llm_derive` utilities to `src/opencortex/utils/text.py`, then replaces hard `[:N]` cuts at 7 call sites with paragraph-boundary-aware equivalents. Track B extends hash/ID lengths at 8 sites (3 kept unchanged for backward compatibility). Each task is independently testable.

**Tech Stack:** Python 3.10+ async, unittest, existing MarkdownParser chunking as reference pattern.

**Spec:** `docs/superpowers/specs/2026-03-28-truncation-elimination-design.md`

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `src/opencortex/utils/text.py` | `smart_truncate`, `smart_split`, `chunked_llm_derive` | **Create** |
| `src/opencortex/prompts.py` | LLM prompt builders | Modify: remove `[:3000]` and `[:4000]`, add overview compression prompt |
| `src/opencortex/orchestrator.py` | Memory orchestrator | Modify: `_derive_layers`, `_generate_abstract_overview`, `_auto_uri`, `_write_immediate` |
| `src/opencortex/alpha/trace_splitter.py` | Trace splitting | Modify: `_transcript_to_text` |
| `src/opencortex/http/admin_routes.py` | Admin API | Modify: remove `[:80]` |
| `src/opencortex/retrieve/rerank_client.py` | Rerank client | Modify: `_build_rerank_prompt` |
| `src/opencortex/context/manager.py` | Context manager | Modify: `_clamp` |
| `src/opencortex/utils/semantic_name.py` | Semantic node naming | Modify: `max_length`, hash suffix length |
| `src/opencortex/utils/uri.py` | URI utilities | Modify: `sanitize_segment`, `create_temp_uri` |
| `src/opencortex/core/user_id.py` | User ID | Modify: `unique_space_name` |
| `src/opencortex/alpha/archivist.py` | Knowledge extraction | Modify: knowledge ID |
| `tests/test_text_utils.py` | Tests for smart_truncate/split | **Create** |
| `tests/test_semantic_name.py` | Semantic name tests | Modify: update hash length assertions |

---

### Task 1: `smart_truncate` and `smart_split` utilities

**Files:**
- Create: `src/opencortex/utils/text.py`
- Create: `tests/test_text_utils.py`

- [ ] **Step 1: Write failing tests for `smart_truncate`**

```python
# tests/test_text_utils.py
import unittest


class TestSmartTruncate(unittest.TestCase):

    def test_short_text_unchanged(self):
        from opencortex.utils.text import smart_truncate
        self.assertEqual(smart_truncate("hello", 100), "hello")

    def test_empty_text(self):
        from opencortex.utils.text import smart_truncate
        self.assertEqual(smart_truncate("", 100), "")

    def test_truncate_at_paragraph_boundary(self):
        from opencortex.utils.text import smart_truncate
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        result = smart_truncate(text, 40)
        # "First paragraph.\n\nSecond paragraph." = 36 chars, fits
        # Adding "\n\nThird paragraph." = 55 chars, exceeds 40
        self.assertEqual(result, "First paragraph.\n\nSecond paragraph.")

    def test_truncate_at_line_boundary(self):
        from opencortex.utils.text import smart_truncate
        text = "Line one.\nLine two.\nLine three is long."
        result = smart_truncate(text, 25)
        # "Line one.\nLine two." = 20 chars, fits
        self.assertEqual(result, "Line one.\nLine two.")

    def test_truncate_at_sentence_boundary(self):
        from opencortex.utils.text import smart_truncate
        text = "First sentence. Second sentence. Third sentence."
        result = smart_truncate(text, 35)
        # "First sentence. Second sentence." = 32 chars
        self.assertEqual(result, "First sentence. Second sentence.")

    def test_truncate_at_word_boundary(self):
        from opencortex.utils.text import smart_truncate
        text = "one two three four five six seven"
        result = smart_truncate(text, 20)
        # "one two three four " would be 19, but we want word boundary
        self.assertLessEqual(len(result), 20)
        self.assertFalse(result.endswith(" "))

    def test_guarantee_max_chars(self):
        from opencortex.utils.text import smart_truncate
        # Single very long word
        text = "a" * 500
        result = smart_truncate(text, 100)
        self.assertLessEqual(len(result), 100)

    def test_exactly_at_limit(self):
        from opencortex.utils.text import smart_truncate
        text = "Exact."
        result = smart_truncate(text, 6)
        self.assertEqual(result, "Exact.")

    def test_chinese_text(self):
        from opencortex.utils.text import smart_truncate
        text = "第一段话。\n\n第二段话。\n\n第三段话。"
        result = smart_truncate(text, 15)
        self.assertLessEqual(len(result), 15)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python3 -m unittest tests.test_text_utils -v`
Expected: ModuleNotFoundError or ImportError

- [ ] **Step 3: Implement `smart_truncate` and `smart_split`**

```python
# src/opencortex/utils/text.py
"""Paragraph-aware text truncation and splitting utilities."""
import re
from typing import List


def smart_truncate(text: str, max_chars: int) -> str:
    """Truncate text at the nearest semantic boundary.

    Priority: paragraph (\\n\\n) > line (\\n) > sentence (. ! ?) > word > hard cut.
    GUARANTEE: return value is always <= max_chars.
    """
    if len(text) <= max_chars:
        return text

    # Try paragraph boundary
    result = _truncate_at(text, max_chars, "\n\n")
    if result:
        return result

    # Try line boundary
    result = _truncate_at(text, max_chars, "\n")
    if result:
        return result

    # Try sentence boundary
    result = _truncate_at_sentence(text, max_chars)
    if result:
        return result

    # Try word boundary
    result = _truncate_at_word(text, max_chars)
    if result:
        return result

    # Hard cut as absolute last resort
    return text[:max_chars]


def _truncate_at(text: str, max_chars: int, sep: str) -> str:
    """Truncate at the last occurrence of sep within max_chars."""
    truncated = text[:max_chars]
    idx = truncated.rfind(sep)
    if idx > 0:
        return text[:idx]
    return ""


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Truncate at the last sentence-ending punctuation within max_chars."""
    truncated = text[:max_chars]
    # Find last sentence boundary (. ! ? followed by space or end)
    matches = list(re.finditer(r'[.!?。！？]\s', truncated))
    if matches:
        end = matches[-1].end()
        return text[:end].rstrip()
    # Check if ends with sentence punctuation
    match = re.search(r'[.!?。！？]$', truncated)
    if match:
        return truncated
    return ""


def _truncate_at_word(text: str, max_chars: int) -> str:
    """Truncate at the last word boundary within max_chars."""
    truncated = text[:max_chars]
    idx = truncated.rfind(" ")
    if idx > 0:
        return text[:idx]
    return ""


def smart_split(text: str, max_chars: int) -> List[str]:
    """Split text into chunks, each <= max_chars, at paragraph boundaries.

    Each chunk is a complete paragraph or group of paragraphs.
    No content is lost — all chunks concatenated equal the original text
    (modulo separator whitespace).
    """
    if len(text) <= max_chars:
        return [text]

    paragraphs = text.split("\n\n")
    chunks = []
    current = ""

    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para

        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # Single paragraph exceeds max_chars — split by lines
            if len(para) > max_chars:
                line_chunks = _split_by_lines(para, max_chars)
                chunks.extend(line_chunks[:-1])
                current = line_chunks[-1]
            else:
                current = para

    if current:
        chunks.append(current)

    return chunks


def _split_by_lines(text: str, max_chars: int) -> List[str]:
    """Split a single paragraph by line boundaries."""
    lines = text.split("\n")
    chunks = []
    current = ""

    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(line) > max_chars:
                # Hard split at word boundary for very long lines
                chunks.extend(_split_by_words(line, max_chars))
                current = ""
            else:
                current = line

    if current:
        chunks.append(current)

    return chunks if chunks else [text[:max_chars]]


def _split_by_words(text: str, max_chars: int) -> List[str]:
    """Last resort: split by word boundaries."""
    words = text.split(" ")
    chunks = []
    current = ""

    for word in words:
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = word if len(word) <= max_chars else word[:max_chars]

    if current:
        chunks.append(current)

    return chunks if chunks else [text[:max_chars]]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python3 -m unittest tests.test_text_utils -v`
Expected: All tests PASS

- [ ] **Step 5: Add tests for `smart_split`**

Append to `tests/test_text_utils.py`:

```python
class TestSmartSplit(unittest.TestCase):

    def test_short_text_single_chunk(self):
        from opencortex.utils.text import smart_split
        self.assertEqual(smart_split("hello", 100), ["hello"])

    def test_split_at_paragraphs(self):
        from opencortex.utils.text import smart_split
        text = "Para one.\n\nPara two.\n\nPara three."
        chunks = smart_split(text, 20)
        # Each paragraph is <= 20 chars
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 20)
        # No content lost
        self.assertEqual("\n\n".join(chunks), text)

    def test_no_content_loss(self):
        from opencortex.utils.text import smart_split
        text = "A" * 50 + "\n\n" + "B" * 50 + "\n\n" + "C" * 50
        chunks = smart_split(text, 60)
        rejoined = "\n\n".join(chunks)
        self.assertEqual(rejoined, text)

    def test_single_long_paragraph(self):
        from opencortex.utils.text import smart_split
        text = "word " * 100  # 500 chars
        chunks = smart_split(text, 60)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 60)
        self.assertTrue(len(chunks) > 1)

    def test_empty_text(self):
        from opencortex.utils.text import smart_split
        self.assertEqual(smart_split("", 100), [""])
```

- [ ] **Step 6: Run tests**

Run: `uv run python3 -m unittest tests.test_text_utils -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/opencortex/utils/text.py tests/test_text_utils.py
git commit -m "feat: add smart_truncate and smart_split paragraph-aware utilities"
```

---

### Task 2: `chunked_llm_derive` utility and overview compression prompt

**Files:**
- Modify: `src/opencortex/utils/text.py`
- Modify: `src/opencortex/prompts.py`
- Modify: `tests/test_text_utils.py`

- [ ] **Step 1: Write failing test for `chunked_llm_derive`**

Append to `tests/test_text_utils.py`:

```python
import asyncio


class TestChunkedLLMDerive(unittest.TestCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def test_single_chunk_passthrough(self):
        from opencortex.utils.text import chunked_llm_derive

        async def mock_llm(prompt):
            return '{"abstract": "Sum", "overview": "Over", "keywords": ["k1"]}'

        def mock_parse(response):
            import json
            return json.loads(response)

        result = self._run(chunked_llm_derive(
            content="Short content",
            prompt_builder=lambda c: f"Summarize: {c}",
            llm_fn=mock_llm,
            parse_fn=mock_parse,
            max_chars_per_chunk=3000,
        ))
        self.assertEqual(result["abstract"], "Sum")
        self.assertEqual(result["overview"], "Over")
        self.assertEqual(result["keywords"], ["k1"])

    def test_multi_chunk_merges_keywords(self):
        from opencortex.utils.text import chunked_llm_derive

        call_count = 0
        async def mock_llm(prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return '{"abstract": "First", "overview": "Overview 1.", "keywords": ["a", "b"]}'
            elif call_count == 2:
                return '{"abstract": "Second", "overview": "Overview 2.", "keywords": ["b", "c"]}'
            # Compression call for overview
            return '{"abstract": "Compressed", "overview": "Compressed overview.", "keywords": []}'

        def mock_parse(response):
            import json
            return json.loads(response)

        text = "Para one content.\n\n" + "Para two content."
        result = self._run(chunked_llm_derive(
            content=text,
            prompt_builder=lambda c: f"Summarize: {c}",
            llm_fn=mock_llm,
            parse_fn=mock_parse,
            max_chars_per_chunk=20,
        ))
        # Abstract from first chunk
        self.assertEqual(result["abstract"], "First")
        # Keywords deduplicated union
        self.assertIn("a", result["keywords"])
        self.assertIn("b", result["keywords"])
        self.assertIn("c", result["keywords"])

    def test_abstract_overview_merge_policy(self):
        from opencortex.utils.text import chunked_llm_derive

        async def mock_llm(prompt):
            if "compress" in prompt.lower() or "Compress" in prompt:
                return '{"abstract": "compressed abs", "overview": "compressed over"}'
            return '{"abstract": "abs", "overview": "over"}'

        def mock_parse(response):
            import json
            return json.loads(response)

        text = "Chunk one.\n\nChunk two."
        result = self._run(chunked_llm_derive(
            content=text,
            prompt_builder=lambda c: f"Summarize: {c}",
            llm_fn=mock_llm,
            parse_fn=mock_parse,
            merge_policy="abstract_overview",
            max_chars_per_chunk=15,
        ))
        self.assertIn("abstract", result)
        self.assertIn("overview", result)
        self.assertNotIn("keywords", result)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python3 -m unittest tests.test_text_utils.TestChunkedLLMDerive -v`
Expected: ImportError

- [ ] **Step 3: Add overview compression prompt to `prompts.py`**

Add at the end of `src/opencortex/prompts.py`:

```python
# =========================================================================
# 7. Overview Compression  (for chunked derivation)
# =========================================================================

def build_overview_compression_prompt(overviews: str) -> str:
    """Compress multiple chunk overviews into a single 3-8 sentence overview.

    Args:
        overviews: Concatenated overviews from multiple chunks.
    """
    return f"""Compress the following multiple overview sections into a single coherent overview.

Source overviews:
{overviews}

Rules:
- Produce 3-8 sentences that cover the key facts from ALL source overviews
- Do NOT repeat information
- Maintain factual accuracy
- Return ONLY the compressed overview text, no JSON wrapping"""
```

- [ ] **Step 4: Implement `chunked_llm_derive` in `text.py`**

Add to `src/opencortex/utils/text.py`:

```python
from typing import Any, Awaitable, Callable, Dict, List


async def chunked_llm_derive(
    content: str,
    prompt_builder: Callable[[str], str],
    llm_fn: Callable[[str], Awaitable[str]],
    parse_fn: Callable[[str], dict],
    merge_policy: str = "default",
    max_chars_per_chunk: int = 3000,
) -> Dict[str, Any]:
    """Split content into chunks, call LLM on each, merge results.

    prompt_builder: Takes content string, returns prompt. Wrap multi-arg
                    builders with lambda/partial before passing.
    parse_fn:       Parses LLM response into dict.
    merge_policy:   "default" — merges {abstract, overview, keywords}
                    "abstract_overview" — merges {abstract, overview} only
    """
    chunks = smart_split(content, max_chars_per_chunk)

    if len(chunks) == 1:
        prompt = prompt_builder(chunks[0])
        response = await llm_fn(prompt)
        result = parse_fn(response)
        if merge_policy == "abstract_overview":
            return {
                "abstract": result.get("abstract", ""),
                "overview": result.get("overview", ""),
            }
        return result

    # Process each chunk
    chunk_results = []
    for chunk in chunks:
        prompt = prompt_builder(chunk)
        try:
            response = await llm_fn(prompt)
            parsed = parse_fn(response)
            if isinstance(parsed, dict):
                chunk_results.append(parsed)
        except Exception:
            pass

    if not chunk_results:
        return {"abstract": "", "overview": ""}

    # Merge: abstract from first chunk
    abstract = chunk_results[0].get("abstract", "")

    # Merge: overview via second-pass compression
    all_overviews = "\n\n".join(
        r.get("overview", "") for r in chunk_results if r.get("overview")
    )
    if all_overviews and len(chunk_results) > 1:
        try:
            from opencortex.prompts import build_overview_compression_prompt
            compress_prompt = build_overview_compression_prompt(all_overviews)
            compress_response = await llm_fn(compress_prompt)
            overview = compress_response.strip()
        except Exception:
            overview = chunk_results[0].get("overview", "")
    else:
        overview = all_overviews

    if merge_policy == "abstract_overview":
        return {"abstract": abstract, "overview": overview}

    # Merge: keywords deduplicated union
    all_keywords = []
    seen = set()
    for r in chunk_results:
        kw = r.get("keywords", [])
        if isinstance(kw, list):
            for k in kw:
                k_lower = str(k).lower()
                if k_lower not in seen:
                    seen.add(k_lower)
                    all_keywords.append(k)

    return {
        "abstract": abstract,
        "overview": overview,
        "keywords": all_keywords,
    }
```

- [ ] **Step 5: Run tests**

Run: `uv run python3 -m unittest tests.test_text_utils -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/utils/text.py src/opencortex/prompts.py tests/test_text_utils.py
git commit -m "feat: add chunked_llm_derive with overview compression"
```

---

### Task 3: Apply Track A — prompts.py and orchestrator LLM paths (A1, A2, A3)

**Files:**
- Modify: `src/opencortex/prompts.py:155-204`
- Modify: `src/opencortex/orchestrator.py:784,2503-2520`

- [ ] **Step 1: Remove truncation from `build_doc_summarization_prompt`**

In `src/opencortex/prompts.py`, change line 160-166:

```python
# Before
    """Build prompt for document abstract + overview generation.

    Args:
        file_path: Document file path.
        content: Full document content (truncated to 3000 chars inside the prompt).
    """
    return f"""Summarize this document for a memory system.

File: {file_path}
Content (first 3000 chars):
{content[:3000]}
```

```python
# After
    """Build prompt for document abstract + overview generation.

    Args:
        file_path: Document file path.
        content: Document content (caller handles chunking for oversized content).
    """
    return f"""Summarize this document for a memory system.

File: {file_path}
Content:
{content}
```

- [ ] **Step 2: Remove truncation from `build_layer_derivation_prompt`**

In `src/opencortex/prompts.py`, change lines 178-191:

```python
# Before
        content: Full content text (truncated to 4000 chars in the prompt).
# ...
{content[:4000]}
```

```python
# After
        content: Content text (caller handles chunking for oversized content).
# ...
{content}
```

- [ ] **Step 3: Update `_generate_abstract_overview` in orchestrator**

In `src/opencortex/orchestrator.py`, replace the method at line 2503:

```python
    async def _generate_abstract_overview(self, content: str, file_path: str) -> tuple:
        """Use LLM to generate abstract (L0) and overview (L1) from content."""
        from opencortex.utils.text import smart_truncate

        if not self._llm_completion:
            return file_path, smart_truncate(content, 500)

        if len(content) > 3000:
            from opencortex.utils.text import chunked_llm_derive
            from opencortex.utils.json_parse import parse_json_from_response
            try:
                result = await chunked_llm_derive(
                    content=content,
                    prompt_builder=lambda chunk: build_doc_summarization_prompt(file_path, chunk),
                    llm_fn=self._llm_completion,
                    parse_fn=parse_json_from_response,
                    merge_policy="abstract_overview",
                    max_chars_per_chunk=3000,
                )
                return result.get("abstract", file_path), result.get("overview", smart_truncate(content, 500))
            except Exception:
                pass
            return file_path, smart_truncate(content, 500)

        prompt = build_doc_summarization_prompt(file_path, content)
        try:
            response = await self._llm_completion(prompt)
            from opencortex.utils.json_parse import parse_json_from_response
            data = parse_json_from_response(response)
            if isinstance(data, dict):
                return data.get("abstract", file_path), data.get("overview", smart_truncate(content, 500))
        except Exception:
            pass

        return file_path, smart_truncate(content, 500)
```

- [ ] **Step 4: Update `_derive_layers` to use chunked derivation for long content**

In `src/opencortex/orchestrator.py`, around line 783-804, wrap the LLM call:

```python
        if self._llm_completion:
            if len(content) > 4000:
                from opencortex.utils.text import chunked_llm_derive
                from opencortex.utils.json_parse import parse_json_from_response
                try:
                    result = await chunked_llm_derive(
                        content=content,
                        prompt_builder=lambda chunk: build_layer_derivation_prompt(chunk, user_abstract),
                        llm_fn=self._llm_completion,
                        parse_fn=parse_json_from_response,
                        max_chars_per_chunk=4000,
                    )
                    keywords_list = result.get("keywords", [])
                    if isinstance(keywords_list, list):
                        keywords = ", ".join(str(k) for k in keywords_list if k)
                    else:
                        keywords = str(keywords_list)
                    return {
                        "abstract": user_abstract or result.get("abstract", ""),
                        "overview": user_overview or result.get("overview", ""),
                        "keywords": keywords,
                    }
                except Exception as e:
                    logger.warning("[Orchestrator] _derive_layers chunked LLM failed: %s", e)

            prompt = build_layer_derivation_prompt(content, user_abstract)
            # ... existing single-call path unchanged ...
```

- [ ] **Step 5: Run existing tests**

Run: `uv run python3 -m unittest tests.test_context_manager tests.test_noise_reduction tests.test_alpha_config -v`
Expected: All PASS (no behavior change for short content)

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/prompts.py src/opencortex/orchestrator.py
git commit -m "feat: paragraph-aware truncation in prompts and orchestrator (A1-A3)"
```

---

### Task 4: Apply Track A — trace_splitter, admin_routes, rerank, _clamp (A4-A7)

**Files:**
- Modify: `src/opencortex/alpha/trace_splitter.py:44-45`
- Modify: `src/opencortex/http/admin_routes.py:177`
- Modify: `src/opencortex/retrieve/rerank_client.py:268-269`
- Modify: `src/opencortex/context/manager.py:735-739`

- [ ] **Step 1: Fix trace_splitter.py (A4)**

In `src/opencortex/alpha/trace_splitter.py`, replace lines 43-45:

```python
# Before
            # Truncate very long messages for the prompt
            if len(content) > 2000:
                content = content[:2000] + "... [truncated]"
```

```python
# After
            if len(content) > 2000:
                from opencortex.utils.text import smart_truncate
                truncated = smart_truncate(content, 2000)
                content = f"{truncated} [...{len(content) - len(truncated)} chars omitted]"
```

- [ ] **Step 2: Fix admin_routes.py (A5)**

In `src/opencortex/http/admin_routes.py`, change line 177:

```python
# Before
            "abstract": r.get("abstract", "")[:80],
```

```python
# After
            "abstract": r.get("abstract", ""),
```

- [ ] **Step 3: Fix rerank_client.py (A6)**

In `src/opencortex/retrieve/rerank_client.py`, change lines 268-270:

```python
# Before
        docs_text = "\n".join(
            f"[{i}] {doc[:500]}" for i, doc in enumerate(documents)
        )
```

```python
# After
        from opencortex.utils.text import smart_truncate
        docs_text = "\n".join(
            f"[{i}] {smart_truncate(doc, 500)}" for i, doc in enumerate(documents)
        )
```

- [ ] **Step 4: Fix context/manager.py _clamp (A7)**

In `src/opencortex/context/manager.py`, replace the `_clamp` method at line 735:

```python
# Before
    def _clamp(self, text: str) -> str:
        """Hard limit per-item content to max_content_chars."""
        if len(text) <= self._max_content_chars:
            return text
        return text[: self._max_content_chars] + "...[truncated]"
```

```python
# After
    def _clamp(self, text: str) -> str:
        """Limit per-item content to max_content_chars at paragraph boundary."""
        if len(text) <= self._max_content_chars:
            return text
        from opencortex.utils.text import smart_truncate
        truncated = smart_truncate(text, self._max_content_chars)
        omitted = len(text) - len(truncated)
        return f"{truncated} [...{omitted} chars omitted]"
```

- [ ] **Step 5: Run existing tests**

Run: `uv run python3 -m unittest tests.test_context_manager tests.test_noise_reduction -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/alpha/trace_splitter.py src/opencortex/http/admin_routes.py \
        src/opencortex/retrieve/rerank_client.py src/opencortex/context/manager.py
git commit -m "feat: paragraph-aware truncation in trace_splitter, admin, rerank, clamp (A4-A7)"
```

---

### Task 5: Apply Track B — Hash/ID length changes

**Files:**
- Modify: `src/opencortex/orchestrator.py:582,2541`
- Modify: `src/opencortex/utils/uri.py:346,355`
- Modify: `src/opencortex/utils/semantic_name.py:12,37-39`
- Modify: `src/opencortex/core/user_id.py:64-68`
- Modify: `src/opencortex/alpha/archivist.py:158`
- Modify: `src/opencortex/alpha/trace_splitter.py:141`

- [ ] **Step 1: Write failing tests for new ID lengths**

Append to `tests/test_text_utils.py`:

```python
class TestIDLengths(unittest.TestCase):
    """Verify ID/hash lengths after truncation elimination."""

    def test_semantic_name_hash_suffix_16(self):
        from opencortex.utils.semantic_name import semantic_node_name
        long_text = "a" * 200
        result = semantic_node_name(long_text, max_length=80)
        self.assertLessEqual(len(result), 80)
        # Should end with _<16-char-hash>
        self.assertRegex(result, r"_[a-f0-9]{16}$")

    def test_semantic_name_default_max_80(self):
        from opencortex.utils.semantic_name import semantic_node_name
        long_text = "a" * 200
        result = semantic_node_name(long_text)
        self.assertLessEqual(len(result), 80)

    def test_sanitize_segment_max_80(self):
        from opencortex.utils.uri import CortexURI
        long_text = "word_" * 30  # 150 chars
        result = CortexURI.sanitize_segment(long_text)
        self.assertLessEqual(len(result), 80)
        # Should truncate at underscore boundary
        self.assertFalse(result.endswith("_"))

    def test_sanitize_segment_underscore_boundary(self):
        from opencortex.utils.uri import CortexURI
        text = "one_two_three_four_five_six_seven_eight_nine_ten_eleven_twelve_thirteen_fourteen_fifteen_sixteen"
        result = CortexURI.sanitize_segment(text)
        self.assertLessEqual(len(result), 80)
        # Should not end mid-word
        parts = result.split("_")
        for part in parts:
            self.assertTrue(len(part) > 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python3 -m unittest tests.test_text_utils.TestIDLengths -v`
Expected: AssertionError (hash is 8 chars, not 16; max_length is 50, not 80)

- [ ] **Step 3: Update `semantic_name.py`**

Replace `src/opencortex/utils/semantic_name.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""
Semantic node naming for OpenCortex URIs.

Generates filesystem-safe semantic names from text.
Produces deterministic, human-readable URI segments from arbitrary text.
"""
import hashlib
import re


def semantic_node_name(text: str, max_length: int = 80) -> str:
    """Sanitize text for use as a URI node name.

    Preserves letters, digits, CJK characters, underscores, and hyphens.
    Replaces all other characters with underscores. Merges consecutive
    underscores. If the result exceeds *max_length*, truncates at underscore
    boundary and appends a SHA-256 hash suffix for uniqueness.

    Args:
        text: Input text (e.g., abstract, filename).
        max_length: Maximum output length (default 80).

    Returns:
        URI-safe, deterministic node name. Returns ``"unnamed"`` for empty input.
    """
    safe = re.sub(
        r"[^\w\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af\u3400-\u4dbf-]",
        "_",
        text,
    )
    safe = re.sub(r"_+", "_", safe).strip("_")

    if not safe:
        return "unnamed"

    if len(safe) > max_length:
        hash_suffix = hashlib.sha256(text.encode()).hexdigest()[:16]
        # Truncate at underscore boundary
        prefix_limit = max_length - 17  # _<16-char-hash>
        prefix = safe[:prefix_limit]
        last_underscore = prefix.rfind("_")
        if last_underscore > 0:
            prefix = prefix[:last_underscore]
        safe = f"{prefix}_{hash_suffix}"

    return safe
```

- [ ] **Step 4: Update `uri.py` `sanitize_segment`**

In `src/opencortex/utils/uri.py`, change line 346:

```python
# Before
        safe = safe.strip("_")[:50]
```

```python
# After
        safe = safe.strip("_")
        if len(safe) > 80:
            # Truncate at underscore boundary
            prefix = safe[:80]
            last_underscore = prefix.rfind("_")
            if last_underscore > 0:
                safe = prefix[:last_underscore]
            else:
                safe = prefix
```

- [ ] **Step 5: Update `uri.py` `create_temp_uri`**

In `src/opencortex/utils/uri.py`, change line 355:

```python
# Before
        temp_id = uuid.uuid4().hex[:6]
```

```python
# After
        temp_id = uuid.uuid4().hex[:16]
```

- [ ] **Step 6: Update `orchestrator.py` ID lengths**

In `src/opencortex/orchestrator.py`:

Line 582:
```python
# Before
        nid = uuid4().hex[:12]
# After
        nid = uuid4().hex
```

Line 2541:
```python
# Before
        node_name = semantic_node_name(abstract) if abstract else uuid4().hex[:12]
# After
        node_name = semantic_node_name(abstract) if abstract else uuid4().hex
```

- [ ] **Step 7: Update `user_id.py`**

In `src/opencortex/core/user_id.py`, change lines 64-68:

```python
# Before
    def unique_space_name(self, short: bool = True) -> str:
        """Anonymized space name: {user_id}_{md5[:8]}."""
        h = hashlib.md5((self._user_id + self._agent_id).encode()).hexdigest()
        if short:
            return f"{self._user_id}_{h[:8]}"
        return f"{self._user_id}_{h}"
```

```python
# After
    def unique_space_name(self, short: bool = True) -> str:
        """Anonymized space name: {user_id}_{md5}."""
        h = hashlib.md5((self._user_id + self._agent_id).encode()).hexdigest()
        return f"{self._user_id}_{h}"
```

- [ ] **Step 8: Update `archivist.py`**

In `src/opencortex/alpha/archivist.py`, change line 158:

```python
# Before
                knowledge_id=f"k-{uuid.uuid4().hex[:12]}",
# After
                knowledge_id=f"k-{uuid.uuid4().hex}",
```

- [ ] **Step 9: Update `trace_splitter.py`**

In `src/opencortex/alpha/trace_splitter.py`, change line 141:

```python
# Before
            trace_id = f"tr-{uuid.uuid4().hex[:12]}"
# After
            trace_id = f"tr-{uuid.uuid4().hex}"
```

- [ ] **Step 10: Update `tests/test_semantic_name.py`**

Change the hash assertion in `test_truncation_with_hash`:

```python
# Before
        self.assertLessEqual(len(result), 50)
        # Should end with _<8-char-hash>
        self.assertRegex(result, r"_[a-f0-9]{8}$")
```

```python
# After
        self.assertLessEqual(len(result), 80)
        # Should end with _<16-char-hash>
        self.assertRegex(result, r"_[a-f0-9]{16}$")
```

Also update `test_truncation_with_hash` to use `max_length=80`:

```python
        result = semantic_node_name(long_text, max_length=80)
```

- [ ] **Step 11: Run all tests**

Run: `uv run python3 -m unittest tests.test_text_utils tests.test_semantic_name tests.test_context_manager tests.test_noise_reduction tests.test_cortexfs_async tests.test_alpha_config -v`
Expected: All PASS

- [ ] **Step 12: Commit**

```bash
git add src/opencortex/utils/semantic_name.py src/opencortex/utils/uri.py \
        src/opencortex/orchestrator.py src/opencortex/core/user_id.py \
        src/opencortex/alpha/archivist.py src/opencortex/alpha/trace_splitter.py \
        tests/test_semantic_name.py tests/test_text_utils.py
git commit -m "feat: extend hash/ID lengths, eliminate collision-prone truncation (B1-B11)"
```

---

### Task 6: Final verification and cleanup

**Files:**
- All modified files

- [ ] **Step 1: Run full test suite**

Run: `uv run python3 -m unittest discover -s tests -v`
Expected: All tests PASS (except known flaky: test_09_feedback_batch, test_11_protect_slows_decay)

- [ ] **Step 2: Grep for remaining hard truncation patterns**

Run: `grep -rn '\[:[:digit:]*\]' src/opencortex/ --include='*.py' | grep -v 'test\|log\|migration\|__pycache__'`
Verify: No remaining data-pipeline truncation sites (only log/display/datetime/URI-path patterns should remain).

- [ ] **Step 3: Commit any fixes**

```bash
git add -A
git commit -m "chore: final verification for truncation elimination"
```

Plan complete and saved to `docs/superpowers/plans/2026-03-28-truncation-elimination.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
