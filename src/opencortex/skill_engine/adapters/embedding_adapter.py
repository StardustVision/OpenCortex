"""Embedding adapter — delegates to existing OpenCortex embedder."""

from typing import List, Protocol


class EmbeddingAdapter(Protocol):
    """Protocol for embedding generation."""

    def embed(self, text: str) -> List[float]: ...
    def embed_batch(self, texts: List[str]) -> List[List[float]]: ...
