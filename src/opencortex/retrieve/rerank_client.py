# SPDX-License-Identifier: Apache-2.0
"""
Rerank client for OpenCortex retrieval pipeline.

Provides cross-encoder or LLM-based reranking for query-document relevance scoring.
Supports three modes:
1. API mode — dedicated Rerank API (Jina/Cohere compatible)
2. LLM mode — use LLM completion as listwise reranker (fallback)
3. Disabled — returns zero scores
"""

import orjson as json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from opencortex.prompts import build_rerank_prompt
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
        self._local_reranker = None
        self._mode = self._detect_mode()
        # Reusable HTTP client for connection pooling (lazy-created on first API call)
        self._http_client: Optional[Any] = None
        # Initialize local reranker if mode is "local"
        self._init_local_reranker()
        logger.info("[RerankClient] Initialized in '%s' mode", self._mode)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def fusion_beta(self) -> float:
        return self._config.fusion_beta

    def _detect_mode(self) -> str:
        """Detect rerank mode based on available configuration."""
        if self._config.provider == "local":
            return "local"
        if self._config.model and self._config.api_key:
            return "api"
        if self._config.use_llm_fallback and self._llm_completion:
            return "llm"
        return "disabled"

    def _init_local_reranker(self) -> None:
        """Initialize FastEmbed TextCrossEncoder for local reranking."""
        if self._mode != "local":
            return
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            model_name = self._config.model or "jinaai/jina-reranker-v2-base-multilingual"
            self._local_reranker = TextCrossEncoder(model_name=model_name)
            logger.info(
                "[RerankClient] Loaded local reranker: %s", model_name,
            )
        except ImportError:
            logger.warning(
                "[RerankClient] fastembed not installed — "
                "local rerank disabled. Install with: uv add fastembed"
            )
            self._local_reranker = None
            self._fallback_to_llm_or_disable("fastembed not installed")
        except Exception as exc:
            logger.warning(
                "[RerankClient] Failed to load local reranker: %s", exc,
            )
            self._local_reranker = None
            self._fallback_to_llm_or_disable(str(exc))

    def _fallback_to_llm_or_disable(self, reason: str) -> None:
        """Fall back to LLM reranking when the primary mode fails to init."""
        if self._llm_completion and self._config.use_llm_fallback:
            self._mode = "llm"
            logger.info(
                "[RerankClient] Falling back to LLM reranker (%s)", reason,
            )
        else:
            self._mode = "disabled"

    async def rerank(self, query: str, documents: List[str]) -> List[float]:
        """Score each document against query, return scores in same order.

        When there are more documents than max_candidates, scores them in
        batches so every document gets a relevance score (instead of giving
        0 to overflow candidates).

        Args:
            query: Search query text.
            documents: List of document texts to score.

        Returns:
            List of relevance scores (0-1) in the same order as documents.
        """
        if not documents:
            return []

        max_k = self._config.max_candidates or len(documents)

        if len(documents) <= max_k:
            return await self._rerank_batch(query, documents)

        # Sliding window: score all documents in batches of max_k
        import asyncio
        scores = [0.0] * len(documents)
        batches = []
        for start in range(0, len(documents), max_k):
            batch = documents[start:start + max_k]
            batches.append((start, batch))

        # Run batches concurrently for local/API modes, sequentially for LLM
        # (to avoid overwhelming the LLM endpoint)
        if self._mode == "llm":
            for start, batch in batches:
                batch_scores = await self._rerank_batch(query, batch)
                for i, s in enumerate(batch_scores):
                    scores[start + i] = s
        else:
            coros = [self._rerank_batch(query, batch) for _, batch in batches]
            results = await asyncio.gather(*coros)
            for (start, batch), batch_scores in zip(batches, results):
                for i, s in enumerate(batch_scores):
                    scores[start + i] = s

        return scores

    async def _rerank_batch(self, query: str, documents: List[str]) -> List[float]:
        """Score a single batch of documents."""
        if self._mode == "local":
            return await self._rerank_via_local(query, documents)
        elif self._mode == "api":
            return await self._rerank_via_api(query, documents)
        elif self._mode == "llm":
            return await self._rerank_via_llm(query, documents)
        return [0.0] * len(documents)

    async def _rerank_via_local(self, query: str, documents: List[str]) -> List[float]:
        """Score documents using local FastEmbed cross-encoder."""
        if not self._local_reranker:
            return [0.0] * len(documents)
        try:
            import asyncio
            import math
            loop = asyncio.get_running_loop()
            # TextCrossEncoder.rerank is sync — run in thread pool
            results = await loop.run_in_executor(
                None,
                lambda: list(self._local_reranker.rerank(query, documents)),
            )
            # results is list of dicts with 'score' and 'index'
            # Scores are raw logits (can be negative) — apply sigmoid to normalize to [0,1]
            scores = [0.0] * len(documents)
            for item in results:
                idx = item.get("index", 0) if isinstance(item, dict) else 0
                raw = item.get("score", 0.0) if isinstance(item, dict) else float(item)
                if 0 <= idx < len(scores):
                    scores[idx] = 1.0 / (1.0 + math.exp(-float(raw)))
            return scores
        except Exception as exc:
            logger.warning("[RerankClient] Local rerank failed: %s", exc)
            if self._llm_completion and self._config.use_llm_fallback:
                return await self._rerank_via_llm(query, documents)
            return [0.0] * len(documents)

    def _get_http_client(self):
        """Return a reusable httpx.AsyncClient (lazy-created)."""
        if self._http_client is None:
            import httpx
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    async def _rerank_via_api(self, query: str, documents: List[str]) -> List[float]:
        """Call Rerank API (Jina/Cohere compatible).

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
        return build_rerank_prompt(query, docs_text)

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
