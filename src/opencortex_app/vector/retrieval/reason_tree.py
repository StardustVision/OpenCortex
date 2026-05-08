# SPDX-License-Identifier: Apache-2.0
"""LLM-assisted reason-tree entry selection."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from qdrant_client import models

from opencortex_app.core.identity import IdentityProfile
from opencortex_app.utils.json_parse import parse_json_from_response
from opencortex_app.vector.retrieval.filters import field_match, retrieval_filter
from opencortex_app.vector.retrieval.records import source_uri
from opencortex_app.vector.retrieval.schemas import (
    RetrievalHit,
    RetrievalPlan,
    RetrievalSurface,
)


class ReasonTreeSelection(BaseModel):
    """Reason-tree URI choices and records selected for retrieval."""

    selected_uris: list[str] = Field(default_factory=list)
    hits: list[RetrievalHit] = Field(default_factory=list)


class ReasonTreeRunner:
    """Select reason-tree entry URIs from existing index candidates."""

    def __init__(
        self,
        *,
        vector_store: Any,
        collection_resolver: Any,
        llm_completion: Any = None,
    ) -> None:
        self.vector_store = vector_store
        self.collection_resolver = collection_resolver
        self.llm_completion = llm_completion

    async def select(
        self,
        *,
        plan: RetrievalPlan,
        profile: IdentityProfile,
    ) -> ReasonTreeSelection:
        """Return LLM-selected reason-tree entry URIs."""
        if not plan.reason_tree.enabled or not plan.reason_tree.use_llm:
            return ReasonTreeSelection()
        if self.llm_completion is None:
            raise RuntimeError("Reason tree plan requires LLM completion")
        candidates = await self.candidates(plan=plan, profile=profile)
        if not candidates:
            return ReasonTreeSelection()
        response = await self.llm_completion(reason_tree_prompt(plan.query, candidates))
        parsed = parse_json_from_response(response)
        selected = normalize_selected_uris(parsed.get("selected_uris"), candidates)
        return ReasonTreeSelection(
            selected_uris=selected,
            hits=hits_from_selected_uris(selected, candidates),
        )

    async def candidates(
        self,
        *,
        plan: RetrievalPlan,
        profile: IdentityProfile,
    ) -> list[dict[str, Any]]:
        """Load bounded reason-tree candidates near planner entry points."""
        filters = reason_tree_filter(profile)
        uri_filters = reason_tree_uri_filters(plan.starting_uris)
        if uri_filters:
            filters.must = list(filters.must or []) + [uri_filters]
        records = await self.vector_store.filter(
            self.collection_resolver(),
            filters,
            limit=max(plan.reason_tree.max_nodes, 1),
        )
        return records[: plan.reason_tree.max_nodes]


def reason_tree_uri_filters(uris: list[str]) -> models.Filter | None:
    """Return a should filter for reason-tree source and tree URIs."""
    conditions: list[models.FieldCondition] = []
    for uri in uris:
        text = str(uri or "").strip()
        if not text:
            continue
        conditions.extend(
            [
                field_match("source_uri", text),
                field_match("tree_uri", text),
                field_match("parent_uri", text),
            ]
        )
    if not conditions:
        return None
    return models.Filter(
        should=conditions,
        min_should=models.MinShould(conditions=conditions, min_count=1),
    )


def reason_tree_filter(profile: IdentityProfile) -> models.Filter:
    """Return visibility filter for reason-tree projections."""
    return retrieval_filter(
        profile=profile,
        surface=RetrievalSurface.REASON_TREE_INDEX.value,
        tenant_key="source_tenant_id",
    )


def reason_tree_prompt(query: str, candidates: list[dict[str, Any]]) -> str:
    """Build the LLM prompt for reason-tree entry selection."""
    lines = [
        "Select the best reason-tree entry URIs for this recall query.",
        'Return JSON only: {"selected_uris":["uri1","uri2"]}.',
        "Use only URIs from the candidates. Prefer precise entries over broad ones.",
        "",
        f"Query: {query}",
        "",
        "Candidates:",
    ]
    for index, record in enumerate(candidates, start=1):
        uri = source_uri(record)
        context = str(record.get("context_window", "") or "")
        abstract = str(record.get("abstract") or record.get("overview") or "")
        lines.append(f"{index}. uri={uri} context={context} text={abstract}")
    return "\n".join(lines)


def normalize_selected_uris(value: Any, candidates: list[dict[str, Any]]) -> list[str]:
    """Return selected URIs constrained to candidate source URIs."""
    allowed = [source_uri(record) for record in candidates if source_uri(record)]
    requested: list[str] = []
    selected: list[str] = []
    if isinstance(value, list):
        for item in value:
            text = str(item or "").strip()
            if text:
                requested.append(text)
            if text in allowed and text not in selected:
                selected.append(text)
    invalid = [uri for uri in requested if uri not in allowed]
    if requested and invalid and not selected:
        raise ValueError("Reason tree selected no valid candidate URIs")
    return selected[:3]


def hits_from_selected_uris(
    selected_uris: list[str],
    candidates: list[dict[str, Any]],
) -> list[RetrievalHit]:
    """Return reason-tree hits selected by the LLM."""
    by_uri = {source_uri(record): record for record in candidates if source_uri(record)}
    hits: list[RetrievalHit] = []
    for uri in selected_uris:
        record = by_uri.get(uri)
        if record is None:
            continue
        hits.append(
            RetrievalHit(
                record=record,
                score=float(record.get("_score", record.get("score", 0.0)) or 0.0),
                surface=RetrievalSurface.REASON_TREE_INDEX,
                source_uri=uri,
            )
        )
    return hits
