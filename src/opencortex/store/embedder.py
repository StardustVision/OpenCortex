# SPDX-License-Identifier: Apache-2.0
"""Embedding helper for store flows."""

from __future__ import annotations

import asyncio
from typing import Any

from opencortex.core.context import Context
from opencortex.store.schemas import StoreEmbedding


class StoreEmbedder:
    """Embed store drafts without owning store business logic."""

    def __init__(self, embedder: Any) -> None:
        self._embedder = embedder

    async def embed_context(self, ctx: Context) -> StoreEmbedding:
        """Embed a context and attach its dense vector."""
        if not self._embedder:
            return StoreEmbedding()

        loop = asyncio.get_running_loop()
        embed_started = loop.time()
        result = await loop.run_in_executor(
            None,
            self._embedder.embed,
            ctx.get_vectorization_text(),
        )
        ctx.vector = result.dense_vector
        return StoreEmbedding(
            embed_ms=int((loop.time() - embed_started) * 1000),
            sparse_vector=result.sparse_vector if result.sparse_vector else None,
        )

