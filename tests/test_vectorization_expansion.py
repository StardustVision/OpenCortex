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
                cfg = CortexConfig(data_root=tmpdir, embedding_dimension=4)
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
                    # The first embed call is for the add() operation itself
                    self.assertGreater(len(embedded_texts), 0)
                    first_embedded = embedded_texts[0]
                    self.assertIn("auth", first_embedded)
                    self.assertIn("login", first_embedded)
                    self.assertIn("JWT", first_embedded)
                    self.assertIn("test abstract", first_embedded)
                finally:
                    reset_request_identity(tokens)
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
