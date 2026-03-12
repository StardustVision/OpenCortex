# Ingestion & Retrieval Pipeline Optimization — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three-mode smart ingestion (Memory/Document/Conversation), port OpenViking parsers writing to CortexFS, and optimize the IntentRouter with session-aware routing and multi-query concurrency.

**Architecture:** IngestModeResolver routes content to Memory (pass-through), Document (parser + chunking + hierarchy), or Conversation (two-layer incremental) modes. Parsers ported from OpenViking return `List[ParsedChunk]`; orchestrator writes chunks to CortexFS + Qdrant. IntentRouter gains session-aware fast-path (zero LLM when no session context) and multi-query concurrent retrieval.

**Tech Stack:** Python 3.10+ async, Qdrant embedded, CortexFS three-layer filesystem, OpenViking parser patterns (python-docx, openpyxl, python-pptx, pdfplumber, ebooklib as optional deps)

**Spec:** `docs/superpowers/specs/2026-03-12-ingestion-retrieval-optimization-design.md`

---

## Chunk 1: Foundation (Phase 1)

### Task 1: Vectorization Text Expansion

Expand the vectorization text from `abstract`-only to `abstract + keywords` so that keyword-rich content has higher retrieval density.

**Files:**
- Modify: `src/opencortex/orchestrator.py:724-768` (add() method, between _derive_layers and embed)
- Test: `tests/test_vectorization_expansion.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vectorization_expansion.py
"""Test that vectorization text includes keywords after LLM derivation."""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.core.context import Context


class TestVectorizationExpansion(unittest.TestCase):
    """Verify add() uses abstract+keywords for embedding, not abstract alone."""

    def test_vectorize_text_includes_keywords(self):
        """After _derive_layers, embed() should receive 'abstract keywords'."""
        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.config import CortexConfig, init_config
        from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
        from opencortex.http.request_context import set_request_identity, reset_request_identity

        # Track what text was passed to embed()
        embedded_texts = []

        class SpyEmbedder(DenseEmbedderBase):
            def __init__(self):
                super().__init__(model_name="spy")
            def embed(self, text: str) -> EmbedResult:
                embedded_texts.append(text)
                return EmbedResult(dense_vector=[0.1, 0.2, 0.3, 0.4])
            def get_dimension(self) -> int:
                return 4

        async def mock_llm(prompt: str) -> str:
            # _derive_layers joins list into comma-separated string
            return '{"abstract": "test abstract", "overview": "test overview", "keywords": ["auth", "login", "JWT"]}'

        async def run():
            import tempfile
            tmpdir = tempfile.mkdtemp()
            try:
                cfg = CortexConfig(data_root=tmpdir)
                init_config(cfg)
                orch = MemoryOrchestrator(
                    config=cfg,
                    embedder=SpyEmbedder(),
                    llm_completion=mock_llm,
                )
                await orch.init()
                tokens = set_request_identity("test-tenant", "test-user")
                try:
                    ctx = await orch.add(
                        abstract="",
                        content="Full content about authentication using JWT tokens for login flow.",
                        category="documents",
                        dedup=False,
                    )
                    # The embedded text should contain keywords (comma-separated string from _derive_layers)
                    self.assertEqual(len(embedded_texts), 1)
                    self.assertIn("auth", embedded_texts[0])
                    self.assertIn("login", embedded_texts[0])
                    self.assertIn("JWT", embedded_texts[0])
                    self.assertIn("test abstract", embedded_texts[0])
                finally:
                    reset_request_identity(tokens)
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_vectorization_expansion.py -v`
Expected: FAIL — embedded text currently contains only abstract, not keywords.

- [ ] **Step 3: Implement vectorization text expansion**

In `src/opencortex/orchestrator.py`, after `_derive_layers()` returns and before `embed()`, update the context's vectorization text:

```python
# After line 739 (keywords = layers["keywords"]), add:

        # Expand vectorization text: abstract + keywords for higher retrieval density
        vectorize_text = f"{abstract} {keywords}" if keywords else abstract
        # This will be used by ctx.get_vectorization_text() via Vectorize
```

Then at line ~746 where Context is constructed, the `abstract` field already gets the derived value. After ctx is built (after line 758), add:

```python
        # Override vectorization with expanded text (abstract + keywords)
        if keywords:
            from opencortex.core.context import Vectorize
            ctx.vectorize = Vectorize(f"{abstract} {keywords}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_vectorization_expansion.py -v`
Expected: PASS

- [ ] **Step 5: Run existing tests to verify no regressions**

Run: `uv run python -m unittest tests.test_e2e_phase1 tests.test_write_dedup -v`
Expected: All existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_vectorization_expansion.py
git commit -m "feat: expand vectorization text to include keywords for higher retrieval density"
```

---

### Task 2: Dedup Default Change

Change `add()` dedup parameter default from `True` to `False`. This is a behavioral change — all three ingestion modes use dedup=OFF by default.

**Files:**
- Modify: `src/opencortex/orchestrator.py:674` (add() signature)
- Test: `tests/test_write_dedup.py` (verify existing dedup tests still work with explicit `dedup=True`)

- [ ] **Step 1: Check existing dedup tests**

Read `tests/test_write_dedup.py` to understand which tests rely on `dedup=True` being the default. Any test calling `add()` without explicit `dedup=True` that expects dedup behavior needs updating.

- [ ] **Step 2: Change the default**

In `src/opencortex/orchestrator.py:674`, change:
```python
    dedup: bool = True,
```
to:
```python
    dedup: bool = False,
```

- [ ] **Step 3: Update tests that relied on implicit dedup=True**

Any test in `test_write_dedup.py` that calls `add()` without `dedup=True` but expects dedup behavior → add explicit `dedup=True`.

- [ ] **Step 4: Run dedup tests**

Run: `uv run python -m unittest tests.test_write_dedup -v`
Expected: All pass.

- [ ] **Step 5: Run full E2E tests**

Run: `uv run python -m unittest tests.test_e2e_phase1 -v`
Expected: All pass. (E2E tests use explicit `dedup=False` in batch_add, so unaffected.)

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_write_dedup.py
git commit -m "feat: change add() dedup default from True to False (all modes dedup OFF)"
```

---

### Task 3: HierarchicalRetriever _global_vector_search Fix

Fix `_global_vector_search` to strip content-level metadata filters (like `category`) that exclude directory nodes with `category=""`.

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py:294-301` (call site)
- Test: `tests/test_global_search_dir_filter.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_global_search_dir_filter.py
"""Test that _global_vector_search finds directory nodes regardless of category filter."""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever


class TestGlobalSearchDirFilter(unittest.TestCase):
    def test_global_search_strips_metadata_filter(self):
        """_global_vector_search should be called with filter=None, not the content-level filter."""

        captured_args = {}

        async def mock_global_search(collection, query_vector, sparse_query_vector,
                                     limit, filter=None, text_query=""):
            captured_args["filter"] = filter
            return []

        storage = MagicMock()
        storage.search = AsyncMock(return_value=[])
        retriever = HierarchicalRetriever(storage=storage, embedder=None)

        # Patch _global_vector_search to capture its arguments
        retriever._global_vector_search = mock_global_search

        # Also need to provide a query_vector for the method to be called
        from opencortex.retrieve.types import ContextType, TypedQuery
        query = TypedQuery(query="test", context_type=ContextType.ANY, intent="quick_lookup")

        async def run():
            metadata_filter = {
                "op": "and",
                "conds": [
                    {"op": "must", "field": "category", "conds": ["documents"]},
                ]
            }
            try:
                # Force a query_vector so global search is actually called
                retriever._embed_query = AsyncMock(return_value=([0.1, 0.2], {}))
                await retriever.retrieve(query, metadata_filter=metadata_filter)
            except Exception:
                pass

            # _global_vector_search should have been called with filter=None
            self.assertIn("filter", captured_args)
            self.assertIsNone(captured_args["filter"],
                "Global vector search should strip content-level metadata_filter")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_global_search_dir_filter.py -v`
Expected: FAIL — currently passes `final_metadata_filter` which includes category.

- [ ] **Step 3: Fix the call site**

In `src/opencortex/retrieve/hierarchical_retriever.py`, at line ~294, change:
```python
        global_results = await self._global_vector_search(
            collection=collection,
            query_vector=query_vector,
            sparse_query_vector=sparse_query_vector,
            limit=self.GLOBAL_SEARCH_TOPK,
            filter=final_metadata_filter,
            text_query=text_query,
        )
```
to:
```python
        # Directory nodes have category="" so content-level filters (like
        # category=X) would exclude them. Strip metadata_filter for directory
        # search — tenant/user scope is already enforced by scope_filter.
        global_results = await self._global_vector_search(
            collection=collection,
            query_vector=query_vector,
            sparse_query_vector=sparse_query_vector,
            limit=self.GLOBAL_SEARCH_TOPK,
            filter=None,
            text_query=text_query,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_global_search_dir_filter.py -v`
Expected: PASS

- [ ] **Step 5: Run existing retrieval tests**

Run: `uv run python -m unittest tests.test_frontier_search -v`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py tests/test_global_search_dir_filter.py
git commit -m "fix: strip metadata_filter from _global_vector_search to find directory nodes"
```

---

### Task 4: IngestModeResolver

Add lightweight routing logic to determine ingestion mode (memory/document/conversation) from input signals.

**Files:**
- Create: `src/opencortex/ingest/__init__.py`
- Create: `src/opencortex/ingest/resolver.py`
- Test: `tests/test_ingest_resolver.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ingest_resolver.py
"""Test IngestModeResolver routing logic."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.ingest.resolver import IngestModeResolver


class TestIngestModeResolver(unittest.TestCase):

    def test_explicit_mode_wins(self):
        """meta.ingest_mode overrides everything."""
        r = IngestModeResolver.resolve(
            content="short text",
            meta={"ingest_mode": "document"},
        )
        self.assertEqual(r, "document")

    def test_source_path_implies_document(self):
        """source_path present → document mode."""
        r = IngestModeResolver.resolve(
            content="some content",
            source_path="/tmp/report.pdf",
        )
        self.assertEqual(r, "document")

    def test_scan_meta_implies_document(self):
        """scan_meta present → document mode."""
        r = IngestModeResolver.resolve(
            content="code content",
            scan_meta={"has_git": True},
        )
        self.assertEqual(r, "document")

    def test_session_id_implies_conversation(self):
        """session_id present → conversation mode."""
        r = IngestModeResolver.resolve(
            content="User: Hello\nAssistant: Hi",
            session_id="sess-123",
        )
        self.assertEqual(r, "conversation")

    def test_dialog_pattern_implies_conversation(self):
        """Dialog patterns in content → conversation mode."""
        r = IngestModeResolver.resolve(
            content="User: What is the weather?\nAssistant: It's sunny today.\nUser: Thanks!",
        )
        self.assertEqual(r, "conversation")

    def test_long_headed_content_implies_document(self):
        """Long content with markdown headings → document mode."""
        content = "# Introduction\n" + "x " * 3000 + "\n## Methods\n" + "y " * 2000
        r = IngestModeResolver.resolve(content=content)
        self.assertEqual(r, "document")

    def test_short_content_defaults_memory(self):
        """Short plain text → memory mode."""
        r = IngestModeResolver.resolve(content="The user prefers dark mode.")
        self.assertEqual(r, "memory")

    def test_empty_content_defaults_memory(self):
        """Empty content → memory mode."""
        r = IngestModeResolver.resolve(content="")
        self.assertEqual(r, "memory")

    def test_explicit_memory_overrides_patterns(self):
        """Explicit memory mode overrides dialog patterns."""
        r = IngestModeResolver.resolve(
            content="User: Hello\nAssistant: Hi",
            meta={"ingest_mode": "memory"},
        )
        self.assertEqual(r, "memory")

    def test_batch_store_implies_document(self):
        """is_batch=True → document mode."""
        r = IngestModeResolver.resolve(
            content="file content",
            is_batch=True,
        )
        self.assertEqual(r, "document")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_ingest_resolver.py -v`
Expected: FAIL — module `opencortex.ingest.resolver` does not exist.

- [ ] **Step 3: Create the resolver module**

Create `src/opencortex/ingest/__init__.py` (empty file).

Create `src/opencortex/ingest/resolver.py`:

```python
"""IngestModeResolver — route content to memory/document/conversation mode."""

import re

# Dialog patterns: "User:", "Assistant:", "Human:", "AI:"
_DIALOG_RE = re.compile(
    r"^(User|Assistant|Human|AI|System)\s*:", re.MULTILINE | re.IGNORECASE
)
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_SMALL_DOC_THRESHOLD = 4000  # tokens (estimated)


def _estimate_tokens(text: str) -> int:
    """Estimate token count (CJK chars * 0.7 + other * 0.3)."""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cjk
    return int(cjk * 0.7 + other * 0.3)


class IngestModeResolver:
    """Determine ingestion mode from input signals.

    Resolution order (explicit first):
    1. meta.ingest_mode (forced)
    2. batch_store / source_path / scan_meta → document
    3. session_id → conversation
    4. Dialog patterns in content → conversation
    5. Headings + length > 4000 tokens → document
    6. Default → memory
    """

    @staticmethod
    def resolve(
        content: str = "",
        *,
        meta: dict | None = None,
        source_path: str = "",
        scan_meta: dict | None = None,
        session_id: str = "",
        is_batch: bool = False,
    ) -> str:
        meta = meta or {}

        # Priority 1: explicit mode
        explicit = meta.get("ingest_mode", "")
        if explicit in ("memory", "document", "conversation"):
            return explicit

        # Priority 2: batch / source_path / scan_meta → document
        if is_batch or source_path or scan_meta:
            return "document"

        # Priority 3: session_id → conversation
        if session_id:
            return "conversation"

        # Priority 4: dialog patterns
        if content and len(_DIALOG_RE.findall(content)) >= 2:
            return "conversation"

        # Priority 5: headings + length
        if content and _HEADING_RE.search(content):
            if _estimate_tokens(content) > _SMALL_DOC_THRESHOLD:
                return "document"

        # Priority 6: default
        return "memory"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_ingest_resolver.py -v`
Expected: PASS

**Note:** IngestModeResolver is wired into orchestrator `add()` in Task 9 (Chunk 3). This task creates the standalone module only.

**Note:** `_estimate_tokens()` uses the same CJK formula as MarkdownParser (Task 6). Both should import from `opencortex.parse.base` once Task 5 is complete — refactor during Task 6 to add `estimate_tokens()` to `parse/base.py` and reuse here.

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/ingest/__init__.py src/opencortex/ingest/resolver.py tests/test_ingest_resolver.py
git commit -m "feat: add IngestModeResolver for three-mode smart ingestion routing"
```

---

## Chunk 2: Document Mode — Parser Infrastructure (Phase 3a)

### Task 5: Parser Base Classes

Port the parser infrastructure from OpenViking: `ParsedChunk` dataclass, `BaseParser` abstract class, `ParserConfig`, utility functions.

**Files:**
- Create: `src/opencortex/parse/__init__.py`
- Create: `src/opencortex/parse/base.py`
- Create: `src/opencortex/parse/parsers/__init__.py`
- Create: `src/opencortex/parse/parsers/base_parser.py`
- Create: `src/opencortex/parse/parsers/constants.py`
- Create: `src/opencortex/parse/parsers/upload_utils.py`
- Test: `tests/test_parse_base.py`

**Reference:** OpenViking files at `/Users/hugo/CodeSpace/Work/OpenViking/openviking/parse/`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_parse_base.py
"""Test parser base classes and utilities."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.parse.base import ParsedChunk, format_table_to_markdown, lazy_import, ParserConfig


class TestParsedChunk(unittest.TestCase):
    def test_create_chunk(self):
        chunk = ParsedChunk(
            content="Hello world",
            title="Intro",
            level=1,
            parent_index=-1,
            source_format="markdown",
            meta={},
        )
        self.assertEqual(chunk.content, "Hello world")
        self.assertEqual(chunk.title, "Intro")
        self.assertEqual(chunk.level, 1)
        self.assertEqual(chunk.parent_index, -1)

    def test_chunk_defaults(self):
        chunk = ParsedChunk(content="text", title="", level=0, parent_index=-1, source_format="text")
        self.assertEqual(chunk.meta, {})


class TestFormatTable(unittest.TestCase):
    def test_simple_table(self):
        rows = [["Name", "Age"], ["Alice", "30"], ["Bob", "25"]]
        result = format_table_to_markdown(rows)
        self.assertIn("| Name", result)
        self.assertIn("| ---", result)
        self.assertIn("| Alice", result)

    def test_empty_table(self):
        self.assertEqual(format_table_to_markdown([]), "")


class TestParserConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = ParserConfig()
        self.assertEqual(cfg.max_section_size, 1024)
        self.assertEqual(cfg.min_section_tokens, 512)

    def test_custom(self):
        cfg = ParserConfig(max_section_size=2048, min_section_tokens=256)
        self.assertEqual(cfg.max_section_size, 2048)


class TestLazyImport(unittest.TestCase):
    def test_existing_module(self):
        mod = lazy_import("os")
        self.assertTrue(hasattr(mod, "path"))

    def test_missing_module(self):
        with self.assertRaises(ImportError):
            lazy_import("nonexistent_module_xyz_12345")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_parse_base.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Create parse base module**

Create `src/opencortex/parse/__init__.py` (empty).
Create `src/opencortex/parse/parsers/__init__.py` (empty).

Create `src/opencortex/parse/base.py`:
```python
"""Base types and utilities for the OpenCortex parser subsystem."""

import importlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ParsedChunk:
    """Output unit from any parser.

    Represents a single chunk of content with hierarchy metadata.
    The orchestrator writes each chunk to CortexFS (L2) + Qdrant.
    """

    content: str
    title: str
    level: int
    parent_index: int
    source_format: str
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParserConfig:
    """Configuration for parser chunking behavior."""

    max_section_size: int = 1024    # max tokens per chunk
    min_section_tokens: int = 512   # below this, merge with adjacent


def format_table_to_markdown(rows: List[List[str]], has_header: bool = True) -> str:
    """Format table data as a Markdown table."""
    if not rows:
        return ""
    col_count = max(len(row) for row in rows)
    col_widths = [0] * col_count
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))
    lines = []
    for row_idx, row in enumerate(rows):
        padded = list(row) + [""] * (col_count - len(row))
        cells = [str(cell).ljust(col_widths[i]) for i, cell in enumerate(padded)]
        lines.append("| " + " | ".join(cells) + " |")
        if row_idx == 0 and has_header and len(rows) > 1:
            separator = ["-" * w for w in col_widths]
            lines.append("| " + " | ".join(separator) + " |")
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """Estimate token count (CJK chars * 0.7 + other * 0.3)."""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cjk
    return int(cjk * 0.7 + other * 0.3)


def lazy_import(module_name: str, package_name: Optional[str] = None) -> Any:
    """Import a module lazily, raising ImportError with install hint if missing."""
    try:
        return importlib.import_module(module_name)
    except ImportError:
        pkg = package_name or module_name
        raise ImportError(f"Module '{module_name}' not available. Install: pip install {pkg}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_parse_base.py -v`
Expected: PASS

- [ ] **Step 5: Create parser base class and utility files**

Create `src/opencortex/parse/parsers/base_parser.py`:
```python
"""Abstract base class for document parsers."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Union

from opencortex.parse.base import ParsedChunk, ParserConfig


class BaseParser(ABC):
    """Abstract parser that converts documents into ParsedChunk lists."""

    @abstractmethod
    async def parse(self, source: Union[str, Path], **kwargs) -> List[ParsedChunk]:
        """Parse a document from file path or content string."""
        ...

    @abstractmethod
    async def parse_content(self, content: str, source_path: Optional[str] = None, **kwargs) -> List[ParsedChunk]:
        """Parse document content directly."""
        ...

    @property
    @abstractmethod
    def supported_extensions(self) -> List[str]:
        """List of supported file extensions."""
        ...

    def can_parse(self, path: Union[str, Path]) -> bool:
        """Check if this parser can handle the given file."""
        return Path(path).suffix.lower() in self.supported_extensions

    def _read_file(self, path: Union[str, Path]) -> str:
        """Read file content with encoding detection."""
        path = Path(path)
        for encoding in ["utf-8", "utf-8-sig", "latin-1", "cp1252"]:
            try:
                return path.read_text(encoding=encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError(f"Unable to decode file: {path}")
```

Port `src/opencortex/parse/parsers/constants.py` from OpenViking's `openviking/parse/parsers/constants.py` (copy IGNORE_DIRS, IGNORE_EXTENSIONS, CODE_EXTENSIONS, DOCUMENTATION_EXTENSIONS, TEXT_ENCODINGS).

Port `src/opencortex/parse/parsers/upload_utils.py` from OpenViking's `openviking/parse/parsers/upload_utils.py` (copy should_skip_file, should_skip_directory, detect_and_convert_encoding, is_text_file — remove VikingFS references).

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/parse/ tests/test_parse_base.py
git commit -m "feat: add parser base classes (ParsedChunk, BaseParser, ParserConfig, utilities)"
```

---

### Task 6: MarkdownParser Port

Port the core MarkdownParser from OpenViking. This is the foundation — all other parsers convert to Markdown then delegate here.

**Files:**
- Create: `src/opencortex/parse/parsers/markdown.py`
- Test: `tests/test_parse_markdown.py`
- Reference: `/Users/hugo/CodeSpace/Work/OpenViking/openviking/parse/parsers/markdown.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_parse_markdown.py
"""Test MarkdownParser chunking and hierarchy."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.parse.parsers.markdown import MarkdownParser


class TestMarkdownParser(unittest.TestCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def test_small_doc_single_chunk(self):
        """Documents < 4000 tokens → single chunk, no splitting."""
        parser = MarkdownParser()
        content = "# Title\n\nShort document content."
        chunks = self._run(parser.parse_content(content))
        self.assertEqual(len(chunks), 1)
        self.assertIn("Short document content", chunks[0].content)

    def test_heading_split(self):
        """Large doc with headings → split by heading hierarchy."""
        parser = MarkdownParser()
        section_text = "word " * 600  # ~600 tokens each
        content = f"# Introduction\n\n{section_text}\n\n# Methods\n\n{section_text}\n\n# Results\n\n{section_text}"
        chunks = self._run(parser.parse_content(content))
        self.assertGreater(len(chunks), 1)
        titles = [c.title for c in chunks if c.title]
        self.assertIn("Introduction", titles)
        self.assertIn("Methods", titles)

    def test_parent_index_hierarchy(self):
        """Chunks have correct parent_index for hierarchy."""
        parser = MarkdownParser()
        long = "content " * 400
        content = f"# Top\n\n{long}\n\n## Sub1\n\n{long}\n\n## Sub2\n\n{long}"
        chunks = self._run(parser.parse_content(content))
        # At least one chunk should have parent_index pointing to another
        has_child = any(c.parent_index >= 0 for c in chunks)
        self.assertTrue(has_child, "Should have parent-child relationships")

    def test_small_sections_merged(self):
        """Adjacent small sections (< min_tokens) should be merged."""
        parser = MarkdownParser()
        # Create many tiny sections that should be merged
        sections = "\n\n".join(f"# Section {i}\n\nTiny." for i in range(10))
        # Pad overall to exceed small doc threshold
        padding = "word " * 2500
        content = f"{sections}\n\n# Big Section\n\n{padding}"
        chunks = self._run(parser.parse_content(content))
        # Should have fewer chunks than 11 due to merging
        self.assertLess(len(chunks), 11)

    def test_source_format(self):
        """All chunks should have source_format='markdown'."""
        parser = MarkdownParser()
        long = "content " * 2500
        content = f"# A\n\n{long}\n\n# B\n\n{long}"
        chunks = self._run(parser.parse_content(content))
        for c in chunks:
            self.assertEqual(c.source_format, "markdown")

    def test_parse_file(self):
        """Parse from file path."""
        import tempfile
        parser = MarkdownParser()
        long = "content " * 2500
        content = f"# Hello\n\n{long}\n\n# World\n\n{long}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            f.flush()
            chunks = self._run(parser.parse(f.name))
        self.assertGreater(len(chunks), 0)
        os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_parse_markdown.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Port MarkdownParser**

**Prerequisite:** OpenViking source must be accessible at `/Users/hugo/CodeSpace/Work/OpenViking/`. The MarkdownParser is ~500 lines and requires careful adaptation — read the full source before porting.

Port from `/Users/hugo/CodeSpace/Work/OpenViking/openviking/parse/parsers/markdown.py`. Key adaptations:
- Return `List[ParsedChunk]` instead of writing to VikingFS
- Replace `ParseResult`/`ResourceNode`/`NodeType` with `ParsedChunk`
- Replace `get_viking_fs()`/`create_temp_uri()` — no filesystem writes during parsing
- Replace `openviking_cli.utils.logger` with `logging.getLogger(__name__)`
- Replace `ParserConfig` import with `opencortex.parse.base.ParserConfig`
- Keep all chunking logic intact: heading detection, section merge, smart split, token estimation

Key methods to port:
- `_find_headings()` — regex heading detection
- `_estimate_token_count()` — CJK 0.7 + other 0.3
- `_parse_and_create_structure()` → adapt to return `List[ParsedChunk]`
- `_process_sections_with_merge()` — small section merge logic
- `_smart_split_content()` — paragraph-based splitting for oversized sections
- `_extract_frontmatter()` — YAML frontmatter extraction

The parser builds chunks in-memory and returns them. The orchestrator is responsible for writing to CortexFS.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_parse_markdown.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/parse/parsers/markdown.py tests/test_parse_markdown.py
git commit -m "feat: port MarkdownParser from OpenViking with ParsedChunk output"
```

---

### Task 7: Delegate Parsers (Text, Word, Excel, PowerPoint, PDF, EPUB)

Port the remaining 6 parsers. Each converts to Markdown then delegates to MarkdownParser.

**Files:**
- Create: `src/opencortex/parse/parsers/text.py`
- Create: `src/opencortex/parse/parsers/word.py`
- Create: `src/opencortex/parse/parsers/excel.py`
- Create: `src/opencortex/parse/parsers/powerpoint.py`
- Create: `src/opencortex/parse/parsers/pdf.py`
- Create: `src/opencortex/parse/parsers/epub.py`
- Test: `tests/test_parse_delegates.py`
- Reference: `/Users/hugo/CodeSpace/Work/OpenViking/openviking/parse/parsers/`

- [ ] **Step 1: Write tests for TextParser and each delegate**

```python
# tests/test_parse_delegates.py
"""Test delegate parsers (Text, Word, Excel, PowerPoint, PDF, EPUB)."""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.parse.parsers.text import TextParser


class TestTextParser(unittest.TestCase):
    def test_parse_plain_text(self):
        parser = TextParser()
        content = "Just plain text content.\n\nAnother paragraph."
        chunks = asyncio.run(parser.parse_content(content))
        self.assertGreater(len(chunks), 0)
        self.assertEqual(chunks[0].source_format, "text")

    def test_supported_extensions(self):
        parser = TextParser()
        self.assertIn(".txt", parser.supported_extensions)

    def test_parse_file(self):
        parser = TextParser()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Hello world.")
            f.flush()
            chunks = asyncio.run(parser.parse(f.name))
        self.assertGreater(len(chunks), 0)
        os.unlink(f.name)


class TestWordParser(unittest.TestCase):
    def test_supported_extensions(self):
        from opencortex.parse.parsers.word import WordParser
        parser = WordParser()
        self.assertIn(".docx", parser.supported_extensions)


class TestExcelParser(unittest.TestCase):
    def test_supported_extensions(self):
        from opencortex.parse.parsers.excel import ExcelParser
        parser = ExcelParser()
        self.assertIn(".xlsx", parser.supported_extensions)


class TestPowerPointParser(unittest.TestCase):
    def test_supported_extensions(self):
        from opencortex.parse.parsers.powerpoint import PowerPointParser
        parser = PowerPointParser()
        self.assertIn(".pptx", parser.supported_extensions)


class TestPDFParser(unittest.TestCase):
    def test_supported_extensions(self):
        from opencortex.parse.parsers.pdf import PDFParser
        parser = PDFParser()
        self.assertIn(".pdf", parser.supported_extensions)


class TestEPubParser(unittest.TestCase):
    def test_supported_extensions(self):
        from opencortex.parse.parsers.epub import EPubParser
        parser = EPubParser()
        self.assertIn(".epub", parser.supported_extensions)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_parse_delegates.py -v`
Expected: FAIL — modules do not exist.

- [ ] **Step 3: Port each parser**

Port each parser from OpenViking. Key adaptations for ALL:
- Return `List[ParsedChunk]` instead of `ParseResult`
- Convert to markdown → delegate to `MarkdownParser.parse_content()`
- Remove all VikingFS references
- Use `lazy_import()` for optional deps (python-docx, openpyxl, etc.)
- Use `logging.getLogger(__name__)` instead of OpenViking logger

**TextParser** (`text.py`): Simplest — delegate directly to MarkdownParser.
**WordParser** (`word.py`): `python-docx` → markdown → MarkdownParser.
**ExcelParser** (`excel.py`): `openpyxl` → markdown tables → MarkdownParser.
**PowerPointParser** (`powerpoint.py`): `python-pptx` → markdown → MarkdownParser.
**PDFParser** (`pdf.py`): `pdfplumber` (local only, no MinerU API) → markdown → MarkdownParser.
**EPubParser** (`epub.py`): `ebooklib`/zipfile → HTML → markdown → MarkdownParser.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_parse_delegates.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/parse/parsers/*.py tests/test_parse_delegates.py
git commit -m "feat: port delegate parsers (text, word, excel, pptx, pdf, epub) from OpenViking"
```

---

### Task 8: ParserRegistry and Optional Dependencies

Create the parser registry for extension-based dispatch and add optional dependencies to pyproject.toml.

**Files:**
- Create: `src/opencortex/parse/registry.py`
- Modify: `pyproject.toml` (add optional deps group)
- Test: `tests/test_parse_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_parse_registry.py
"""Test ParserRegistry extension-based dispatch."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.parse.registry import ParserRegistry


class TestParserRegistry(unittest.TestCase):
    def test_markdown_dispatch(self):
        registry = ParserRegistry()
        parser = registry.get_parser_for_file("report.md")
        self.assertIsNotNone(parser)

    def test_txt_dispatch(self):
        registry = ParserRegistry()
        parser = registry.get_parser_for_file("readme.txt")
        self.assertIsNotNone(parser)

    def test_pdf_dispatch(self):
        """PDF parser available only if pdfplumber installed."""
        registry = ParserRegistry()
        parser = registry.get_parser_for_file("document.pdf")
        # May be None if pdfplumber not installed — just verify no crash
        if parser:
            self.assertIn(".pdf", parser.supported_extensions)

    def test_docx_dispatch(self):
        """Word parser available only if python-docx installed."""
        registry = ParserRegistry()
        parser = registry.get_parser_for_file("report.docx")
        if parser:
            self.assertIn(".docx", parser.supported_extensions)

    def test_xlsx_dispatch(self):
        """Excel parser available only if openpyxl installed."""
        registry = ParserRegistry()
        parser = registry.get_parser_for_file("data.xlsx")
        if parser:
            self.assertIn(".xlsx", parser.supported_extensions)

    def test_unknown_returns_none(self):
        registry = ParserRegistry()
        parser = registry.get_parser_for_file("image.png")
        self.assertIsNone(parser)

    def test_list_extensions(self):
        registry = ParserRegistry()
        exts = registry.list_supported_extensions()
        self.assertIn(".md", exts)
        self.assertIn(".txt", exts)

    def test_parse_markdown_content(self):
        """End-to-end: registry.parse() on markdown content."""
        registry = ParserRegistry()
        chunks = asyncio.run(
            registry.parse_content("# Hello\n\nWorld", source_format="markdown")
        )
        self.assertGreater(len(chunks), 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_parse_registry.py -v`
Expected: FAIL

- [ ] **Step 3: Create ParserRegistry**

```python
# src/opencortex/parse/registry.py
"""Parser registry for extension-based dispatch."""

import logging
from pathlib import Path
from typing import Dict, List, Optional, Union

from opencortex.parse.base import ParsedChunk
from opencortex.parse.parsers.base_parser import BaseParser

logger = logging.getLogger(__name__)


class ParserRegistry:
    """Registry that maps file extensions to parser instances."""

    def __init__(self):
        self._parsers: Dict[str, BaseParser] = {}
        self._extension_map: Dict[str, str] = {}
        self._register_defaults()

    def _register_defaults(self):
        from opencortex.parse.parsers.text import TextParser
        from opencortex.parse.parsers.markdown import MarkdownParser
        self.register("text", TextParser())
        self.register("markdown", MarkdownParser())

        # Optional parsers — register if dependencies available
        for name, cls_path in [
            ("word", "opencortex.parse.parsers.word.WordParser"),
            ("excel", "opencortex.parse.parsers.excel.ExcelParser"),
            ("powerpoint", "opencortex.parse.parsers.powerpoint.PowerPointParser"),
            ("pdf", "opencortex.parse.parsers.pdf.PDFParser"),
            ("epub", "opencortex.parse.parsers.epub.EPubParser"),
        ]:
            try:
                module_name, class_name = cls_path.rsplit(".", 1)
                import importlib
                mod = importlib.import_module(module_name)
                cls = getattr(mod, class_name)
                self.register(name, cls())
            except (ImportError, AttributeError) as e:
                logger.debug(f"Optional parser '{name}' not available: {e}")

    def register(self, name: str, parser: BaseParser) -> None:
        self._parsers[name] = parser
        for ext in parser.supported_extensions:
            self._extension_map[ext.lower()] = name

    def get_parser_for_file(self, path: Union[str, Path]) -> Optional[BaseParser]:
        ext = Path(path).suffix.lower()
        name = self._extension_map.get(ext)
        return self._parsers.get(name) if name else None

    async def parse_content(
        self, content: str, source_format: str = "text", **kwargs
    ) -> List[ParsedChunk]:
        """Parse content string using appropriate parser."""
        # Map format to parser name
        format_map = {"markdown": "markdown", "md": "markdown", "text": "text", "txt": "text"}
        parser_name = format_map.get(source_format, "text")
        parser = self._parsers.get(parser_name, self._parsers.get("text"))
        if parser:
            return await parser.parse_content(content, **kwargs)
        return []

    def list_supported_extensions(self) -> List[str]:
        return list(self._extension_map.keys())
```

- [ ] **Step 4: Add optional dependencies to pyproject.toml**

Add an optional deps group:
```toml
[project.optional-dependencies]
parsers = [
    "python-docx>=1.0",
    "openpyxl>=3.1",
    "python-pptx>=0.6",
    "pdfplumber>=0.10",
    "ebooklib>=0.18",
]
```

- [ ] **Step 5: Run tests**

Run: `uv run python -m pytest tests/test_parse_registry.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/parse/registry.py pyproject.toml tests/test_parse_registry.py
git commit -m "feat: add ParserRegistry with extension dispatch + optional parser deps"
```

---

## Chunk 3: Document Mode Integration + Code Repository (Phase 3b)

### Task 9: Wire Document Mode into Orchestrator

Connect IngestModeResolver + ParserRegistry into the orchestrator's `add()` and `batch_add()` methods. Document mode: parse → chunks → write each chunk to CortexFS + Qdrant with parent-child hierarchy.

**Files:**
- Modify: `src/opencortex/orchestrator.py` (add() around line 706, batch_add() around line 1897)
- Test: `tests/test_document_mode.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_document_mode.py
"""Test Document mode end-to-end: content → parse → chunks → CortexFS + Qdrant."""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.config import CortexConfig, init_config
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.http.request_context import set_request_identity, reset_request_identity
from opencortex.orchestrator import MemoryOrchestrator


class MockEmbedder(DenseEmbedderBase):
    def __init__(self):
        super().__init__(model_name="mock")
    def embed(self, text):
        return EmbedResult(dense_vector=[0.1, 0.2, 0.3, 0.4])
    def get_dimension(self):
        return 4


class TestDocumentMode(unittest.TestCase):
    def test_large_markdown_produces_multiple_records(self):
        """Large markdown with headings → multiple Qdrant records with hierarchy."""
        async def mock_llm(prompt):
            return '{"abstract": "chunk summary", "overview": "chunk detail", "keywords": ["test"]}'

        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                cfg = CortexConfig(data_root=tmpdir)
                init_config(cfg)
                orch = MemoryOrchestrator(config=cfg, embedder=MockEmbedder(), llm_completion=mock_llm)
                await orch.init()
                tokens = set_request_identity("t1", "u1")
                try:
                    section = "word " * 600
                    content = f"# Intro\n\n{section}\n\n# Methods\n\n{section}\n\n# Results\n\n{section}"
                    result = await orch.add(
                        abstract="",
                        content=content,
                        meta={"ingest_mode": "document"},
                        category="documents",
                        context_type="resource",
                    )
                    # Should have created multiple records (parent + children)
                    # The result context should be the parent node
                    self.assertIsNotNone(result)
                    self.assertIsNotNone(result.uri)
                finally:
                    reset_request_identity(tokens)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())

    def test_small_doc_goes_to_memory(self):
        """Small doc < 4000 tokens → single record (memory mode)."""
        async def mock_llm(prompt):
            return '{"abstract": "small doc", "overview": "detail", "keywords": ["small"]}'

        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                cfg = CortexConfig(data_root=tmpdir)
                init_config(cfg)
                orch = MemoryOrchestrator(config=cfg, embedder=MockEmbedder(), llm_completion=mock_llm)
                await orch.init()
                tokens = set_request_identity("t1", "u1")
                try:
                    result = await orch.add(
                        abstract="",
                        content="# Small Doc\n\nJust a few paragraphs.",
                        meta={"ingest_mode": "document"},
                        category="documents",
                        context_type="resource",
                    )
                    self.assertIsNotNone(result)
                finally:
                    reset_request_identity(tokens)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_document_mode.py -v`
Expected: FAIL — add() doesn't handle ingest_mode=document yet.

- [ ] **Step 3: Wire IngestModeResolver into add()**

In `src/opencortex/orchestrator.py`, at the beginning of `add()` (after line 706), add resolver call:

```python
        # Determine ingestion mode
        from opencortex.ingest.resolver import IngestModeResolver
        ingest_mode = IngestModeResolver.resolve(
            content=content,
            meta=meta,
            source_path=(meta or {}).get("source_path", ""),
            session_id=session_id or "",
        )

        # Document mode: parse → chunks → write each with hierarchy
        if ingest_mode == "document" and content and is_leaf:
            return await self._add_document(
                content=content,
                abstract=abstract,
                overview=overview,
                category=category,
                parent_uri=parent_uri,
                context_type=context_type or "resource",
                meta=meta,
                session_id=session_id,
                source_path=(meta or {}).get("source_path", ""),
            )
```

Add new method `_add_document()`:
```python
    async def _add_document(self, content, abstract, overview, category, parent_uri,
                            context_type, meta, session_id, source_path):
        """Document mode: parse content into chunks, write each to CortexFS + Qdrant."""
        from opencortex.parse.registry import ParserRegistry

        registry = ParserRegistry()
        if source_path:
            parser = registry.get_parser_for_file(source_path)
        else:
            parser = None

        if parser:
            chunks = await parser.parse_content(content, source_path=source_path)
        else:
            chunks = await registry.parse_content(content, source_format="markdown")

        # Small doc (single chunk or no chunks) → fall through to memory mode
        if len(chunks) <= 1:
            single_content = chunks[0].content if chunks else content
            return await self.add(
                abstract=abstract,
                content=single_content,
                category=category,
                parent_uri=parent_uri,
                context_type=context_type,
                meta={**(meta or {}), "ingest_mode": "memory"},  # prevent re-entry
                session_id=session_id,
            )

        # Multi-chunk: create parent + children
        doc_title = chunks[0].title or Path(source_path).stem if source_path else "Document"

        # Create parent (directory) node
        parent_ctx = await self.add(
            abstract=doc_title,
            content="",
            category=category,
            parent_uri=parent_uri,
            is_leaf=False,
            context_type=context_type,
            meta={**(meta or {}), "ingest_mode": "memory"},
            session_id=session_id,
        )
        doc_parent_uri = parent_ctx.uri

        # Process chunks concurrently with semaphore
        sem = asyncio.Semaphore(5)
        results = []

        async def process_chunk(chunk, idx):
            async with sem:
                chunk_parent = doc_parent_uri
                if chunk.parent_index >= 0 and chunk.parent_index < len(results):
                    parent_result = results[chunk.parent_index]
                    if parent_result and not parent_result.is_leaf:
                        chunk_parent = parent_result.uri

                return await self.add(
                    abstract="",
                    content=chunk.content,
                    category=category,
                    parent_uri=chunk_parent,
                    context_type=context_type,
                    meta={**(meta or {}), "ingest_mode": "memory", "chunk_index": idx},
                    session_id=session_id,
                )

        # Process in order to maintain parent_index references
        for idx, chunk in enumerate(chunks):
            ctx = await process_chunk(chunk, idx)
            results.append(ctx)

        return parent_ctx
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_document_mode.py -v`
Expected: PASS

- [ ] **Step 5: Run E2E regression**

Run: `uv run python -m unittest tests.test_e2e_phase1 -v`
Expected: All pass (existing add() calls don't trigger document mode).

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_document_mode.py
git commit -m "feat: wire Document mode into orchestrator add() with parser integration"
```

---

### Task 10: Enhanced batch_add with Directory Tree Building

Enhance `batch_add()` to detect code repository scans (via `scan_meta`) and build directory hierarchy from `meta.file_path`.

**Files:**
- Modify: `src/opencortex/orchestrator.py:1897-1947` (batch_add method)
- Test: `tests/test_batch_add_hierarchy.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_batch_add_hierarchy.py
"""Test batch_add directory tree building from file paths."""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.config import CortexConfig, init_config
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.http.request_context import set_request_identity, reset_request_identity
from opencortex.orchestrator import MemoryOrchestrator


class MockEmbedder(DenseEmbedderBase):
    def __init__(self):
        super().__init__(model_name="mock")
    def embed(self, text):
        return EmbedResult(dense_vector=[0.1, 0.2, 0.3, 0.4])
    def get_dimension(self):
        return 4


class TestBatchAddHierarchy(unittest.TestCase):
    def test_scan_meta_builds_directory_tree(self):
        """batch_add with scan_meta builds parent-child hierarchy from file_path."""
        call_count = {"n": 0}

        async def mock_llm(prompt):
            call_count["n"] += 1
            return '{"abstract": "file summary", "overview": "details", "keywords": ["code"]}'

        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                cfg = CortexConfig(data_root=tmpdir)
                init_config(cfg)
                orch = MemoryOrchestrator(config=cfg, embedder=MockEmbedder(), llm_completion=mock_llm)
                await orch.init()
                tokens = set_request_identity("t1", "u1")
                try:
                    items = [
                        {"content": "def main(): pass", "meta": {"file_path": "src/main.py", "file_type": ".py"}},
                        {"content": "import os", "meta": {"file_path": "src/utils.py", "file_type": ".py"}},
                        {"content": "# Tests\ntest code", "meta": {"file_path": "tests/test_main.py", "file_type": ".py"}},
                    ]
                    result = await orch.batch_add(
                        items=items,
                        source_path="/project",
                        scan_meta={"has_git": True, "project_id": "myproject"},
                    )
                    self.assertEqual(result["status"], "ok")
                    self.assertEqual(result["imported"], 3)
                    # Should have created directory nodes + leaf nodes
                    self.assertGreater(len(result["uris"]), 3, "Should include directory URIs")
                finally:
                    reset_request_identity(tokens)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_batch_add_hierarchy.py -v`
Expected: FAIL — current batch_add doesn't build directory tree.

- [ ] **Step 3: Enhance batch_add with tree building**

In `src/opencortex/orchestrator.py`, modify `batch_add()` to:
1. When `scan_meta` present, build a directory tree from `meta.file_path` values
2. Phase 1: Process leaf nodes (files) concurrently with Semaphore(5)
3. Phase 2: Process directory nodes bottom-up (LLM summarize child abstracts)

Key implementation:
```python
    async def batch_add(self, items, source_path="", scan_meta=None):
        self._ensure_init()
        # If scan_meta present → hierarchical tree building
        if scan_meta:
            return await self._batch_add_hierarchical(items, source_path, scan_meta)
        # Otherwise: existing flat batch logic
        # ... (keep existing code)
```

New method `_batch_add_hierarchical()`:
- Build directory tree dict from `meta.file_path` paths
- Create directory nodes (`is_leaf=False`) for each unique directory
- Phase 1: Process all file items concurrently (Semaphore(5))
- Phase 2: Process directories bottom-up (summarize children)
- Return result dict with all URIs

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_batch_add_hierarchy.py -v`
Expected: PASS

- [ ] **Step 5: Run regression**

Run: `uv run python -m unittest tests.test_e2e_phase1 -v`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_batch_add_hierarchy.py
git commit -m "feat: enhance batch_add with hierarchical tree building from file paths"
```

---

## Chunk 4: Conversation Mode (Phase 2)

### Task 11: Immediate Layer — Per-Message Embed + Write

Add `_write_immediate()` to orchestrator and wire it into ContextManager's `_commit()`.

**Files:**
- Modify: `src/opencortex/orchestrator.py` (add _write_immediate method)
- Modify: `src/opencortex/context/manager.py` (wire into _commit, add ConversationBuffer)
- Test: `tests/test_conversation_immediate.py`

- [ ] **Step 1: Write the failing test**

Test that after `_commit()` with a message, the message content is immediately searchable via vector retrieval.

```python
# tests/test_conversation_immediate.py
"""Test immediate layer: per-message embed + write for instant searchability."""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.config import CortexConfig, init_config
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.http.request_context import set_request_identity, reset_request_identity
from opencortex.orchestrator import MemoryOrchestrator


class MockEmbedder(DenseEmbedderBase):
    def __init__(self):
        super().__init__(model_name="mock")
    def embed(self, text):
        # Produce somewhat different vectors for different texts
        h = hash(text) % 1000
        return EmbedResult(dense_vector=[h/1000, 0.2, 0.3, 0.4])
    def get_dimension(self):
        return 4


class TestConversationImmediate(unittest.TestCase):
    def test_write_immediate_creates_searchable_record(self):
        """_write_immediate writes to Qdrant without LLM, making message searchable."""
        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                cfg = CortexConfig(data_root=tmpdir)
                init_config(cfg)
                orch = MemoryOrchestrator(config=cfg, embedder=MockEmbedder())
                await orch.init()
                tokens = set_request_identity("t1", "u1")
                try:
                    uri = await orch._write_immediate(
                        session_id="sess-1",
                        msg_index=0,
                        text="The user prefers dark mode for all applications.",
                    )
                    self.assertTrue(uri.startswith("opencortex://"))
                    self.assertIn("events", uri)
                finally:
                    reset_request_identity(tokens)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_conversation_immediate.py -v`
Expected: FAIL — `_write_immediate` does not exist.

- [ ] **Step 3: Add _write_immediate to orchestrator**

In `src/opencortex/orchestrator.py`, add:

```python
    async def _write_immediate(self, session_id: str, msg_index: int, text: str) -> str:
        """Write a single message for immediate searchability. No LLM, no CortexFS."""
        from opencortex.http.request_context import get_effective_identity, get_effective_project_id
        from opencortex.utils.uri import CortexURI
        from uuid import uuid4

        tid, uid = get_effective_identity()
        nid = uuid4().hex[:12]
        uri = CortexURI.build_private(tid, uid, "memories", "events", nid)

        # Embed without LLM
        vector = None
        if self._embedder:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self._embedder.embed, text)
            vector = result.dense_vector

        record = {
            "uri": uri,
            "parent_uri": CortexURI.build_private(tid, uid, "memories", "events", session_id),
            "is_leaf": True,
            "abstract": text[:500],
            "overview": "",
            "context_type": "memory",
            "category": "events",
            "scope": "private",
            "source_user_id": uid,
            "source_tenant_id": tid,
            "keywords": "",
            "meta": {"layer": "immediate", "msg_index": msg_index, "session_id": session_id},
            "session_id": session_id,
            "project_id": get_effective_project_id(),
            "mergeable": False,
            "ttl_expires_at": "",
        }
        if vector:
            record["vector"] = vector

        await self._storage.upsert(_CONTEXT_COLLECTION, record)
        return uri
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_conversation_immediate.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_conversation_immediate.py
git commit -m "feat: add _write_immediate for zero-LLM per-message searchability"
```

---

### Task 12: ConversationBuffer + Merge Layer + _commit Wiring

Add ConversationBuffer to ContextManager, wire immediate writes into `_commit()`, implement merge layer at token threshold.

**Files:**
- Modify: `src/opencortex/context/manager.py` (add buffer, wire _commit, merge logic)
- Test: `tests/test_conversation_merge.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_conversation_merge.py
"""Test conversation merge layer: buffer accumulation + threshold merge."""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestConversationBuffer(unittest.TestCase):
    def test_buffer_dataclass(self):
        from opencortex.context.manager import ConversationBuffer
        buf = ConversationBuffer()
        self.assertEqual(buf.messages, [])
        self.assertEqual(buf.token_count, 0)
        self.assertEqual(buf.start_msg_index, 0)
        self.assertEqual(buf.immediate_uris, [])

    def test_buffer_accumulates(self):
        from opencortex.context.manager import ConversationBuffer
        buf = ConversationBuffer()
        buf.messages.append("Hello world")
        buf.token_count += 100
        buf.immediate_uris.append("opencortex://test/uri")
        self.assertEqual(len(buf.messages), 1)
        self.assertEqual(buf.token_count, 100)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_conversation_merge.py -v`
Expected: FAIL — ConversationBuffer does not exist.

- [ ] **Step 3: Add ConversationBuffer and wire _commit**

In `src/opencortex/context/manager.py`, add:

```python
from dataclasses import dataclass, field as dc_field

@dataclass
class ConversationBuffer:
    """Per-session buffer for conversation mode incremental chunking."""
    messages: list = dc_field(default_factory=list)
    token_count: int = 0
    start_msg_index: int = 0
    immediate_uris: list = dc_field(default_factory=list)
```

Add `_conversation_buffers` dict to ContextManager `__init__`:
```python
        self._conversation_buffers: Dict[SessionKey, ConversationBuffer] = {}
```

In `_commit()`, after Observer recording succeeds (line ~341), add immediate write:
```python
        # Conversation mode: write immediate records for each message
        buffer = self._conversation_buffers.setdefault(sk, ConversationBuffer())
        for msg in messages:
            text = msg.get("content", msg.get("assistant_response", msg.get("user_message", "")))
            if text:
                try:
                    uri = await self._orchestrator._write_immediate(
                        session_id=session_id,
                        msg_index=buffer.start_msg_index + len(buffer.messages),
                        text=text,
                    )
                    buffer.messages.append(text)
                    buffer.immediate_uris.append(uri)
                    buffer.token_count += self._estimate_tokens(text)
                except Exception as exc:
                    logger.warning("[ContextManager] Immediate write failed: %s", exc)

        # Check merge threshold
        if buffer.token_count >= 1000:
            asyncio.create_task(self._merge_buffer(sk, session_id, tenant_id, user_id))
```

Add `_estimate_tokens()` and `_merge_buffer()` methods:
```python
    @staticmethod
    def _estimate_tokens(text: str) -> int:
        cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        other = len(text) - cjk
        return int(cjk * 0.7 + other * 0.3)

    async def _merge_buffer(self, sk, session_id, tenant_id, user_id):
        """Merge accumulated buffer into a high-quality chunk."""
        buffer = self._conversation_buffers.get(sk)
        if not buffer or not buffer.messages:
            return
        try:
            combined = "\n\n".join(buffer.messages)
            set_request_identity(tenant_id, user_id)
            ctx = await self._orchestrator.add(
                abstract="",
                content=combined,
                category="events",
                context_type="memory",
                meta={"layer": "merged", "msg_range": [buffer.start_msg_index, buffer.start_msg_index + len(buffer.messages) - 1], "session_id": session_id},
                session_id=session_id,
            )
            # Delete immediate records
            for uri in buffer.immediate_uris:
                try:
                    await self._orchestrator._delete_by_uri(uri)
                except Exception:
                    pass
            # Reset buffer
            new_start = buffer.start_msg_index + len(buffer.messages)
            self._conversation_buffers[sk] = ConversationBuffer(start_msg_index=new_start)
        except Exception as exc:
            logger.error("[ContextManager] Merge failed: %s", exc)
        finally:
            reset_request_identity()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_conversation_merge.py -v`
Expected: PASS

- [ ] **Step 5: Run context manager regression**

Run: `uv run python -m unittest tests.test_context_manager -v`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/context/manager.py tests/test_conversation_merge.py
git commit -m "feat: add ConversationBuffer + merge layer for two-layer incremental chunking"
```

---

### Task 13: Session End — Flush Buffer + Parent Node

On session_end, flush remaining buffer and create session parent node.

**Files:**
- Modify: `src/opencortex/context/manager.py` (_end method)
- Test: `tests/test_conversation_session_end.py`

- [ ] **Step 1: Write test for session end flush**

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: In `_end()`, add buffer flush logic**

Before Alpha pipeline trigger, force-merge any remaining buffer:
```python
        # Flush conversation buffer
        sk = self._make_session_key(tenant_id, user_id, session_id)
        buffer = self._conversation_buffers.get(sk)
        if buffer and buffer.messages:
            await self._merge_buffer(sk, session_id, tenant_id, user_id)
        # Create session parent node (is_leaf=False, summarize all chunks)
        # ... existing Alpha pipeline code ...
```

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Run regression**

Run: `uv run python -m unittest tests.test_context_manager -v`

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/context/manager.py tests/test_conversation_session_end.py
git commit -m "feat: flush conversation buffer on session_end + create parent node"
```

---

## Chunk 5: Intent Router Optimization (Phase 4)

### Task 14: Session-Aware Routing

The primary performance lever: skip LLM when no session context is present.

**Files:**
- Modify: `src/opencortex/retrieve/intent_router.py` (route method)
- Test: `tests/test_intent_router_session.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intent_router_session.py
"""Test session-aware routing: zero LLM when no session context."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.retrieve.intent_router import IntentRouter
from opencortex.retrieve.types import ContextType


class TestSessionAwareRouting(unittest.TestCase):
    def test_no_session_skips_llm(self):
        """Without session context, LLM should NOT be called."""
        llm_called = {"count": 0}

        async def mock_llm(prompt):
            llm_called["count"] += 1
            return '{"intent_type": "quick_lookup", "top_k": 5}'

        router = IntentRouter(llm_completion=mock_llm)

        async def run():
            # No session context → should skip LLM
            intent = await router.route("What is authentication?", session_context=None)
            self.assertEqual(llm_called["count"], 0, "LLM should not be called without session context")
            self.assertTrue(len(intent.queries) > 0, "Should still produce queries")

        asyncio.run(run())

    def test_with_session_calls_llm(self):
        """With session context, LLM SHOULD be called for multi-query."""
        llm_called = {"count": 0}

        async def mock_llm(prompt):
            llm_called["count"] += 1
            return '{"intent_type": "deep_analysis", "top_k": 10, "queries": [{"query": "auth middleware", "context_type": "any", "intent": "lookup"}]}'

        router = IntentRouter(llm_completion=mock_llm)

        async def run():
            session_ctx = {
                "summary": "Discussion about authentication system",
                "recent_messages": ["We talked about JWT tokens"],
            }
            intent = await router.route("What about the middleware?", session_context=session_ctx)
            self.assertGreater(llm_called["count"], 0, "LLM should be called with session context")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_intent_router_session.py -v`
Expected: FAIL — `route()` doesn't accept `session_context` parameter.

- [ ] **Step 3: Add session_context parameter to route()**

In `src/opencortex/retrieve/intent_router.py`, modify `route()`:

```python
    async def route(
        self,
        query: str,
        context_type: Optional[ContextType] = None,
        session_context: Optional[Dict[str, Any]] = None,
    ) -> SearchIntent:
        # Layer 1: keyword-based quick match
        intent = self._keyword_extract(query)

        if not intent.should_recall:
            return intent

        # Session-aware routing: only invoke LLM when session context exists
        if self._llm and session_context:
            try:
                llm_intent = await self._llm_classify(query, context_type, session_context)
                if llm_intent:
                    intent = self._merge(intent, llm_intent)
            except Exception as exc:
                logger.warning("[IntentRouter] LLM classification failed: %s", exc)

        # Build TypedQueries from intent
        intent.queries = self._build_queries(query, context_type, intent)
        return intent
```

Update `_llm_classify()` to accept and use session_context.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_intent_router_session.py -v`
Expected: PASS

- [ ] **Step 5: Update callers**

In `src/opencortex/orchestrator.py:1126`, pass session_context when available:
```python
            intent = await router.route(query, context_type, session_context=None)
```

In `src/opencortex/context/manager.py` (prepare phase), pass session summaries.

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/retrieve/intent_router.py tests/test_intent_router_session.py src/opencortex/orchestrator.py
git commit -m "feat: session-aware IntentRouter — zero LLM when no session context"
```

---

### Task 15: Multi-Query Concurrent Retrieval

When LLM analysis produces multiple TypedQuery objects, execute them concurrently via asyncio.gather.

**Files:**
- Modify: `src/opencortex/orchestrator.py:1208-1220` (search method, retrieval coros)
- Test: `tests/test_multi_query_concurrent.py`

- [ ] **Step 1: Write the failing test**

Test that multiple TypedQueries from IntentRouter result in concurrent retrieval calls.

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Implement multi-query from LLM response**

The current code already does `asyncio.gather(*retrieval_coros)` at line 1220. The change is in how `_llm_classify` produces multiple queries. Update `_llm_classify` to parse LLM response's `queries` array into multiple `TypedQuery` objects and set them on the returned `SearchIntent.queries`.

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/retrieve/intent_router.py src/opencortex/orchestrator.py tests/test_multi_query_concurrent.py
git commit -m "feat: multi-query concurrent retrieval from LLM intent analysis"
```

---

### Task 16: LRU Intent Cache

Cache recent intent analysis results (TTL 60s, maxsize 128) to avoid redundant LLM calls.

**Files:**
- Modify: `src/opencortex/retrieve/intent_router.py`
- Test: `tests/test_intent_cache.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intent_cache.py
"""Test LRU intent cache with TTL."""
import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.retrieve.intent_router import IntentRouter


class TestIntentCache(unittest.TestCase):
    def test_cache_hit_skips_llm(self):
        """Same query within TTL should reuse cached result."""
        call_count = {"n": 0}

        async def mock_llm(prompt):
            call_count["n"] += 1
            return '{"intent_type": "quick_lookup", "top_k": 3}'

        router = IntentRouter(llm_completion=mock_llm)

        async def run():
            ctx = {"summary": "test session"}
            await router.route("What is X?", session_context=ctx)
            first_count = call_count["n"]
            await router.route("What is X?", session_context=ctx)
            self.assertEqual(call_count["n"], first_count, "Second call should use cache")

        asyncio.run(run())

    def test_different_query_misses_cache(self):
        """Different query should not hit cache."""
        call_count = {"n": 0}

        async def mock_llm(prompt):
            call_count["n"] += 1
            return '{"intent_type": "quick_lookup", "top_k": 3}'

        router = IntentRouter(llm_completion=mock_llm)

        async def run():
            ctx = {"summary": "test"}
            await router.route("What is X?", session_context=ctx)
            await router.route("What is Y?", session_context=ctx)
            self.assertEqual(call_count["n"], 2, "Different queries should both call LLM")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Add LRU cache to IntentRouter**

In `src/opencortex/retrieve/intent_router.py`, add cache to `__init__`:

```python
    def __init__(self, llm_completion=None):
        self._llm = llm_completion
        self._cache: Dict[str, Tuple[SearchIntent, float]] = {}
        self._cache_ttl = 60.0
        self._cache_maxsize = 128
```

In `route()`, before LLM call:
```python
        cache_key = query  # simple key; can include context_type if needed
        cached = self._cache.get(cache_key)
        if cached:
            intent_cached, ts = cached
            if time.time() - ts < self._cache_ttl:
                intent_cached.queries = self._build_queries(query, context_type, intent_cached)
                return intent_cached
            else:
                del self._cache[cache_key]
```

After LLM call succeeds, store in cache:
```python
        # Cache the result
        if len(self._cache) >= self._cache_maxsize:
            # Evict oldest
            oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]
        self._cache[cache_key] = (intent, time.time())
```

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/retrieve/intent_router.py tests/test_intent_cache.py
git commit -m "feat: add LRU intent cache (TTL 60s, maxsize 128) to IntentRouter"
```

---

## Chunk 6: Integration Testing & Cleanup

### Task 17: End-to-End Integration Test

Verify the complete pipeline: store → IngestModeResolver → Document/Conversation mode → search → retrieve with correct hierarchy.

**Files:**
- Create: `tests/test_ingestion_e2e.py`

- [ ] **Step 1: Write comprehensive E2E test**

Cover:
1. Memory mode: short text → single record → searchable
2. Document mode: long markdown → multiple chunks → searchable by section
3. Conversation mode: multiple add_message → immediate searchability → merge
4. batch_add with scan_meta → directory hierarchy → searchable
5. IntentRouter: no-session query → fast (zero LLM) → results

- [ ] **Step 2: Run E2E test**

Run: `uv run python -m pytest tests/test_ingestion_e2e.py -v`
Expected: All pass.

- [ ] **Step 3: Run full regression**

Run: `uv run python -m unittest discover -s tests -v`
Expected: All existing tests pass, no regressions.

- [ ] **Step 4: Commit**

```bash
git add tests/test_ingestion_e2e.py
git commit -m "test: add end-to-end integration tests for ingestion + retrieval pipeline"
```

---

### Task 18: Update CLAUDE.md

Update the developer guide to document the new three-mode ingestion architecture, parser subsystem, and IntentRouter optimizations.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add parser directory to Directory Structure section**

- [ ] **Step 2: Add IngestModeResolver to Architecture section**

- [ ] **Step 3: Document Conversation Mode data flow**

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with three-mode ingestion and parser architecture"
```
