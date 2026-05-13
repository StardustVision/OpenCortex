# SPDX-License-Identifier: Apache-2.0
"""API-backed rerank for fused recall candidates."""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx
import structlog
from pydantic import BaseModel, Field, ValidationError

from opencortex.prompts.retrieval import (
    RECALL_RERANK_SYSTEM_PROMPT,
    build_recall_rerank_prompt,
)
from opencortex.vector.retrieval.schemas import RetrievalHit

logger = structlog.get_logger(__name__)


class RerankClient(Protocol):
    """Provider-neutral rerank client."""

    async def rerank_batch(
        self,
        query: str,
        documents: list[str],
    ) -> list[float] | None:
        """Return one relevance score per document, or None on failure."""


class RerankScore(BaseModel):
    """One LLM rerank score."""

    uri: str
    score: float = Field(ge=0.0, le=1.0)


class RerankScores(BaseModel):
    """Validated LLM rerank output."""

    scores: list[RerankScore] = Field(default_factory=list)


class RecallReranker:
    """Rerank fused candidates without changing public response shape."""

    def __init__(
        self,
        *,
        client: RerankClient | None = None,
        enabled: bool = True,
        seed_limit: int = 30,
        final_limit: int = 30,
    ) -> None:
        self.client = client
        self.enabled = enabled
        self.seed_limit = seed_limit
        self.final_limit = final_limit

    async def rerank(
        self,
        query: str,
        hits: list[RetrievalHit],
        *,
        limit: int,
    ) -> list[RetrievalHit]:
        """Return provider-ranked hits, falling back to input order on failure."""
        if not self.enabled or self.client is None or len(hits) <= 1:
            return hits
        candidates = hits[: max(limit, 1)]
        documents = [rerank_text(hit) for hit in candidates]
        scores = await self.client.rerank_batch(query, documents)
        if not valid_scores(scores, len(documents)):
            logger.warning(
                "recall_rerank_fallback",
                provider=type(self.client).__name__,
                candidate_count=len(candidates),
                error_type="invalid_scores",
            )
            return hits
        scored = [
            apply_rerank_score(hit, score)
            for hit, score in zip(candidates, scores or [], strict=True)
        ]
        scored.sort(
            key=lambda hit: (
                float(hit.record.get("_rerank_score", 0.0) or 0.0),
                hit.score,
            ),
            reverse=True,
        )
        return [*scored, *hits[len(candidates) :]]


class LLMRerankClient:
    """Prompt-based rerank through the configured LLM API."""

    def __init__(self, llm_completion: Any) -> None:
        self.llm_completion = llm_completion

    async def rerank_batch(
        self,
        query: str,
        documents: list[str],
    ) -> list[float] | None:
        """Return one LLM relevance score per document."""
        candidates = [
            {"uri": str(index), "type": "", "title": "", "section": "", "text": text}
            for index, text in enumerate(documents)
        ]
        prompt = build_recall_rerank_prompt(query, candidates)
        try:
            raw = await self.llm_completion.complete(
                prompt,
                temperature=0.0,
                system_prompt=RECALL_RERANK_SYSTEM_PROMPT,
            )
            scores = parse_rerank_scores(raw)
        except (
            AttributeError,
            TypeError,
            ValueError,
            ValidationError,
            json.JSONDecodeError,
        ) as exc:
            logger.warning(
                "recall_rerank_fallback",
                provider="llm",
                candidate_count=len(documents),
                error_type=type(exc).__name__,
            )
            return None
        values: list[float] = []
        for index in range(len(documents)):
            score = scores.get(str(index))
            if score is None:
                return None
            values.append(score)
        return values


class LiteLLMRerankClient:
    """LiteLLM rerank API client."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        api_base: str = "",
    ) -> None:
        self.model = model
        self.api_key = api_key or None
        self.api_base = api_base or None

    async def rerank_batch(
        self,
        query: str,
        documents: list[str],
    ) -> list[float] | None:
        """Return LiteLLM relevance scores in input order."""
        if not self.model or not documents:
            return None
        try:
            import litellm

            response = await litellm.arerank(
                model=self.model,
                query=query,
                documents=documents,
                api_key=self.api_key,
                api_base=self.api_base,
                return_documents=False,
            )
        except Exception as exc:
            logger.warning(
                "recall_rerank_fallback",
                provider="litellm",
                candidate_count=len(documents),
                error_type=type(exc).__name__,
            )
            return None
        return scores_from_results(getattr(response, "results", None), len(documents))


class HostedVLLMRerankClient:
    """LiteLLM-hosted vLLM rerank client."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        api_base: str = "",
    ) -> None:
        self.model = model
        self.api_key = api_key or None
        self.api_base = api_base or None

    async def rerank_batch(
        self,
        query: str,
        documents: list[str],
    ) -> list[float] | None:
        """Return hosted-vLLM rerank scores in input order."""
        if not self.model or not self.api_base or not documents:
            return None
        try:
            import litellm

            response = await litellm.arerank(
                model=hosted_vllm_model(self.model),
                query=query,
                documents=documents,
                custom_llm_provider="hosted_vllm",
                api_key=self.api_key,
                api_base=self.api_base,
                top_n=len(documents),
                return_documents=False,
            )
        except Exception as exc:
            logger.warning(
                "recall_rerank_fallback",
                provider="hosted_vllm",
                candidate_count=len(documents),
                error_type=type(exc).__name__,
            )
            return None
        return scores_from_results(getattr(response, "results", None), len(documents))


class OpenAIRerankClient:
    """OpenAI-compatible rerank HTTP client."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        api_base: str = "",
    ) -> None:
        self.model = model
        self.api_key = api_key or "opencortex-local"
        self.api_base = api_base.rstrip("/")

    async def rerank_batch(
        self,
        query: str,
        documents: list[str],
    ) -> list[float] | None:
        """Return /rerank relevance scores in input order."""
        if not self.model or not self.api_base or not documents:
            return None
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    rerank_url(self.api_base),
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "accept": "application/json",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "query": query,
                        "documents": documents,
                        "top_n": len(documents),
                        "return_documents": False,
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError:
            return None
        return scores_from_results(response.json().get("results"), len(documents))


def scores_from_results(results: Any, expected: int) -> list[float] | None:
    """Project provider results to scores in original document order."""
    if not results or len(results) != expected:
        return None
    scores = [0.0] * expected
    for item in results:
        index = result_value(item, "index")
        score = result_value(item, "relevance_score")
        if index is None or score is None:
            return None
        index_value = int(index)
        if not 0 <= index_value < expected:
            return None
        scores[index_value] = float(score)
    return scores


def result_value(item: Any, key: str) -> Any:
    """Return a value from dict-like or object-like provider result."""
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def hosted_vllm_model(model: str) -> str:
    """Return LiteLLM's hosted_vllm model name."""
    return model if model.startswith("hosted_vllm/") else f"hosted_vllm/{model}"


def rerank_url(api_base: str) -> str:
    """Return the rerank endpoint for an OpenAI-compatible base URL."""
    base = api_base.rstrip("/")
    if base.endswith("/v1/rerank") or base.endswith("/rerank"):
        return base
    return f"{base}/rerank"


def build_rerank_client(
    *,
    provider: str,
    llm_completion: Any = None,
    model: str = "",
    api_key: str = "",
    api_base: str = "",
    default_model: str = "",
) -> RerankClient | None:
    """Build a rerank client for the configured API provider."""
    if provider == "llm":
        return LLMRerankClient(llm_completion) if llm_completion is not None else None
    if provider == "litellm":
        resolved_model = model or default_model
        return LiteLLMRerankClient(
            model=resolved_model,
            api_key=api_key,
            api_base=api_base,
        )
    if provider == "hosted_vllm":
        resolved_model = model or default_model
        return HostedVLLMRerankClient(
            model=resolved_model,
            api_key=api_key,
            api_base=api_base,
        )
    if provider == "openai":
        resolved_model = model or default_model
        return OpenAIRerankClient(
            model=resolved_model,
            api_key=api_key,
            api_base=api_base,
        )
    return None


def parse_rerank_scores(text: str) -> dict[str, float]:
    """Parse and validate LLM rerank JSON."""
    parsed = RerankScores.model_validate_json(extract_json(text))
    scores: dict[str, float] = {}
    for item in parsed.scores:
        uri = item.uri.strip()
        if uri:
            scores[uri] = item.score
    return scores


def extract_json(text: str) -> str:
    """Return a JSON object from a plain or fenced model response."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("rerank response missing JSON object")
    return stripped[start : end + 1]


def valid_scores(scores: list[float] | None, expected: int) -> bool:
    """Return whether a provider produced a complete score list."""
    return scores is not None and len(scores) == expected


def apply_rerank_score(hit: RetrievalHit, score: float) -> RetrievalHit:
    """Return hit with an internal rerank score."""
    record = dict(hit.record)
    record["_rerank_score"] = score
    record["_pre_rerank_score"] = hit.score
    return RetrievalHit(
        record=record,
        score=score,
        surface=hit.surface,
        source_uri=hit.source_uri,
        path_cost=hit.path_cost,
    )


def rerank_text(hit: RetrievalHit) -> str:
    """Project one hit to business text for reranking."""
    record = hit.record
    meta = dict(record.get("meta") or {})
    entities = ", ".join(str(item) for item in record.get("entities") or [])
    abstract_json = record.get("abstract_json")
    fact_points = []
    if isinstance(abstract_json, dict):
        fact_points.extend(abstract_json.get("fact_points") or [])
    fact_points.extend(record.get("fact_points") or meta.get("fact_points") or [])
    text_parts = [
        f"type={record.get('context_type', '')}",
        f"title={record.get('source_doc_title') or meta.get('title') or ''}",
        f"section={record.get('source_section_path') or ''}",
        str(record.get("abstract", "") or ""),
        str(record.get("overview", "") or ""),
        str(record.get("content", "") or ""),
        " ".join(str(fact) for fact in fact_points if str(fact).strip()),
        str(record.get("keywords", "") or ""),
        entities,
    ]
    return " ".join("\n".join(text_parts).split())
