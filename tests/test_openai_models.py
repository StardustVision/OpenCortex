"""
Tests for OpenAI-compatible model integrations.

Covers:
- OpenAIDenseEmbedder (embed, embed_batch, dimension, errors)
- Orchestrator openai provider branch
- LLM factory config-key priority
- RerankClient API and LLM fallback modes
"""

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.config import CortexConfig


# =============================================================================
# Helpers
# =============================================================================


def _make_embedding_response(vectors, model="text-embedding-3-small"):
    """Build a mock /v1/embeddings JSON response."""
    data = []
    for i, vec in enumerate(vectors):
        data.append({"object": "embedding", "index": i, "embedding": vec})
    return {
        "object": "list",
        "data": data,
        "model": model,
        "usage": {"prompt_tokens": 5, "total_tokens": 5},
    }


def _mock_httpx_response(json_body, status_code=200):
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = json.dumps(json_body)
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return resp


# =============================================================================
# 1. OpenAIDenseEmbedder
# =============================================================================


class TestOpenAIEmbedder(unittest.TestCase):
    """Test OpenAIDenseEmbedder with mocked httpx.Client."""

    def _make_embedder(self, dimension=None):
        from opencortex.models.embedder.openai_embedder import OpenAIDenseEmbedder

        mock_client = MagicMock()
        with patch("opencortex.models.embedder.openai_embedder.httpx.Client",
                    return_value=mock_client):
            embedder = OpenAIDenseEmbedder(
                model_name="text-embedding-3-small",
                api_key="sk-test-key",
                api_base="https://api.openai.com/v1",
                dimension=dimension,
            )
        self._mock_client = mock_client
        return embedder

    def test_embed_single(self):
        """Single text -> correct EmbedResult with dense_vector."""
        embedder = self._make_embedder()
        vec = [0.1, 0.2, 0.3, 0.4]
        self._mock_client.post.return_value = _mock_httpx_response(
            _make_embedding_response([vec])
        )

        result = embedder.embed("hello world")

        self.assertIsNotNone(result.dense_vector)
        self.assertEqual(result.dense_vector, vec)
        self._mock_client.post.assert_called_once()

    def test_embed_batch(self):
        """Batch of texts -> correct list of EmbedResult."""
        embedder = self._make_embedder()
        vecs = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
        self._mock_client.post.return_value = _mock_httpx_response(
            _make_embedding_response(vecs)
        )

        results = embedder.embed_batch(["a", "b", "c"])

        self.assertEqual(len(results), 3)
        for i, r in enumerate(results):
            self.assertEqual(r.dense_vector, vecs[i])
        # Should be a single API call, not 3
        self.assertEqual(self._mock_client.post.call_count, 1)

    def test_dimension_auto_detect(self):
        """First embed auto-detects dimension."""
        embedder = self._make_embedder(dimension=None)
        vec = [0.1, 0.2, 0.3]
        self._mock_client.post.return_value = _mock_httpx_response(
            _make_embedding_response([vec])
        )

        self.assertIsNone(embedder._dimension)
        embedder.embed("probe")
        self.assertEqual(embedder.get_dimension(), 3)

    def test_dimension_explicit(self):
        """Explicit dimension truncates and normalizes vectors."""
        embedder = self._make_embedder(dimension=2)
        # 4-dim vector, should be truncated to 2 and L2-normalized
        vec = [3.0, 4.0, 99.0, 99.0]
        self._mock_client.post.return_value = _mock_httpx_response(
            _make_embedding_response([vec])
        )

        result = embedder.embed("test")

        self.assertEqual(len(result.dense_vector), 2)
        # After truncation to [3.0, 4.0], L2 norm = 5.0 -> [0.6, 0.8]
        self.assertAlmostEqual(result.dense_vector[0], 0.6, places=5)
        self.assertAlmostEqual(result.dense_vector[1], 0.8, places=5)

    def test_api_error_raises(self):
        """4xx/5xx responses raise RuntimeError."""
        embedder = self._make_embedder()
        self._mock_client.post.return_value = _mock_httpx_response(
            {"error": {"message": "Invalid API key"}}, status_code=401
        )

        with self.assertRaises(RuntimeError) as ctx:
            embedder.embed("fail")
        self.assertIn("401", str(ctx.exception))


# =============================================================================
# 2. Orchestrator openai provider
# =============================================================================


class TestOrchestratorProvider(unittest.TestCase):
    """Verify orchestrator creates OpenAIDenseEmbedder for provider='openai'."""

    def test_orchestrator_openai_provider(self):
        """config.embedding_provider='openai' -> OpenAIDenseEmbedder created."""
        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.models.embedder.openai_embedder import OpenAIDenseEmbedder

        config = CortexConfig(
            embedding_provider="openai",
            embedding_model="text-embedding-3-small",
            embedding_api_key="sk-test",
            embedding_api_base="https://api.openai.com/v1",
            embedding_dimension=1536,
        )

        mock_client = MagicMock()
        with patch("opencortex.models.embedder.openai_embedder.httpx.Client",
                    return_value=mock_client):
            orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
            orch._config = config
            embedder = orch._create_default_embedder()

        self.assertIsInstance(embedder, OpenAIDenseEmbedder)
        self.assertEqual(embedder.model_name, "text-embedding-3-small")
        self.assertEqual(embedder.api_key, "sk-test")
        embedder.close()


# =============================================================================
# 3. LLM Factory
# =============================================================================


class TestLLMFactory(unittest.TestCase):
    """Test llm_factory config-key priority."""

    @patch.dict(os.environ, {}, clear=True)
    def test_llm_factory_config_key(self):
        """Config llm_api_key works without OPENAI_API_KEY env var."""
        from opencortex.models.llm_factory import create_llm_completion

        config = CortexConfig(
            llm_api_key="sk-from-config",
            llm_api_base="https://api.deepseek.com/v1",
            llm_model="deepseek-chat",
        )

        callable_ = create_llm_completion(config)
        self.assertIsNotNone(callable_)

    @patch.dict(os.environ, {}, clear=True)
    def test_llm_factory_openai_fallback(self):
        """Ark SDK unavailable + config key -> OpenAI-compatible backend."""
        from opencortex.models.llm_factory import create_llm_completion

        config = CortexConfig(
            llm_api_key="sk-from-config",
            llm_api_base="https://api.openai.com/v1",
            llm_model="gpt-4o-mini",
        )

        # Ark SDK not installed -> should fall through to OpenAI-compatible
        callable_ = create_llm_completion(config)
        self.assertIsNotNone(callable_)


# =============================================================================
# 4. RerankClient
# =============================================================================


class TestRerankClient(unittest.TestCase):
    """Test RerankClient API and LLM fallback modes."""

    def test_rerank_api_mode(self):
        """Mock /rerank endpoint -> correct scores returned."""
        from opencortex.retrieve.rerank_client import RerankClient
        from opencortex.retrieve.rerank_config import RerankConfig

        config = RerankConfig(
            model="rerank-v3.5",
            api_key="rk-test",
            api_base="https://api.cohere.com/v2",
        )
        client = RerankClient(config=config)
        self.assertEqual(client.mode, "api")

        # Mock the async API call
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.95},
                {"index": 1, "relevance_score": 0.3},
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_async_client):
            scores = asyncio.run(
                client.rerank("query", ["doc1", "doc2"])
            )

        self.assertEqual(len(scores), 2)
        self.assertAlmostEqual(scores[0], 0.95)
        self.assertAlmostEqual(scores[1], 0.3)

    def test_rerank_llm_fallback(self):
        """API fails -> LLM fallback returns scores."""
        from opencortex.retrieve.rerank_client import RerankClient
        from opencortex.retrieve.rerank_config import RerankConfig

        async def mock_llm(prompt: str) -> str:
            return "[0.9, 0.4]"

        config = RerankConfig(
            model="rerank-v3.5",
            api_key="rk-test",
            api_base="https://api.cohere.com/v2",
            use_llm_fallback=True,
        )
        client = RerankClient(config=config, llm_completion=mock_llm)

        # Make the API call fail so it falls back to LLM
        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_client.post = AsyncMock(side_effect=Exception("API down"))

        with patch("httpx.AsyncClient", return_value=mock_async_client):
            scores = asyncio.run(
                client.rerank("query", ["doc1", "doc2"])
            )

        self.assertEqual(len(scores), 2)
        self.assertAlmostEqual(scores[0], 0.9)
        self.assertAlmostEqual(scores[1], 0.4)


if __name__ == "__main__":
    unittest.main()
