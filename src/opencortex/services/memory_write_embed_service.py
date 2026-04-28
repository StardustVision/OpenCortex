# SPDX-License-Identifier: Apache-2.0
"""Write-path embedding coordination for memory writes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from opencortex.core.context import Context

if TYPE_CHECKING:
    from opencortex.services.memory_write_service import MemoryWriteService


@dataclass(frozen=True)
class MemoryWriteEmbedResult:
    """Result of embedding a write context."""

    embed_ms: int = 0
    sparse_vector: Optional[Any] = None


class MemoryWriteEmbedService:
    """Owns normal write-path embedding mechanics."""

    def __init__(self, write_service: "MemoryWriteService") -> None:
        """Bind the embed service to a write service facade."""
        self._write_service = write_service

    @property
    def _orch(self) -> Any:
        return self._write_service._orch

    async def embed_for_write(self, ctx: Context) -> MemoryWriteEmbedResult:
        """Embed a normal write context and attach its dense vector."""
        embedder = self._orch._embedder
        if not embedder:
            return MemoryWriteEmbedResult()

        loop = asyncio.get_running_loop()
        embed_started = loop.time()
        result = await loop.run_in_executor(
            None,
            embedder.embed,
            ctx.get_vectorization_text(),
        )
        embed_ms = int((loop.time() - embed_started) * 1000)
        ctx.vector = result.dense_vector
        return MemoryWriteEmbedResult(
            embed_ms=embed_ms,
            sparse_vector=result.sparse_vector if result.sparse_vector else None,
        )
