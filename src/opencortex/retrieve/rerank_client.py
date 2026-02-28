# SPDX-License-Identifier: Apache-2.0
"""
Rerank client for OpenCortex retrieval pipeline.

Provides cross-encoder or LLM-based reranking for query-document relevance scoring.
Supports three modes:
1. API mode — dedicated Rerank API (Volcengine/Jina/Cohere compatible)
2. LLM mode — use LLM completion as listwise reranker (fallback)
3. Disabled — returns zero scores
"""

import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from opencortex.retrieve.rerank_config import RerankConfig

logger = logging.getLogger(__name__)

LLMCompletionCallable = Callable[[str], Awaitable[str]]


class RerankClient:
    """Cross-encoder reranker for query-document relevance scoring.

    Args:
        config: RerankConfig with API credentials and settings.
        llm_completion: Async callable for LLM-based reranking fallback.
    """

    def __init__(
        self,
        config: RerankConfig,
        llm_completion: Optional[LLMCompletionCallable] = None,
    ):
        self._config = config
        self._llm_completion = llm_completion
        self._mode = self._detect_mode()
        # Reusable HTTP client for connection pooling (lazy-created on first API call)
        self._http_client: Optional[Any] = None
        logger.info("[RerankClient] Initialized in '%s' mode", self._mode)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def fusion_beta(self) -> float:
        return self._config.fusion_beta

    def _detect_mode(self) -> str:
        """Detect rerank mode based on available configuration."""
        if self._config.model and self._config.api_key:
            return "api"
        if self._config.use_llm_fallback and self._llm_completion:
            return "llm"
        return "disabled"

    async def rerank(self, query: str, documents: List[str]) -> List[float]:
        """Score each document against query, return scores in same order.

        Args:
            query: Search query text.
            documents: List of document texts to score.

        Returns:
            List of relevance scores (0-1) in the same order as documents.
        """
        if not documents:
            return []

        # Limit candidates for cost control
        max_k = self._config.max_candidates
        truncated = documents[:max_k]

        if self._mode == "api":
            scores = await self._rerank_via_api(query, truncated)
        elif self._mode == "llm":
            scores = await self._rerank_via_llm(query, truncated)
        else:
            scores = [0.0] * len(truncated)

        # Pad with zeros for any documents beyond max_candidates
        if len(documents) > max_k:
            scores.extend([0.0] * (len(documents) - max_k))

        return scores

    def _get_http_client(self):
        """Return a reusable httpx.AsyncClient (lazy-created)."""
        if self._http_client is None:
            import httpx
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    async def _rerank_via_api(self, query: str, documents: List[str]) -> List[float]:
        """Call Rerank API (Volcengine/Jina/Cohere compatible).

        Expected API format:
            POST /rerank
            body: {"query": ..., "documents": [...], "model": ...}
            response: {"results": [{"index": 0, "relevance_score": 0.95}, ...]}
        """
        try:
            api_base = self._config.api_base.rstrip("/")
            url = f"{api_base}/rerank"
            headers = {
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            }
            payload: Dict[str, Any] = {
                "query": query,
                "documents": documents,
                "model": self._config.model,
            }

            client = self._get_http_client()
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

            # Parse results — API returns [{index, relevance_score}]
            results = data.get("results", [])
            scores = [0.0] * len(documents)
            for item in results:
                idx = item.get("index", 0)
                score = item.get("relevance_score", 0.0)
                if 0 <= idx < len(scores):
                    scores[idx] = float(score)

            return scores

        except Exception as exc:
            logger.warning("[RerankClient] API rerank failed: %s — falling back", exc)
            if self._llm_completion and self._config.use_llm_fallback:
                return await self._rerank_via_llm(query, documents)
            return [0.0] * len(documents)

    async def _rerank_via_llm(self, query: str, documents: List[str]) -> List[float]:
        """Use LLM as a listwise reranker (fallback when no dedicated API)."""
        if not self._llm_completion:
            return [0.0] * len(documents)

        try:
            prompt = self._build_rerank_prompt(query, documents)
            response = await self._llm_completion(prompt)
            scores = self._parse_rerank_response(response, len(documents))
            return scores
        except Exception as exc:
            logger.warning("[RerankClient] LLM rerank failed: %s", exc)
            return [0.0] * len(documents)

    def _build_rerank_prompt(self, query: str, documents: List[str]) -> str:
        """Build LLM prompt for listwise reranking."""
        docs_text = "\n".join(
            f"[{i}] {doc[:500]}" for i, doc in enumerate(documents)
        )
        return (
            "You are a relevance scoring system. "
            "Score each document's relevance to the query on a scale of 0.0 to 1.0.\n\n"
            f"Query: {query}\n\n"
            f"Documents:\n{docs_text}\n\n"
            "Return ONLY a JSON array of scores in the same order as the documents. "
            "Example: [0.95, 0.3, 0.8]\n"
            "Scores:"
        )

    def _parse_rerank_response(self, response: str, expected_count: int) -> List[float]:
        """Parse LLM response into a list of float scores."""
        # Try direct JSON parse
        try:
            scores = json.loads(response.strip())
            if isinstance(scores, list):
                return self._normalize_scores(scores, expected_count)
        except json.JSONDecodeError:
            pass

        # Try extracting JSON array from response
        match = re.search(r"\[[\d.,\s]+\]", response)
        if match:
            try:
                scores = json.loads(match.group())
                if isinstance(scores, list):
                    return self._normalize_scores(scores, expected_count)
            except json.JSONDecodeError:
                pass

        # Fallback: extract individual floats
        floats = re.findall(r"\d+\.?\d*", response)
        if len(floats) >= expected_count:
            scores = [float(f) for f in floats[:expected_count]]
            return self._normalize_scores(scores, expected_count)

        return [0.0] * expected_count

    def _normalize_scores(self, scores: List[Any], expected_count: int) -> List[float]:
        """Normalize scores to [0, 1] range and pad/truncate to expected count."""
        result = []
        for s in scores[:expected_count]:
            try:
                v = float(s)
                result.append(max(0.0, min(1.0, v)))
            except (ValueError, TypeError):
                result.append(0.0)

        # Pad if too few
        while len(result) < expected_count:
            result.append(0.0)

        return result
