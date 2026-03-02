# SPDX-License-Identifier: Apache-2.0
"""
BM25-based sparse embedder for OpenCortex.

Zero external dependencies. Produces sparse vectors (token → weight)
suitable for Qdrant's native sparse vector search with RRF fusion.
"""

import math
import re
from typing import Dict, List, Optional

from opencortex.models.embedder.base import EmbedResult, SparseEmbedderBase

# Shared tokenization regexes (mirrors adapter.py:30 _tokenize_for_scoring)
_EN_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-\.]*[a-z0-9]|[a-z0-9]")
_CN_CHAR_RE = re.compile(r"[\u4e00-\u9fa5]")
# CamelCase identifiers get a bonus (preserved as-is before lowercasing)
_CAMEL_RE = re.compile(r"[a-z]+[A-Z][a-zA-Z]*")
# ALL_CAPS identifiers get a bonus
_CAPS_RE = re.compile(r"\b[A-Z]{2,}\b")


def _tokenize(text: str) -> List[str]:
    """Mixed Chinese/English tokenizer.

    Returns lowercased English tokens and individual Chinese characters.
    Mirrors _tokenize_for_scoring from adapter.py for consistency.
    """
    lower = (text or "").lower()
    tokens = _EN_TOKEN_RE.findall(lower)
    tokens.extend(_CN_CHAR_RE.findall(text or ""))
    return tokens


def _estimate_idf(token: str) -> float:
    """Heuristic IDF estimate based on token characteristics.

    Short common tokens get lower weight; CamelCase and ALL_CAPS tokens
    get higher weight. Avoids maintaining a corpus.

    Returns a value in roughly [0.5, 3.0].
    """
    length = len(token)

    # Single Chinese character — very common, low IDF
    if len(token) == 1 and _CN_CHAR_RE.match(token):
        return 0.8

    # Very short tokens (1-2 chars) — likely stopwords or common
    if length <= 2:
        return 0.5

    # Short common tokens (3-4 chars)
    if length <= 4:
        return 1.0

    # Medium tokens — reasonable IDF
    if length <= 8:
        return 1.5

    # Long tokens — likely specific, high IDF
    return 2.0


def _boost_special(token: str, original_text: str) -> float:
    """Extra boost for CamelCase/ALL_CAPS tokens found in original text.

    Returns multiplier >= 1.0.
    """
    # Check if original text contains CamelCase or ALL_CAPS patterns
    # that map to this lowercased token
    boost = 1.0

    # If original text has CamelCase patterns matching this token
    for m in _CAMEL_RE.finditer(original_text):
        if m.group().lower() == token:
            boost = max(boost, 2.0)

    for m in _CAPS_RE.finditer(original_text):
        if m.group().lower() == token:
            boost = max(boost, 2.5)

    # Path-like tokens (contain . / _ -) get a mild boost
    if any(c in token for c in "._-/"):
        boost = max(boost, 1.5)

    return boost


class BM25SparseEmbedder(SparseEmbedderBase):
    """Zero-dependency BM25-style sparse vector generator.

    Produces token → weight sparse vectors using BM25 scoring with
    heuristic IDF estimates. Designed for use with CompositeHybridEmbedder.

    Args:
        k1: BM25 term frequency saturation parameter.
        b: BM25 length normalization parameter.
        avg_dl: Assumed average document length for normalization.
        max_tokens: Maximum number of tokens in output vector.
    """

    def __init__(
        self,
        k1: float = 1.2,
        b: float = 0.75,
        avg_dl: float = 50.0,
        max_tokens: int = 128,
    ):
        super().__init__(model_name="bm25-sparse")
        self._k1 = k1
        self._b = b
        self._avg_dl = avg_dl
        self._max_tokens = max_tokens

    def embed(self, text: str) -> EmbedResult:
        """Generate sparse vector from text using BM25 scoring.

        Returns:
            EmbedResult with sparse_vector dict (token → weight), top-N by weight.
        """
        tokens = _tokenize(text)
        if not tokens:
            return EmbedResult(sparse_vector={})

        # Count term frequencies
        tf_map: Dict[str, int] = {}
        for t in tokens:
            tf_map[t] = tf_map.get(t, 0) + 1

        dl = len(tokens)
        weights: Dict[str, float] = {}

        for token, tf in tf_map.items():
            idf = _estimate_idf(token)

            # BM25 TF component
            numerator = tf * (self._k1 + 1)
            denominator = tf + self._k1 * (1 - self._b + self._b * dl / self._avg_dl)
            bm25_tf = numerator / denominator if denominator > 0 else 0.0

            # Apply special token boost
            boost = _boost_special(token, text)

            weight = idf * bm25_tf * boost
            if weight > 0:
                weights[token] = weight

        # Keep top-N by weight
        if len(weights) > self._max_tokens:
            sorted_items = sorted(weights.items(), key=lambda x: x[1], reverse=True)
            weights = dict(sorted_items[: self._max_tokens])

        return EmbedResult(sparse_vector=weights)

    @property
    def is_dense(self) -> bool:
        return False
