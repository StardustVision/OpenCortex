# SPDX-License-Identifier: Apache-2.0
"""Tests for write-path embedding coordination."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import List

from opencortex.core.context import Context, Vectorize
from opencortex.models.embedder.base import EmbedResult
from opencortex.services.memory_write_embed_service import MemoryWriteEmbedService


class _SpyEmbedder:
    """Capture write embed inputs and return a configured result."""

    def __init__(self, result: EmbedResult) -> None:
        self.result = result
        self.inputs: List[str] = []

    def embed(self, text: str) -> EmbedResult:
        self.inputs.append(text)
        return self.result


class TestMemoryWriteEmbedService(unittest.IsolatedAsyncioTestCase):
    """Verify the extracted write embed boundary."""

    def _build_service(
        self,
        embedder: _SpyEmbedder | None,
    ) -> MemoryWriteEmbedService:
        orch = SimpleNamespace(_embedder=embedder)
        write_service = SimpleNamespace(_orch=orch)
        return MemoryWriteEmbedService(write_service)

    async def test_no_embedder_returns_empty_result(self) -> None:
        """Missing embedders leave the context unmodified."""
        service = self._build_service(None)
        ctx = Context(uri="opencortex://tenant/user/memories/events/no_embed")

        result = await service.embed_for_write(ctx)

        self.assertEqual(result.embed_ms, 0)
        self.assertIsNone(result.sparse_vector)
        self.assertIsNone(ctx.vector)

    async def test_dense_embed_sets_context_vector(self) -> None:
        """Dense embeddings are attached to the Context for dedup and store."""
        embedder = _SpyEmbedder(EmbedResult(dense_vector=[0.1, 0.2, 0.3]))
        service = self._build_service(embedder)
        ctx = Context(
            uri="opencortex://tenant/user/memories/events/dense",
            abstract="fallback abstract",
        )
        ctx.vectorize = Vectorize("custom vectorization text")

        result = await service.embed_for_write(ctx)

        self.assertEqual(embedder.inputs, ["custom vectorization text"])
        self.assertEqual(ctx.vector, [0.1, 0.2, 0.3])
        self.assertIsNone(result.sparse_vector)
        self.assertIsInstance(result.embed_ms, int)
        self.assertGreaterEqual(result.embed_ms, 0)

    async def test_sparse_vector_is_returned_for_persistence(self) -> None:
        """Sparse vectors are returned for the store persistence boundary."""
        sparse_vector = {"indices": [1, 3], "values": [0.4, 0.7]}
        embedder = _SpyEmbedder(
            EmbedResult(
                dense_vector=[0.1, 0.2, 0.3],
                sparse_vector=sparse_vector,
            )
        )
        service = self._build_service(embedder)
        ctx = Context(uri="opencortex://tenant/user/memories/events/hybrid")

        result = await service.embed_for_write(ctx)

        self.assertEqual(ctx.vector, [0.1, 0.2, 0.3])
        self.assertEqual(result.sparse_vector, sparse_vector)
