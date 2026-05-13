# SPDX-License-Identifier: Apache-2.0
"""LLM-assisted composition for reasoning recall results."""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel, Field, ValidationError

from opencortex.prompts.retrieval import (
    RECALL_COMPOSER_SYSTEM_PROMPT,
    build_recall_composition_prompt,
)
from opencortex.utils.facts import sorted_answerable_facts
from opencortex.utils.json_parse import parse_json_from_response
from opencortex.vector.retrieval.schemas import MatchedMemory, QueryType

logger = structlog.get_logger(__name__)


class CompositionOutput(BaseModel):
    """Validated LLM composition payload."""

    reasoning_chain: list[str] = Field(default_factory=list)
    supporting_uris: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class RecallComposer:
    """Attach optional reasoning diagnostics to matched memories."""

    def __init__(self, llm_completion: Any = None) -> None:
        self.llm_completion = llm_completion

    async def compose(
        self,
        *,
        query: str,
        query_type: QueryType,
        results: list[MatchedMemory],
    ) -> list[MatchedMemory]:
        """Return results with composition metadata when enabled."""
        if query_type not in {QueryType.REASONING, QueryType.MULTIHOP}:
            return results
        if self.llm_completion is None or not results:
            return results
        memories = [composition_memory(result) for result in results]
        try:
            response = await self.llm_completion(
                build_recall_composition_prompt(query, memories),
                system_prompt=RECALL_COMPOSER_SYSTEM_PROMPT,
            )
            output = CompositionOutput.model_validate(
                parse_json_from_response(response)
            )
        except (AttributeError, TypeError, ValueError, ValidationError) as exc:
            logger.warning(
                "recall_composer_failed",
                candidate_count=len(results),
                error_type=type(exc).__name__,
            )
            return results
        support = [uri for uri in output.supporting_uris if uri]
        payload = {
            "reasoning_chain": [
                str(item) for item in output.reasoning_chain if str(item).strip()
            ][:5],
            "supporting_uris": support[:10],
            "confidence": output.confidence,
        }
        return [with_composition(result, payload) for result in results]


def composition_memory(result: MatchedMemory) -> dict[str, object]:
    """Project one matched memory for composer input."""
    meta = dict(result.meta or {})
    fact_points = []
    abstract_json = meta.get("abstract_json")
    if isinstance(abstract_json, dict):
        fact_points = list(abstract_json.get("fact_points") or [])
    fact_points = sorted_answerable_facts(fact_points, limit=8)
    return {
        "uri": result.uri,
        "score": result.score,
        "surfaces": meta.get("retrieval_surfaces", []),
        "abstract": result.abstract,
        "overview": result.overview or "",
        "snippet": "; ".join(item.snippet for item in result.evidence if item.snippet),
        "fact_points": fact_points,
    }


def with_composition(
    result: MatchedMemory, composition: dict[str, Any]
) -> MatchedMemory:
    """Return a matched memory with optional composition metadata."""
    meta = dict(result.meta or {})
    meta["composition"] = dict(composition)
    return result.model_copy(update={"meta": meta})
