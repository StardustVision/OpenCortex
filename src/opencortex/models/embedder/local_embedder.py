"""
Local embedding via FastEmbed ONNX inference.

Falls back to remote API if local model fails to load.
Provides low-latency local CPU embedding when paired with a lightweight model.

Each thread gets its own ONNX session via threading.local() so concurrent
run_in_executor calls run truly in parallel without serializing on one session.
"""

import logging
import threading
from typing import Any, Dict, List, Optional

from opencortex.models.embedder.base import EmbedderBase, EmbedResult

logger = logging.getLogger(__name__)


DEFAULT_LOCAL_EMBEDDING_MODEL = (
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)

_E5_PREFIXES = frozenset(("intfloat/e5-", "intfloat/multilingual-e5-"))


def _is_e5_model(name: str) -> bool:
    return any(name.startswith(p) for p in _E5_PREFIXES)


class LocalEmbedder(EmbedderBase):
    """Local embedding using FastEmbed (ONNX inference).

    Uses thread-local model instances so concurrent run_in_executor calls
    each get their own ONNX session and run truly in parallel.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_LOCAL_EMBEDDING_MODEL,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(model_name=model_name, config=config)
        self._needs_prefix = _is_e5_model(model_name)
        self._local = threading.local()
        self._dimension: Optional[int] = None
        self._available = False
        # Probe once on the main thread to validate the model loads
        model = self._get_model()
        if model is not None:
            self._available = True

    def _get_model(self):
        """Return this thread's model instance, initializing if needed."""
        if not hasattr(self._local, "model"):
            self._local.model = self._init_model()
        return self._local.model

    def _init_model(self):
        try:
            from fastembed import TextEmbedding
            kwargs = {}
            threads = (self.config or {}).get("onnx_intra_op_threads", 0)
            if threads > 0:
                kwargs["threads"] = threads
            model = TextEmbedding(model_name=self.model_name, **kwargs)
            test_result = list(model.embed(["test"]))[0]
            if self._dimension is None:
                self._dimension = len(test_result)
                logger.info(
                    "[LocalEmbedder] Loaded %s (dim=%d) on thread %s",
                    self.model_name, self._dimension, threading.current_thread().name,
                )
            return model
        except Exception as e:
            logger.warning(
                "[LocalEmbedder] Failed to load %s: %s. "
                "Install with: uv add fastembed",
                self.model_name, e,
            )
            return None

    def embed(self, text: str) -> EmbedResult:
        model = self._get_model()
        if model is None:
            raise RuntimeError(
                f"Local model {self.model_name} not loaded. "
                "Install fastembed: uv add fastembed"
            )
        if self._needs_prefix:
            text = "passage: " + text
        embeddings = list(model.embed([text]))
        vector = embeddings[0].tolist()
        return EmbedResult(dense_vector=vector)

    def embed_query(self, text: str) -> EmbedResult:
        model = self._get_model()
        if model is None:
            raise RuntimeError(
                f"Local model {self.model_name} not loaded. "
                "Install fastembed: uv add fastembed"
            )
        if self._needs_prefix:
            text = "query: " + text
        embeddings = list(model.embed([text]))
        vector = embeddings[0].tolist()
        return EmbedResult(dense_vector=vector)

    def embed_batch(self, texts: List[str]) -> List[EmbedResult]:
        model = self._get_model()
        if model is None:
            raise RuntimeError(f"Local model {self.model_name} not loaded")
        if self._needs_prefix:
            texts = ["passage: " + t for t in texts]
        embeddings = list(model.embed(texts))
        return [EmbedResult(dense_vector=e.tolist()) for e in embeddings]

    def get_dimension(self) -> int:
        return self._dimension or 1024

    def close(self):
        if hasattr(self._local, "model"):
            self._local.model = None

    @property
    def is_available(self) -> bool:
        return self._available
