"""
Local embedding via FastEmbed (BGE-M3 ONNX inference).

Falls back to remote API if local model fails to load.
Provides ~10-30ms CPU embedding without network latency.
"""

import logging
from typing import Any, Dict, List, Optional

from opencortex.models.embedder.base import EmbedderBase, EmbedResult

logger = logging.getLogger(__name__)


class LocalEmbedder(EmbedderBase):
    """Local embedding using FastEmbed (ONNX inference)."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(model_name=model_name, config=config)
        self._model = None
        self._dimension = None
        self._init_model()

    def _init_model(self) -> None:
        try:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=self.model_name)
            # Detect dimension from a test embedding
            test_result = list(self._model.embed(["test"]))[0]
            self._dimension = len(test_result)
            logger.info(
                "[LocalEmbedder] Loaded %s (dim=%d)",
                self.model_name, self._dimension,
            )
        except Exception as e:
            logger.warning(
                "[LocalEmbedder] Failed to load %s: %s. "
                "Install with: uv add fastembed",
                self.model_name, e,
            )
            self._model = None

    def embed(self, text: str) -> EmbedResult:
        if self._model is None:
            raise RuntimeError(
                f"Local model {self.model_name} not loaded. "
                "Install fastembed: uv add fastembed"
            )
        embeddings = list(self._model.embed([text]))
        vector = embeddings[0].tolist()
        return EmbedResult(dense_vector=vector)

    def embed_batch(self, texts: List[str]) -> List[EmbedResult]:
        if self._model is None:
            raise RuntimeError(f"Local model {self.model_name} not loaded")
        embeddings = list(self._model.embed(texts))
        return [EmbedResult(dense_vector=e.tolist()) for e in embeddings]

    def get_dimension(self) -> int:
        return self._dimension or 1024

    def close(self):
        self._model = None

    @property
    def is_available(self) -> bool:
        return self._model is not None
