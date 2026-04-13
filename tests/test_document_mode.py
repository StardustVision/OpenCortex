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
                cfg = CortexConfig(data_root=tmpdir, embedding_dimension=4)
                init_config(cfg)
                orch = MemoryOrchestrator(config=cfg, embedder=MockEmbedder(), llm_completion=mock_llm)
                await orch.init()
                tokens = set_request_identity("t1", "u1")
                try:
                    section = "word " * 1500
                    content = f"# Intro\n\n{section}\n\n# Methods\n\n{section}\n\n# Results\n\n{section}"
                    result = await orch.add(
                        abstract="",
                        content=content,
                        meta={"ingest_mode": "document"},
                        category="documents",
                        context_type="resource",
                    )
                    self.assertIsNotNone(result)
                    self.assertIsNotNone(result.uri)
                    records = await orch._storage.filter("context", None, limit=50)
                    leaf_records = [record for record in records if record.get("is_leaf")]
                    self.assertGreaterEqual(len(leaf_records), 2)
                    self.assertTrue(
                        all(record.get("abstract_json") for record in leaf_records)
                    )
                    self.assertTrue(
                        all(record.get("memory_kind") for record in leaf_records)
                    )
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
                cfg = CortexConfig(data_root=tmpdir, embedding_dimension=4)
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
