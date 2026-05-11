# SPDX-License-Identifier: Apache-2.0
"""Memory retriever for opencortex."""

from __future__ import annotations

from typing import Any

from qdrant_client import models

from opencortex.core.identity import IdentityProfile
from opencortex.storage.cortex_storage import CortexStorage
from opencortex.vector.retrieval.cone import ConeExpander
from opencortex.vector.retrieval.executor import RetrievalExecutor
from opencortex.vector.retrieval.filters import field_match, retrieval_filter
from opencortex.vector.retrieval.planner import RetrievalPlanner
from opencortex.vector.retrieval.probe import RetrievalProbe
from opencortex.vector.retrieval.ranker import (
    RetrievalRanker,
    merge_primary_payload,
)
from opencortex.vector.retrieval.reason_tree import ReasonTreeRunner
from opencortex.vector.retrieval.schemas import (
    DetailLevel,
    MatchedMemory,
    RecallEvidence,
    RecallEvidenceKind,
    RecallSource,
    RetrievalRequest,
    RetrievalResponse,
    RetrievalSurface,
)


class MemoryRetriever:
    """Coordinate planner, Qdrant execution, ranking, and hydration."""

    def __init__(
        self,
        *,
        vector_store: Any,
        collection_resolver: Any,
        embedder: Any,
        cortex_storage: CortexStorage,
        llm_completion: Any = None,
    ) -> None:
        self.vector_store = vector_store
        self.collection_resolver = collection_resolver
        self.cortex_storage = cortex_storage
        self.probe = RetrievalProbe(
            vector_store=vector_store,
            collection_resolver=collection_resolver,
            embedder=embedder,
            llm_completion=llm_completion,
        )
        self.planner = RetrievalPlanner()
        self.executor = RetrievalExecutor(
            vector_store=vector_store,
            collection_resolver=collection_resolver,
        )
        self.reason_tree = ReasonTreeRunner(
            vector_store=vector_store,
            collection_resolver=collection_resolver,
            llm_completion=llm_completion,
        )
        self.cone_expander = ConeExpander(
            vector_store=vector_store,
            collection_resolver=collection_resolver,
            cortex_storage=cortex_storage,
        )
        self.ranker = RetrievalRanker()

    async def search(
        self,
        request: RetrievalRequest,
        *,
        profile: IdentityProfile,
    ) -> RetrievalResponse:
        """Run memory recall."""
        probe = await self.probe.probe(request, profile=profile)
        plan = self.planner.plan(request, probe=probe)
        reason_tree_selection = await self.reason_tree.select(
            plan=plan,
            profile=profile,
        )
        if reason_tree_selection.selected_uris:
            plan.starting_uris = unique_uris(
                [*reason_tree_selection.selected_uris, *plan.starting_uris]
            )
        raw_hits = [
            *await self.executor.execute(plan=plan, profile=profile),
            *reason_tree_selection.hits,
        ]
        ranked_hits = self.ranker.rank(raw_hits, plan=plan)
        cone_result = await self.cone_expander.expand(
            hits=ranked_hits,
            plan=plan,
            profile=profile,
        )
        if cone_result.hits:
            ranked_hits = self.ranker.rank([*raw_hits, *cone_result.hits], plan=plan)
        primary_records = await self.load_primary_records(
            ranked_hits,
            profile=profile,
        )
        results = [
            await self.to_matched_memory(
                merge_primary_payload(
                    hit=hit,
                    primary=primary_records.get(hit.source_uri),
                ),
                hit=hit,
                detail_level=plan.depth,
            )
            for hit in ranked_hits
        ]
        return RetrievalResponse(results=results, total=len(results))

    async def load_primary_records(
        self,
        hits: list[Any],
        *,
        profile: IdentityProfile,
    ) -> dict[str, dict[str, Any]]:
        """Load primary records for projection hits."""
        uris = sorted({hit.source_uri for hit in hits if hit.source_uri})
        if not uris:
            return {}
        uri_conditions = [field_match("uri", uri) for uri in uris]
        filters = retrieval_filter(profile=profile)
        filters.must = list(filters.must or []) + [
            models.Filter(
                should=uri_conditions,
                min_should=models.MinShould(
                    conditions=uri_conditions,
                    min_count=1,
                ),
            )
        ]
        records = await self.vector_store.filter(
            self.collection_resolver(),
            filters,
            limit=len(uris) + 5,
        )
        return {str(record.get("uri", "")): record for record in records}

    async def to_matched_memory(
        self,
        record: dict[str, Any],
        *,
        hit: Any,
        detail_level: DetailLevel,
    ) -> MatchedMemory:
        """Project one primary payload to API result."""
        uri = str(record.get("uri", "") or "")
        overview = None
        content = None
        if detail_level in {DetailLevel.L1, DetailLevel.L2}:
            overview = str(record.get("overview", "") or "") or None
            if not overview and uri:
                overview = await self.read_optional(f"{uri}/.overview.md")
        if detail_level == DetailLevel.L2:
            content = str(record.get("content", "") or "") or None
            if not content and uri:
                content = await self.read_optional(f"{uri}/content.md")
        return MatchedMemory(
            uri=uri,
            type=str(record.get("context_type", "") or ""),
            category=str(record.get("category", "") or ""),
            abstract=str(record.get("abstract", "") or ""),
            overview=overview,
            content=content,
            score=float(record.get("_final_score", record.get("_score", 0.0)) or 0.0),
            session_id=str(record.get("session_id", "") or ""),
            source=recall_source(record, uri=uri),
            evidence=recall_evidence(record, hit=hit),
            entities=list(record.get("entities") or []),
            keywords=str(record.get("keywords", "") or ""),
            meta=dict(record.get("meta") or {}),
        )

    async def read_optional(self, uri: str) -> str | None:
        """Read a CFS text file if it exists."""
        try:
            return await self.cortex_storage.read_file(uri)
        except FileNotFoundError:
            return None


def unique_uris(uris: list[str]) -> list[str]:
    """Return non-empty unique URIs preserving order."""
    values: list[str] = []
    for uri in uris:
        text = str(uri or "").strip()
        if text and text not in values:
            values.append(text)
    return values


def recall_source(record: dict[str, Any], *, uri: str) -> RecallSource:
    """Build business source metadata for a recall result."""
    meta = dict(record.get("meta") or {})
    return RecallSource(
        uri=uri,
        primary_uri=uri,
        session_id=str(
            record.get("session_id", "") or meta.get("session_id", "") or ""
        ),
        document_id=record.get("source_doc_id") or meta.get("source_doc_id"),
        title=str(
            record.get("source_doc_title")
            or meta.get("source_doc_title")
            or meta.get("title")
            or ""
        ),
        section=str(
            record.get("source_section_path") or meta.get("source_section_path") or ""
        ),
    )


def recall_evidence(record: dict[str, Any], *, hit: Any) -> list[RecallEvidence]:
    """Build answer evidence without exposing retrieval internals."""
    surfaces = list(record.get("_retrieval_surfaces") or [])
    if not surfaces:
        surfaces = [str(getattr(getattr(hit, "surface", ""), "value", "") or "match")]
    score = float(record.get("_final_score", record.get("_score", 0.0)) or 0.0)
    snippet = evidence_snippet(record)
    uri = str(record.get("uri", "") or getattr(hit, "source_uri", "") or "")
    return [
        RecallEvidence(
            uri=uri,
            kind=business_evidence_kind(surface),
            score=score,
            snippet=snippet,
        )
        for surface in surfaces
    ]


def evidence_snippet(record: dict[str, Any]) -> str:
    """Return the best short text evidence for a recall result."""
    for key in ("abstract", "overview", "content"):
        value = str(record.get(key, "") or "").strip()
        if value:
            return value
    return ""


def business_evidence_kind(surface: str) -> RecallEvidenceKind:
    """Map physical retrieval surfaces to business-facing evidence kinds."""
    try:
        retrieval_surface = RetrievalSurface(surface)
    except ValueError:
        return RecallEvidenceKind.MATCH
    return {
        RetrievalSurface.L0_OBJECT: RecallEvidenceKind.MEMORY,
        RetrievalSurface.ANCHOR_INDEX: RecallEvidenceKind.TOPIC,
        RetrievalSurface.FACT_INDEX: RecallEvidenceKind.FACT,
        RetrievalSurface.ENTITY_INDEX: RecallEvidenceKind.ENTITY,
        RetrievalSurface.REASON_TREE_INDEX: RecallEvidenceKind.SUMMARY,
    }.get(retrieval_surface, RecallEvidenceKind.MATCH)
