"""
SkillRanker — hybrid BM25 + embedding re-ranking for skill search.

Mirrors OpenSpace skill_ranker.py. Uses local models only (no API calls).
Refines Qdrant search results with lexical + semantic scoring.
"""

import logging
import math
import re
from collections import Counter
from typing import Dict, List, Optional, TYPE_CHECKING

from opencortex.utils.similarity import cosine_similarity

if TYPE_CHECKING:
    from opencortex.skill_engine.types import SkillRecord

logger = logging.getLogger(__name__)

# BM25 parameters
BM25_K1 = 1.5
BM25_B = 0.75


class SkillRanker:
    """Hybrid BM25 + embedding re-ranker for skill candidates."""

    def __init__(self, embedding_adapter=None):
        self._embedding = embedding_adapter

    async def rank(
        self, query: str, candidates: List["SkillRecord"], top_k: int = 5,
    ) -> List["SkillRecord"]:
        """Re-rank candidates using BM25 + embedding similarity.

        Args:
            query: Search query text
            candidates: Pre-filtered candidates from Qdrant
            top_k: Number of results to return

        Returns:
            Re-ranked list of SkillRecords, best first
        """
        if not candidates:
            return []
        if len(candidates) <= 1:
            return candidates[:top_k]

        query_tokens = _tokenize(query)
        if not query_tokens:
            return candidates[:top_k]

        # Build BM25 corpus from candidates
        docs = []
        for c in candidates:
            text = f"{c.name} {c.description} {c.abstract} {c.content[:2000]}"
            docs.append(_tokenize(text))

        # Compute BM25 scores
        bm25_scores = _bm25_score(query_tokens, docs)

        # Normalize BM25 to [0, 1]
        max_bm25 = max(bm25_scores) if bm25_scores else 1.0
        if max_bm25 > 0:
            bm25_scores = [s / max_bm25 for s in bm25_scores]

        # Compute embedding similarity if adapter available
        embed_scores = [0.0] * len(candidates)
        if self._embedding:
            try:
                import asyncio
                loop = asyncio.get_running_loop()
                query_vec = await asyncio.wait_for(
                    loop.run_in_executor(None, self._embedding.embed, query),
                    timeout=2.0,
                )
                if hasattr(query_vec, 'dense_vector'):
                    query_vec = query_vec.dense_vector

                for i, c in enumerate(candidates):
                    doc_text = f"{c.name} {c.description} {c.abstract}"
                    doc_vec = await asyncio.wait_for(
                        loop.run_in_executor(None, self._embedding.embed, doc_text),
                        timeout=2.0,
                    )
                    if hasattr(doc_vec, 'dense_vector'):
                        doc_vec = doc_vec.dense_vector
                    embed_scores[i] = cosine_similarity(query_vec, doc_vec)
            except Exception as exc:
                logger.debug("[SkillRanker] Embedding scoring failed: %s", exc)

        # Combine: 0.4 * BM25 + 0.6 * embedding (semantic weighted higher)
        combined = []
        for i, c in enumerate(candidates):
            score = 0.4 * bm25_scores[i] + 0.6 * embed_scores[i]
            combined.append((score, i, c))

        combined.sort(key=lambda x: x[0], reverse=True)
        return [c for _, _, c in combined[:top_k]]


def _tokenize(text: str) -> List[str]:
    """Tokenize text for BM25. Handles English + Chinese."""
    text = text.lower()
    # English tokens
    english = re.findall(r'[a-z0-9_\-.]+', text)
    # Chinese unigrams
    chinese = re.findall(r'[\u4e00-\u9fa5]', text)
    return english + chinese


def _bm25_score(query_tokens: List[str], docs: List[List[str]]) -> List[float]:
    """Compute BM25 scores for query against documents."""
    n = len(docs)
    if n == 0:
        return []

    # Average document length
    avg_dl = sum(len(d) for d in docs) / n if n > 0 else 1

    # Document frequency for each term
    df: Dict[str, int] = Counter()
    for doc in docs:
        unique_terms = set(doc)
        for term in unique_terms:
            df[term] += 1

    scores = []
    for doc in docs:
        score = 0.0
        dl = len(doc)
        tf_counter = Counter(doc)

        for term in query_tokens:
            if term not in df:
                continue
            tf = tf_counter.get(term, 0)
            if tf == 0:
                continue

            # IDF: log((N - df + 0.5) / (df + 0.5) + 1)
            idf = math.log((n - df[term] + 0.5) / (df[term] + 0.5) + 1.0)

            # TF normalization
            tf_norm = (tf * (BM25_K1 + 1)) / (tf + BM25_K1 * (1 - BM25_B + BM25_B * dl / avg_dl))

            score += idf * tf_norm

        scores.append(score)

    return scores


