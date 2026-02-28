"""
Tests for frontier batching search optimization.

Uses real QdrantStorageAdapter (embedded) + deterministic MockEmbedder.
No mocks on storage or retriever internals.
"""

import asyncio
import math
import os
import shutil
import sys
import tempfile
import unittest
from collections import defaultdict
from typing import List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.config import CortexConfig, init_config
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever
from opencortex.retrieve.types import ContextType, DetailLevel, TypedQuery
from opencortex.storage.qdrant.adapter import QdrantStorageAdapter


class MockEmbedder(DenseEmbedderBase):
    """Deterministic hash-based embedder. Dimension=128."""
    DIMENSION = 128

    def __init__(self):
        super().__init__(model_name="mock-embedder-v1")

    def embed(self, text: str) -> EmbedResult:
        return EmbedResult(dense_vector=self._text_to_vector(text))

    def get_dimension(self) -> int:
        return self.DIMENSION

    @staticmethod
    def _text_to_vector(text: str) -> List[float]:
        raw = []
        for i in range(128):
            bits = hash(f"{text}_{i}") & 0xFFFF
            raw.append((bits & 0xFF) / 255.0 - 0.5)
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]


class StorageSpy(QdrantStorageAdapter):
    """Thin wrapper that counts search calls on real Qdrant."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.call_counts = {"search": 0}

    async def search(self, *args, **kwargs):
        self.call_counts["search"] += 1
        return await super().search(*args, **kwargs)

    def reset_counts(self):
        self.call_counts = {"search": 0}


class TestShouldRerankScoreKey(unittest.TestCase):
    """Test _should_rerank with score_key parameter."""

    def test_should_rerank_score_key_final_score(self):
        """score_key='_final_score' reads _final_score field."""
        retriever = HierarchicalRetriever(
            storage=None, embedder=None, rerank_config=None
        )
        # Gap > threshold (0.15) → should NOT rerank
        results = [{"_final_score": 0.9}, {"_final_score": 0.5}]
        self.assertFalse(retriever._should_rerank(results, score_key="_final_score"))

        # Gap <= threshold → should rerank
        results = [{"_final_score": 0.9}, {"_final_score": 0.85}]
        self.assertTrue(retriever._should_rerank(results, score_key="_final_score"))

    def test_should_rerank_default_score_key(self):
        """Default score_key='_score' preserves backward compat."""
        retriever = HierarchicalRetriever(
            storage=None, embedder=None, rerank_config=None
        )
        results = [{"_score": 0.9}, {"_score": 0.5}]
        self.assertFalse(retriever._should_rerank(results))

        results = [{"_score": 0.9}, {"_score": 0.85}]
        self.assertTrue(retriever._should_rerank(results))

    def test_should_rerank_single_result(self):
        """Single result → always False."""
        retriever = HierarchicalRetriever(
            storage=None, embedder=None, rerank_config=None
        )
        self.assertFalse(retriever._should_rerank([{"_score": 0.9}]))


if __name__ == "__main__":
    unittest.main()
