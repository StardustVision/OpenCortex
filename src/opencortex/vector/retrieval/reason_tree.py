# SPDX-License-Identifier: Apache-2.0
"""LLM-assisted reason-tree entry selection."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from qdrant_client import models

from opencortex.core.identity import IdentityProfile
from opencortex.prompts.retrieval import (
    REASON_TREE_SELECTION_SYSTEM_PROMPT,
    build_reason_tree_selection_prompt,
)
from opencortex.prompts.schemas import (
    ReasonTreeSelectionOutput,
    ReasonTreeSource,
)
from opencortex.utils.json_parse import parse_json_from_response
from opencortex.vector.retrieval.filters import field_match, retrieval_filter
from opencortex.vector.retrieval.records import source_uri
from opencortex.vector.retrieval.schemas import (
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
        response = await self.llm_completion(
            reason_tree_prompt(plan.query, candidates),
            system_prompt=REASON_TREE_SELECTION_SYSTEM_PROMPT,
        )
        parsed = parse_json_from_response(response)
        output = ReasonTreeSelectionOutput.model_validate(parsed)
        selected = normalize_selected_uris(output.selected_uris, candidates)
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
    sources = [reason_tree_source(record) for record in candidates]
    return build_reason_tree_selection_prompt(query, sources)


def reason_tree_source(record: dict[str, Any]) -> ReasonTreeSource:
    """Normalize one vector payload for the reason-tree selection prompt."""
    meta = dict(record.get("meta") or {})
    fact_points = list(record.get("fact_points") or meta.get("fact_points") or [])
    source_refs = list(record.get("source_refs") or meta.get("source_refs") or [])
    title = str(
        record.get("title")
        or meta.get("title")
        or meta.get("section_title")
        or record.get("abstract")
        or ""
    )
    summary = str(
        record.get("summary") or record.get("overview") or record.get("abstract") or ""
    )
    return ReasonTreeSource(
        uri=str(record.get("uri") or source_uri(record)),
        title=title,
        summary=summary,
        fact_points=[str(item) for item in fact_points if str(item).strip()],
        source_refs=[str(item) for item in source_refs if str(item).strip()],
        context_window=str(record.get("context_window", "") or ""),
    )


def normalize_selected_uris(value: Any, candidates: list[dict[str, Any]]) -> list[str]:
    """Return selected URIs constrained to candidate source URIs."""
    allowed = [
        str(record.get("uri") or source_uri(record))
        for record in candidates
        if str(record.get("uri") or source_uri(record))
    ]
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
    by_uri = {
        str(record.get("uri") or source_uri(record)): record
        for record in candidates
        if str(record.get("uri") or source_uri(record))
    }
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
                source_uri=source_uri(record),
            )
        )
    return hits
