"""Query Fast Classifier — Embedding Nearest Centroid."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class QueryClassification:
    query_class: str
    need_llm_intent: bool
    lexical_boost: float
    time_filter_hint: Optional[str] = None
    doc_scope_hint: Optional[str] = None


def _to_dense_array(vec: Any) -> np.ndarray:
    """Extract dense vector from EmbedResult or plain array/list."""
    # Handle EmbedResult (duck-typed check)
    if hasattr(vec, "dense_vector") and vec.dense_vector is not None:
        return np.asarray(vec.dense_vector, dtype=float)
    # Handle numpy arrays, lists, or other array-like objects
    return np.asarray(vec, dtype=float)


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


class QueryFastClassifier:
    """Two-layer query classifier: structural signals + embedding centroid."""

    def __init__(self, embedder: Any, config: Any) -> None:
        self.embedder = embedder
        self.threshold = getattr(config, "query_classifier_threshold", 0.3)
        self.hybrid_weights: Dict[str, Dict[str, float]] = getattr(
            config, "query_classifier_hybrid_weights", {}
        )
        class_descriptions: Dict[str, str] = getattr(
            config, "query_classifier_classes", {}
        )
        self.centroids: Dict[str, np.ndarray] = {}
        for cls, desc in class_descriptions.items():
            vec = embedder.embed(desc)
            self.centroids[cls] = _to_dense_array(vec)
        logger.info("[QueryFastClassifier] Loaded %d class centroids", len(self.centroids))

    def classify(
        self,
        query: str,
        target_doc_id: Optional[str] = None,
        session_context: Optional[dict] = None,
    ) -> QueryClassification:
        # Layer 0: structural signal
        if target_doc_id:
            weights = self.hybrid_weights.get("document_scoped", {})
            return QueryClassification(
                query_class="document_scoped",
                need_llm_intent=False,
                lexical_boost=weights.get("lexical", 0.5),
                doc_scope_hint=target_doc_id,
            )

        if not self.centroids:
            return self._fallback_complex()

        # Layer 1: embedding nearest centroid
        query_vec = _to_dense_array(self.embedder.embed(query))
        scores = {cls: _cosine_sim(query_vec, c) for cls, c in self.centroids.items()}
        best_class = max(scores, key=scores.get)

        if scores[best_class] < self.threshold:
            return self._fallback_complex()

        weights = self.hybrid_weights.get(best_class, {})
        time_hint = None
        if best_class == "temporal_lookup":
            time_hint = "recent"

        return QueryClassification(
            query_class=best_class,
            need_llm_intent=False,
            lexical_boost=weights.get("lexical", 0.3),
            time_filter_hint=time_hint,
        )

    def _fallback_complex(self) -> QueryClassification:
        weights = self.hybrid_weights.get("complex", {})
        return QueryClassification(
            query_class="complex",
            need_llm_intent=True,
            lexical_boost=weights.get("lexical", 0.3),
        )
