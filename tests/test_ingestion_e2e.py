"""End-to-end integration tests for the ingestion + retrieval pipeline.

Covers:
1. Memory mode: short text → single record → searchable
2. Document mode: long markdown → multiple chunks → searchable by section
3. Conversation mode: immediate write → merge layer
4. batch_add with scan_meta → directory hierarchy → searchable
5. IntentRouter: no-session query → fast (zero LLM) → results
"""
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
    """Returns a fixed vector for any text."""
    def __init__(self):
        super().__init__(model_name="mock")

    def embed(self, text):
        return EmbedResult(dense_vector=[0.1, 0.2, 0.3, 0.4])

    def get_dimension(self):
        return 4


def _make_orch(tmpdir, llm_completion=None):
    """Create a configured orchestrator with mock embedder."""
    cfg = CortexConfig(data_root=tmpdir, embedding_dimension=4, rerank_provider="disabled")
    init_config(cfg)
    return MemoryOrchestrator(config=cfg, embedder=MockEmbedder(), llm_completion=llm_completion)


class TestIngestionE2E(unittest.TestCase):
    """End-to-end ingestion pipeline tests."""

    # ── 1. Memory mode ──────────────────────────────────────────────

    def test_memory_mode_short_text(self):
        """Short text → single record via memory mode → searchable."""
        async def mock_llm(prompt):
            return '{"abstract": "user prefers dark mode", "overview": "user always uses dark mode in editors", "keywords": ["dark mode", "preferences"]}'

        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                orch = _make_orch(tmpdir, mock_llm)
                await orch.init()
                tokens = set_request_identity("t1", "u1")
                try:
                    result = await orch.add(
                        abstract="",
                        content="I always prefer dark mode in all my applications.",
                        category="preferences",
                        context_type="memory",
                    )
                    self.assertIsNotNone(result)
                    self.assertTrue(result.uri.startswith("opencortex://"))

                    # Search should find it
                    results = await orch.search("dark mode preference", limit=5)
                    self.assertGreater(results.total, 0)
                finally:
                    reset_request_identity(tokens)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())

    # ── 2. Document mode ─────────────────────────────────────────────

    def test_document_mode_large_markdown(self):
        """Long markdown with headings → multiple chunks via document mode."""
        async def mock_llm(prompt):
            return '{"abstract": "section summary", "overview": "section detail", "keywords": ["test"]}'

        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                orch = _make_orch(tmpdir, mock_llm)
                await orch.init()
                tokens = set_request_identity("t1", "u1")
                try:
                    # Create content large enough to trigger document mode (>4000 tokens)
                    section = "word " * 1500  # ~2250 tokens per section
                    content = f"# Introduction\n\n{section}\n\n# Methods\n\n{section}\n\n# Results\n\n{section}"

                    result = await orch.add(
                        abstract="",
                        content=content,
                        meta={"ingest_mode": "document"},
                        category="documents",
                        context_type="resource",
                    )
                    self.assertIsNotNone(result)
                    self.assertIsNotNone(result.uri)

                    # Should be searchable
                    results = await orch.search("introduction methods results", limit=10)
                    self.assertGreater(results.total, 0)
                finally:
                    reset_request_identity(tokens)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())

    # ── 3. Conversation mode ─────────────────────────────────────────

    def test_conversation_immediate_write(self):
        """Per-message immediate write creates searchable record without LLM."""
        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                orch = _make_orch(tmpdir)
                await orch.init()
                tokens = set_request_identity("t1", "u1")
                try:
                    uri = await orch._write_immediate(
                        session_id="sess-e2e",
                        msg_index=0,
                        text="The deployment uses Kubernetes with 3 replicas.",
                    )
                    self.assertTrue(uri.startswith("opencortex://"))
                    self.assertIn("events", uri)
                finally:
                    reset_request_identity(tokens)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())

    # ── 4. batch_add with hierarchy ──────────────────────────────────

    def test_batch_add_hierarchy_searchable(self):
        """batch_add with scan_meta → directory hierarchy → all searchable."""
        async def mock_llm(prompt):
            return '{"abstract": "file content", "overview": "file detail", "keywords": ["code"]}'

        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                orch = _make_orch(tmpdir, mock_llm)
                await orch.init()
                tokens = set_request_identity("t1", "u1")
                try:
                    items = [
                        {
                            "abstract": "Main entry point",
                            "content": "def main(): print('hello')",
                            "category": "documents",
                            "context_type": "resource",
                            "meta": {"file_path": "src/main.py"},
                        },
                        {
                            "abstract": "Utility functions",
                            "content": "def helper(): return True",
                            "category": "documents",
                            "context_type": "resource",
                            "meta": {"file_path": "src/utils/helpers.py"},
                        },
                    ]
                    result = await orch.batch_add(
                        items=items,
                        scan_meta={"total_files": 2, "has_git": True},
                    )
                    self.assertEqual(result["imported"], 2)
                    self.assertEqual(len(result.get("errors", [])), 0)

                    # Directory nodes + leaf items
                    total_uris = len(result.get("uris", []))
                    self.assertGreaterEqual(total_uris, 4)  # src, src/utils, 2 files

                    # Search should find items
                    results = await orch.search("main entry point", limit=5)
                    self.assertGreater(results.total, 0)
                finally:
                    reset_request_identity(tokens)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())

    # ── 5. Probe: no-session fast path ────────────────────────

    def test_memory_probe_no_session_fast(self):
        """Bootstrap probe stays local and stable without session context."""
        from opencortex.intent import MemoryBootstrapProbe

        class _StorageStub:
            async def search(self, **kwargs):
                return [
                    {
                        "uri": "opencortex://memory/preferences/dark-mode",
                        "category": "preferences",
                        "context_type": "memory",
                        "abstract": "User prefers dark mode.",
                        "_score": 0.82,
                    }
                ]

        class _EmbedderStub:
            model_name = "mock-probe"
            is_available = True

            def embed_query(self, text):
                class _Result:
                    dense_vector = [0.1, 0.2, 0.3, 0.4]

                return _Result()

        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                cfg = CortexConfig(data_root=tmpdir, embedding_dimension=4)
                init_config(cfg)
                probe = MemoryBootstrapProbe(
                    storage=_StorageStub(),
                    embedder=_EmbedderStub(),
                    collection_resolver=lambda: "context",
                    filter_builder=lambda: {"op": "and", "conds": []},
                )

                first = await probe.probe("What is dark mode?")
                second = await probe.probe("What is dark mode?")
                self.assertTrue(first.should_recall)
                self.assertEqual(first.to_dict(), second.to_dict())
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())

    # ── 6. IngestModeResolver routing ────────────────────────────────

    def test_ingest_mode_resolver_routing(self):
        """IngestModeResolver correctly routes to memory/document/conversation."""
        from opencortex.ingest.resolver import IngestModeResolver

        # Explicit mode wins
        self.assertEqual(
            IngestModeResolver.resolve(content="x", meta={"ingest_mode": "document"}),
            "document",
        )

        # Batch source → memory
        self.assertEqual(
            IngestModeResolver.resolve(content="x", meta={"source": "batch:scan"}),
            "memory",
        )

        # Session → conversation
        self.assertEqual(
            IngestModeResolver.resolve(content="x", session_id="s1"),
            "conversation",
        )

        # Large content with headings → document
        big = "# Title\n\n" + "word " * 7000
        self.assertEqual(
            IngestModeResolver.resolve(content=big),
            "document",
        )

        # Default → memory
        self.assertEqual(
            IngestModeResolver.resolve(content="short text"),
            "memory",
        )


if __name__ == "__main__":
    unittest.main()
