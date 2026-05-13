# SPDX-License-Identifier: Apache-2.0
"""Bootstrap probe for opencortex recall."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from opencortex.core.identity import IdentityProfile
from opencortex.prompts.retrieval import (
    QUERY_DECOMPOSITION_SYSTEM_PROMPT,
    build_query_decomposition_prompt,
)
from opencortex.prompts.schemas import QueryDecompositionOutput
from opencortex.utils.json_parse import parse_json_from_response
from opencortex.vector.retrieval.filters import retrieval_filter
from opencortex.vector.retrieval.records import record_score, source_uri
from opencortex.vector.retrieval.schemas import (
    ProbeCandidateEvidence,
    ProbeEvidence,
    QuerySize,
    QueryType,
    RetrievalProbeResult,
    RetrievalRequest,
    RetrievalSurface,
)


class RetrievalProbe:
    """Run a bounded first pass over cheap retrieval surfaces."""

    top_k = 3
    max_retrieval_queries = 4
    max_retrieval_query_chars = 80
    locator_surfaces = (
        RetrievalSurface.ANCHOR_INDEX,
        RetrievalSurface.ENTITY_INDEX,
        RetrievalSurface.REASON_TREE_INDEX,
    )

    def __init__(
        self,
        *,
        vector_store: Any,
        collection_resolver: Any,
        embedder: Any,
        llm_completion: Any = None,
    ) -> None:
        self.vector_store = vector_store
        self.collection_resolver = collection_resolver
        self.embedder = embedder
        self.llm_completion = llm_completion

    async def probe(
        self,
        request: RetrievalRequest,
        *,
        profile: IdentityProfile,
    ) -> RetrievalProbeResult:
        """Run the probe and return evidence for planning."""
        query = request.query.strip()
        query_size = query_size_for(query)
        query_type = query_type_for(query)
        retrieval_queries, planned_query_type = await self.prepare_queries(
            query,
            query_size=query_size,
        )
        if planned_query_type is not None:
            query_type = planned_query_type
        collection = self.collection_resolver()
        probe_results = await asyncio.gather(
            *[
                self.probe_query(
                    collection=collection,
                    retrieval_query=retrieval_query,
                    profile=profile,
                )
                for retrieval_query in retrieval_queries
            ]
        )
        object_records = top_records(
            [record for _, records, _ in probe_results for record in records],
            limit=self.top_k,
        )
        locator_records = top_records(
            [record for _, _, records in probe_results for record in records],
            limit=self.top_k,
        )
        search_vectors = [vector for vector, _, _ in probe_results if vector]
        return self.build_result(
            object_records=object_records,
            locator_records=locator_records,
            search_vectors=search_vectors,
            query_type=query_type,
        )

    async def prepare_queries(
        self,
        query: str,
        *,
        query_size: QuerySize,
    ) -> tuple[list[str], QueryType | None]:
        """Return retrieval queries used by this probe."""
        if query_size != QuerySize.LARGE:
            return [query], None
        if self.llm_completion is None:
            raise RuntimeError("Large recall query requires LLM query planning")
        response = await self.llm_completion(
            build_query_decomposition_prompt(
                query,
                max_queries=self.max_retrieval_queries,
                max_chars=self.max_retrieval_query_chars,
            ),
            system_prompt=QUERY_DECOMPOSITION_SYSTEM_PROMPT,
        )
        parsed = parse_json_from_response(response)
        output = QueryDecompositionOutput.model_validate(parsed)
        queries = normalize_retrieval_queries(output.retrieval_queries)
        if not queries:
            raise ValueError("LLM query planning returned no retrieval_queries")
        return queries, normalized_query_type(output.query_type)

    async def probe_query(
        self,
        *,
        collection: str,
        retrieval_query: str,
        profile: IdentityProfile,
    ) -> tuple[list[float], list[dict[str, Any]], list[dict[str, Any]]]:
        """Run direct and locator probes for one retrieval query."""
        query_vector = await self.aembed_query(retrieval_query)
        object_records, locator_records = await asyncio.gather(
            self.search_surface(
                collection=collection,
                surface=RetrievalSurface.L0_OBJECT,
                query_vector=query_vector,
                profile=profile,
            ),
            self.search_locator_surface(
                collection=collection,
                query_vector=query_vector,
                profile=profile,
            ),
        )
        return query_vector, object_records, locator_records

    async def search_surface(
        self,
        *,
        collection: str,
        surface: RetrievalSurface,
        query_vector: list[float],
        profile: IdentityProfile,
    ) -> list[dict[str, Any]]:
        """Search one probe surface."""
        return await self.vector_store.search(
            collection,
            query_vector=query_vector,
            filters=retrieval_filter(
                profile=profile,
                surface=surface.value,
            ),
            limit=self.top_k,
        )

    async def search_locator_surface(
        self,
        *,
        collection: str,
        query_vector: list[float],
        profile: IdentityProfile,
    ) -> list[dict[str, Any]]:
        """Search secondary indexes that locate retrieval entry points."""
        records = await asyncio.gather(
            *[
                self.search_surface(
                    collection=collection,
                    surface=surface,
                    query_vector=query_vector,
                    profile=profile,
                )
                for surface in self.locator_surfaces
            ]
        )
        return top_records(
            [record for surface_records in records for record in surface_records],
            limit=self.top_k,
        )

    def build_result(
        self,
        *,
        object_records: list[dict[str, Any]],
        locator_records: list[dict[str, Any]],
        search_vectors: list[list[float]],
        query_type: QueryType = QueryType.FACTUAL,
    ) -> RetrievalProbeResult:
        """Build a planner-friendly probe result."""
        records = [*object_records, *locator_records]
        candidates = self.candidates(records)
        scores = sorted((candidate.score for candidate in candidates), reverse=True)
        object_scores = sorted(
            (record_score(record) for record in object_records), reverse=True
        )
        locator_scores = sorted(
            (record_score(record) for record in locator_records), reverse=True
        )
        starting_uris = starting_uris_from_locator_hits(
            direct_records=object_records,
            locator_records=locator_records,
        )
        return RetrievalProbeResult(
            query_type=query_type,
            starting_uris=starting_uris,
            search_vectors=search_vectors,
            evidence=ProbeEvidence(
                top_score=scores[0] if scores else None,
                score_gap=round(scores[0] - scores[1], 4) if len(scores) >= 2 else None,
                object_top_score=object_scores[0] if object_scores else None,
                locator_top_score=locator_scores[0] if locator_scores else None,
                candidate_count=len(candidates),
                object_candidate_count=len(object_records),
                locator_candidate_count=len(locator_records),
            ),
        )

    @staticmethod
    def candidates(records: list[dict[str, Any]]) -> list[ProbeCandidateEvidence]:
        """Collapse probe records by source URI."""
        by_uri: dict[str, ProbeCandidateEvidence] = {}
        for record in records:
            uri = source_uri(record)
            if not uri:
                continue
            score = record_score(record)
            existing = by_uri.get(uri)
            anchors = candidate_anchors(record)
            if existing is None:
                by_uri[uri] = ProbeCandidateEvidence(
                    uri=uri,
                    score=score,
                    entities=[str(entity) for entity in record.get("entities") or []],
                    anchors=anchors,
                )
                continue
            existing.score = max(existing.score, score)
            for anchor in anchors:
                if anchor not in existing.anchors:
                    existing.anchors.append(anchor)
            for entity in record.get("entities") or []:
                text = str(entity)
                if text and text not in existing.entities:
                    existing.entities.append(text)
        candidates = list(by_uri.values())
        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        return candidates

    def embed_query(self, query: str) -> list[float]:
        """Embed the recall query."""
        result = self.embedder.embed(query)
        vector = getattr(result, "dense_vector", None)
        if not vector:
            raise ValueError("Recall probe embedding returned no dense vector")
        return list(vector)

    async def aembed_query(self, query: str) -> list[float]:
        """Embed the recall query using an async embedder when available."""
        if hasattr(self.embedder, "prefer_async") and hasattr(self.embedder, "aembed"):
            result = await self.embedder.aembed(query)
            vector = getattr(result, "dense_vector", None)
            if not vector:
                raise ValueError("Recall probe embedding returned no dense vector")
            return list(vector)
        return await asyncio.to_thread(self.embed_query, query)


def candidate_anchors(record: dict[str, Any]) -> list[str]:
    """Return anchor-like strings from one vector record."""
    values: list[str] = []
    for key in ("entity_text", "abstract", "overview"):
        text = str(record.get(key, "") or "").strip()
        if text and text not in values:
            values.append(text)
    for entity in record.get("entities") or []:
        text = str(entity).strip()
        if text and text not in values:
            values.append(text)
    keywords = str(record.get("keywords", "") or "")
    for keyword in keywords.split(","):
        text = keyword.strip()
        if text and text not in values:
            values.append(text)
    return values[:8]


def top_records(records: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """Return top scored records."""
    return sorted(records, key=record_score, reverse=True)[:limit]


def starting_uris_from_locator_hits(
    *,
    direct_records: list[dict[str, Any]],
    locator_records: list[dict[str, Any]],
) -> list[str]:
    """Return bounded URI entry points for later retrieval planning."""
    values: list[str] = []
    locator_hits = [locator_entry(record) for record in locator_records]
    for uri in [hit["tree_uri"] for hit in locator_hits]:
        append_unique(values, uri)
    for hit in locator_hits:
        append_unique(values, hit["parent_uri"])
        append_unique(values, hit["source_uri"])
        for neighbor in hit["cone_neighbors"]:
            append_unique(values, neighbor)
    for record in direct_records:
        append_unique(values, str(record.get("parent_uri", "") or ""))
        append_unique(values, str(record.get("uri", "") or ""))
    return values[: RetrievalProbe.top_k]


def locator_entry(record: dict[str, Any]) -> dict[str, Any]:
    """Return URI fields needed to seed planner starting points."""
    meta = dict(record.get("meta") or {})
    return {
        "source_uri": source_uri(record),
        "parent_uri": str(record.get("parent_uri", "") or ""),
        "tree_uri": str(record.get("tree_uri") or meta.get("tree_uri") or ""),
        "cone_neighbors": list(record.get("cone_neighbors") or []),
    }


def append_unique(values: list[str], value: str) -> None:
    """Append a non-empty unique string."""
    text = str(value or "").strip()
    if text and text not in values:
        values.append(text)


LARGE_QUERY_HINTS = {
    "总结",
    "回顾",
    "全部",
    "所有",
    "整体",
    "对比",
    "梳理",
    "列出",
    "按阶段",
    "完整",
    "全量",
    "summarize",
    "recap",
    "all",
    "compare",
    "overall",
    "full",
}


def query_size_for(query: str) -> QuerySize:
    """Classify query size for probe query preparation."""
    text = " ".join(str(query or "").split())
    lowered = text.lower()
    if any(hint in lowered for hint in LARGE_QUERY_HINTS):
        return QuerySize.LARGE
    if len(text) > 120 or clause_count(text) >= 3:
        return QuerySize.LARGE
    token_count = len(re.findall(r"[\w-]+|[\u4e00-\u9fff]{2,}", text))
    if token_count <= 5 and len(text) <= 40 and not question_like(text):
        return QuerySize.QUICK
    return QuerySize.MEDIUM


def query_type_for(query: str) -> QueryType:
    """Return a conservative fallback query type."""
    text = " ".join(str(query or "").split())
    lowered = text.lower()
    has_temporal_order = any(
        hint in lowered
        for hint in (
            "before",
            "after",
            "since",
            "until",
            "latest",
            "earliest",
            "之前",
            "之后",
            "以来",
            "直到",
            "最近",
            "最早",
            "最晚",
        )
    )
    has_explicit_date = bool(
        re.search(
            r"\b(?:19|20)\d{2}(?:[-/.]\d{1,2}(?:[-/.]\d{1,2})?)?\b",
            text,
        )
    )
    if has_temporal_order or has_explicit_date:
        return QueryType.TEMPORAL
    if query_size_for(text) == QuerySize.LARGE:
        return QueryType.SUMMARY
    return QueryType.FACTUAL


def normalized_query_type(value: str) -> QueryType | None:
    """Return an LLM-supplied query type constrained to the internal enum."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    try:
        return QueryType(text)
    except ValueError:
        return None


def clause_count(query: str) -> int:
    """Return a rough clause count for a query."""
    return len([part for part in re.split(r"[,，;；、\n]+", query) if part.strip()])


def question_like(query: str) -> bool:
    """Return whether a query looks like a natural-language question."""
    lowered = query.lower()
    return bool(
        "?" in query
        or "？" in query
        or re.search(r"\b(why|how|when|where|who|what)\b", lowered)
        or any(
            word in query
            for word in ("为什么", "怎么", "如何", "什么时候", "哪里", "谁")
        )
    )


def normalize_retrieval_queries(values: list[Any]) -> list[str]:
    """Normalize LLM-proposed retrieval queries."""
    queries: list[str] = []
    for value in values:
        text = " ".join(str(value or "").split())
        if not text:
            continue
        if len(text) > RetrievalProbe.max_retrieval_query_chars:
            raise ValueError("LLM query planning returned an oversized retrieval query")
        if text and text not in queries:
            queries.append(text)
        if len(queries) >= RetrievalProbe.max_retrieval_queries:
            break
    return queries
