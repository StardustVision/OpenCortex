# SPDX-License-Identifier: Apache-2.0
"""Model runtime helper service for CortexMemory.

This service owns embedder fallback/wrapping and rerank runtime helpers. The
orchestrator keeps compatibility wrappers because tests and bootstrap code patch
those method names directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from opencortex.models.embedder.base import EmbedderBase
from opencortex.retrieve.rerank_client import RerankClient
from opencortex.retrieve.rerank_config import RerankConfig

if TYPE_CHECKING:
    from opencortex.cortex_memory import CortexMemory

logger = logging.getLogger(__name__)

_IMMEDIATE_LOCAL_FALLBACK_MODEL = "BAAI/bge-m3"


class ModelRuntimeService:
    """Own embedder and rerank runtime helpers for CortexMemory."""

    def __init__(self, orchestrator: "CortexMemory") -> None:
        self._orch = orchestrator

    def _is_retryable_immediate_embed_exception(self, exc: Exception) -> bool:
        """Return True when immediate remote embedding should fall back locally."""
        if isinstance(exc, TimeoutError):
            return True
        try:
            import httpx

            return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))
        except Exception:
            return False

    def _create_immediate_fallback_embedder(self) -> Optional[EmbedderBase]:
        """Create a local fallback embedder for immediate-write remote failures."""
        orch = self._orch
        try:
            from opencortex.models.embedder.local_embedder import LocalEmbedder

            local_config = {"onnx_intra_op_threads": orch._config.onnx_intra_op_threads}
            embedder = LocalEmbedder(
                model_name=_IMMEDIATE_LOCAL_FALLBACK_MODEL,
                config=local_config,
            )
            if not embedder.is_available:
                logger.warning(
                    "[ModelRuntimeService] Immediate local fallback unavailable "
                    "(model=%s)",
                    _IMMEDIATE_LOCAL_FALLBACK_MODEL,
                )
                return None

            detected_dim = embedder.get_dimension()
            expected_dim = orch._config.embedding_dimension or detected_dim
            if expected_dim and detected_dim != expected_dim:
                logger.warning(
                    "[ModelRuntimeService] Immediate local fallback disabled: "
                    "model=%s dim=%d != configured_dim=%d",
                    _IMMEDIATE_LOCAL_FALLBACK_MODEL,
                    detected_dim,
                    expected_dim,
                )
                embedder.close()
                return None

            logger.info(
                "[ModelRuntimeService] Created immediate local fallback embedder "
                "(model=%s, dim=%d)",
                _IMMEDIATE_LOCAL_FALLBACK_MODEL,
                detected_dim,
            )
            return orch._wrap_with_cache(orch._wrap_with_hybrid(embedder))
        except Exception as exc:
            logger.warning(
                "[ModelRuntimeService] Failed to create immediate local fallback "
                "embedder: %s",
                exc,
            )
            return None

    def _get_immediate_fallback_embedder(self) -> Optional[EmbedderBase]:
        """Return cached immediate local fallback embedder if available."""
        orch = self._orch
        if orch._immediate_fallback_embedder_attempted:
            return orch._immediate_fallback_embedder
        orch._immediate_fallback_embedder_attempted = True
        orch._immediate_fallback_embedder = orch._create_immediate_fallback_embedder()
        return orch._immediate_fallback_embedder

    def _wrap_with_hybrid(self, embedder: EmbedderBase) -> EmbedderBase:
        """Wrap dense embedder with BM25 sparse for hybrid search."""
        from opencortex.models.embedder.base import (
            CompositeHybridEmbedder,
            HybridEmbedderBase,
        )
        from opencortex.models.embedder.sparse import BM25SparseEmbedder

        if isinstance(embedder, HybridEmbedderBase):
            return embedder
        return CompositeHybridEmbedder(embedder, BM25SparseEmbedder())

    def _wrap_with_cache(self, embedder: EmbedderBase) -> EmbedderBase:
        """Wrap an embedder with LRU cache."""
        try:
            from opencortex.models.embedder.cache import CachedEmbedder

            cached = CachedEmbedder(embedder, max_size=10000, ttl_seconds=3600)
            logger.info(
                "[ModelRuntimeService] Wrapped embedder with LRU cache "
                "(max=10000, ttl=3600s)"
            )
            return cached
        except Exception as exc:
            logger.warning("[ModelRuntimeService] Failed to wrap with cache: %s", exc)
            return embedder

    def _get_or_create_rerank_client(self) -> RerankClient:
        """Return the process-lifetime RerankClient singleton."""
        orch = self._orch
        if orch._rerank_client is None:
            orch._rerank_client = RerankClient(
                orch._build_rerank_config(),
                llm_completion=orch._llm_completion,
            )
        return orch._rerank_client

    def _build_rerank_config(self) -> RerankConfig:
        """Build RerankConfig from explicit config plus CortexConfig fields."""
        orch = self._orch
        base = orch._rerank_config or RerankConfig()
        cfg = orch._config
        return RerankConfig(
            model=base.model or cfg.rerank_model,
            api_key=base.api_key or cfg.rerank_api_key or cfg.embedding_api_key,
            api_base=base.api_base or cfg.rerank_api_base,
            threshold=base.threshold or cfg.rerank_threshold,
            provider=getattr(base, "provider", "") or cfg.rerank_provider,
            fusion_beta=getattr(base, "fusion_beta", 0.0) or cfg.rerank_fusion_beta,
            max_candidates=getattr(base, "max_candidates", 0)
            or cfg.rerank_max_candidates,
            use_llm_fallback=getattr(base, "use_llm_fallback", True),
        )
