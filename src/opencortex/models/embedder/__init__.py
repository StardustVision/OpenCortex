# SPDX-License-Identifier: Apache-2.0
from opencortex.models.embedder.base import (
    CompositeHybridEmbedder,
    DenseEmbedderBase,
    EmbedderBase,
    EmbedResult,
    HybridEmbedderBase,
    SparseEmbedderBase,
    truncate_and_normalize,
)

from opencortex.models.embedder.cache import CachedEmbedder
from opencortex.models.embedder.sparse import BM25SparseEmbedder

try:
    from opencortex.models.embedder.openai_embedder import OpenAIDenseEmbedder
except ImportError:  # httpx not installed
    OpenAIDenseEmbedder = None  # type: ignore[assignment,misc]

__all__ = [
    "EmbedderBase",
    "DenseEmbedderBase",
    "SparseEmbedderBase",
    "HybridEmbedderBase",
    "CompositeHybridEmbedder",
    "BM25SparseEmbedder",
    "EmbedResult",
    "truncate_and_normalize",
    "OpenAIDenseEmbedder",
    "CachedEmbedder",
]
