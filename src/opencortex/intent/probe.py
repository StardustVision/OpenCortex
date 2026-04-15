# SPDX-License-Identifier: Apache-2.0
"""Phase 1 bootstrap probe: query -> cheap L0 evidence."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional, Tuple

from opencortex.intent.types import (
    MemoryProbeTrace,
    ProbeScopeInput,
    ProbeScopeSource,
    ScopeLevel,
    SearchCandidate,
    SearchEvidence,
    SearchResult,
    StartingPoint,
)
from opencortex.memory import memory_object_view_from_record

logger = logging.getLogger(__name__)

_CAMEL_CASE_RE = re.compile(r"[a-z]+[A-Z][a-zA-Z]*")
_ALL_CAPS_RE = re.compile(r"\b[A-Z]{2,}\b")
_PATH_SYMBOL_RE = re.compile(r"[a-zA-Z0-9]+[_./-][a-zA-Z0-9]+")
_QUOTED_PHRASE_RE = re.compile(r"[\"'`“”‘’]([^\"'`“”‘’]{2,})[\"'`“”‘’]")
_CAPITALIZED_PHRASE_RE = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*\b")
_WORD_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_TIME_TOKEN_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{4}|\d{1,2}:\d{2}|yesterday|today|tomorrow|last|next)\b",
    re.IGNORECASE,
)
_CJK_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]")
_WHITESPACE_RE = re.compile(r"\s+")
_LEAF_FILTER = {"op": "must", "field": "is_leaf", "conds": [True]}
_OBJECT_SURFACE_FILTER = {
    "op": "must",
    "field": "retrieval_surface",
    "conds": ["l0_object"],
}
_ANCHOR_SURFACE_FILTER = {
    "op": "must",
    "field": "anchor_surface",
    "conds": [True],
}
_MAX_ANCHOR_TERMS = 6
_MIN_ANCHOR_QUERY_LENGTH = 2
_QUERY_STOPWORDS = {
    "what",
    "which",
    "who",
    "when",
    "where",
    "why",
    "how",
    "did",
    "does",
    "do",
    "about",
    "from",
    "with",
    "that",
    "this",
    "have",
    "been",
    "were",
    "your",
    "their",
    "there",
    "into",
    "just",
    "will",
    "would",
    "could",
    "should",
    "please",
    "tell",
    "show",
    "list",
    "summary",
    "summarize",
    "过去",
    "之前",
    "一下",
    "关于",
    "什么",
    "哪个",
    "哪些",
    "现在",
    "需要",
}

_HARD_KEYWORD_LEXICAL_BOOST = 0.55
_DEFAULT_LEXICAL_BOOST = 0.3

_CACHE_TTL_SECONDS = 60.0
_CACHE_MAX_SIZE = 128


def _copy_trace(trace: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(trace)


def _merge_filters(
    left: Optional[Dict[str, Any]],
    right: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Combine two filter clauses into one AND clause."""
    if left and right:
        return {"op": "and", "conds": [left, right]}
    return left or right


class MemoryBootstrapProbe:
    """Execute one bounded local first-pass probe."""

    def __init__(
        self,
        *,
        storage: Any,
        embedder: Any,
        collection_resolver: Callable[[], str],
        filter_builder: Callable[[], Dict[str, Any]],
        top_k: int = 3,
    ) -> None:
        self._storage = storage
        self._embedder = embedder
        self._collection_resolver = collection_resolver
        self._filter_builder = filter_builder
        self._top_k = max(1, top_k)
        self._last_probe_trace = MemoryProbeTrace(
            top_k=self._top_k,
            degraded=True,
            degrade_reason="idle",
        ).to_dict()
        self._decision_cache: OrderedDict[
            str,
            Tuple[SearchResult, Dict[str, Any], float],
        ] = OrderedDict()

    @staticmethod
    def _detect_hard_keywords(query: str) -> bool:
        return bool(
            _CAMEL_CASE_RE.search(query)
            or _ALL_CAPS_RE.search(query)
            or _PATH_SYMBOL_RE.search(query)
        )

    @classmethod
    def lexical_boost(cls, query: str) -> float:
        return (
            _HARD_KEYWORD_LEXICAL_BOOST
            if cls._detect_hard_keywords(query.strip())
            else _DEFAULT_LEXICAL_BOOST
        )

    @property
    def mode(self) -> str:
        return "local_probe"

    def probe_trace(self) -> Dict[str, Any]:
        return _copy_trace(self._last_probe_trace)

    async def probe(
        self,
        query: str,
        *,
        scope_filter: Optional[Dict[str, Any]] = None,
        scope_input: Optional[ProbeScopeInput] = None,
    ) -> SearchResult:
        """Run the bootstrap probe against L0 evidence only."""
        query_stripped = _WHITESPACE_RE.sub(" ", (query or "").strip())
        if not query_stripped:
            result = SearchResult(
                should_recall=False,
                trace=MemoryProbeTrace(
                    backend="local_probe",
                    model=self._model_name(),
                    top_k=self._top_k,
                    latency_ms=0.0,
                    degraded=True,
                    degrade_reason="empty_query",
                ),
            )
            self._last_probe_trace = result.trace.to_dict()
            return result

        normalized_scope_input = scope_input or ProbeScopeInput()
        cache_key = self._cache_key(
            query=query_stripped,
            scope_filter=scope_filter,
            scope_input=normalized_scope_input,
        )
        cached = self._decision_cache_get(cache_key)
        if cached is not None:
            return cached

        started = time.perf_counter()
        try:
            query_vector = self._embed_query(query_stripped)
            base_filter = _merge_filters(self._filter_builder(), scope_filter)
            (
                selected_filter,
                selected_scope_source,
                selected_scope_authoritative,
                selected_scope_level,
                selected_root_uris,
                starting_point_records,
                starting_point_latency_ms,
            ) = await self._select_scope_bucket(
                query=query_stripped,
                query_vector=query_vector,
                base_filter=base_filter,
                scope_input=normalized_scope_input,
            )
            object_probe, anchor_probe = await asyncio.gather(
                self._timed_probe(
                    self._object_probe(
                        query=query_stripped,
                        query_vector=query_vector,
                        base_filter=selected_filter,
                    )
                ),
                self._timed_probe(
                    self._anchor_probe(
                        query=query_stripped,
                        base_filter=selected_filter,
                    )
                ),
            )
            result = self._build_probe_result(
                query=query_stripped,
                object_records=object_probe[0],
                anchor_records=anchor_probe[0],
                starting_point_records=starting_point_records,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                object_latency_ms=object_probe[1],
                anchor_latency_ms=anchor_probe[1],
                starting_point_latency_ms=starting_point_latency_ms,
                scope_source=selected_scope_source,
                scope_authoritative=selected_scope_authoritative,
                selected_root_uris=selected_root_uris,
                selected_scope_level=selected_scope_level,
            )
        except Exception as exc:
            logger.warning("[MemoryBootstrapProbe] probe failed: %s", exc)
            result = SearchResult(
                should_recall=True,
                trace=MemoryProbeTrace(
                    backend="local_probe",
                    model=self._model_name(),
                    top_k=self._top_k,
                    latency_ms=round((time.perf_counter() - started) * 1000.0, 4),
                    degraded=True,
                    degrade_reason=str(exc),
                ),
            )

        if result.scope_authoritative and not result.candidate_entries and not result.anchor_hits:
            result.should_recall = False
            result.scoped_miss = True
            result.fallback_ready = False
            result.trace.scoped_miss = True

        self._last_probe_trace = result.trace.to_dict()
        self._decision_cache_put(cache_key, result, self._last_probe_trace)
        return result

    async def _timed_probe(
        self,
        awaitable: Any,
    ) -> tuple[list[Dict[str, Any]], float]:
        started = time.perf_counter()
        records = await awaitable
        return records, (time.perf_counter() - started) * 1000.0

    async def _object_probe(
        self,
        *,
        query: str,
        query_vector: Optional[list[float]],
        base_filter: Optional[Dict[str, Any]],
    ) -> list[Dict[str, Any]]:
        object_filter = _merge_filters(
            _merge_filters(base_filter, _LEAF_FILTER),
            _OBJECT_SURFACE_FILTER,
        )
        return await self._storage.search(
            collection=self._collection_resolver(),
            query_vector=query_vector,
            filter=object_filter,
            limit=self._top_k,
            text_query=query,
        )

    async def _anchor_probe(
        self,
        *,
        query: str,
        base_filter: Optional[Dict[str, Any]],
    ) -> list[Dict[str, Any]]:
        anchor_terms = self._query_anchor_terms(query)
        if not anchor_terms:
            return []

        searches = [
            self._storage.search(
                collection=self._collection_resolver(),
                filter=_merge_filters(
                    _merge_filters(
                        base_filter,
                        _ANCHOR_SURFACE_FILTER,
                    ),
                    {"op": "must", "field": "anchor_hits", "conds": [term]},
                ),
                limit=self._top_k,
                text_query=term,
            )
            for term in anchor_terms
        ]
        term_results = await asyncio.gather(*searches)
        merged: Dict[str, Dict[str, Any]] = {}
        for term, records in zip(anchor_terms, term_results):
            for record in records:
                uri = str(record.get("uri", "") or "")
                if not uri:
                    continue
                score = self._record_score(record)
                existing = merged.get(uri)
                if existing is None or score > self._record_score(existing):
                    merged_record = dict(record)
                    merged_record["_anchor_terms"] = [term]
                    merged[uri] = merged_record
                    continue
                terms = list(existing.get("_anchor_terms") or [])
                if term not in terms:
                    terms.append(term)
                existing["_anchor_terms"] = terms

        anchor_records = list(merged.values())
        anchor_records.sort(
            key=lambda record: self._record_score(record),
            reverse=True,
        )
        return anchor_records[: max(self._top_k, 1)]

    async def _starting_point_probe(
        self,
        *,
        query: str,
        query_vector: Optional[list[float]],
        base_filter: Optional[Dict[str, Any]],
    ) -> list[Dict[str, Any]]:
        """Find session/document roots as starting points for scoped retrieval."""
        immediate_exclude = {
            "op": "must_not",
            "field": "meta.layer",
            "conds": ["immediate"],
        }
        sp_filter = _merge_filters(base_filter, immediate_exclude)

        records = await self._storage.search(
            collection=self._collection_resolver(),
            query_vector=query_vector,
            filter=sp_filter,
            limit=max(self._top_k * 5, 5),
            text_query=query,
        )

        starting_points: list[Dict[str, Any]] = []
        for record in records:
            session_id = str(record.get("session_id") or "").strip()
            source_doc_id = str(record.get("source_doc_id") or "").strip()
            parent_uri = str(record.get("parent_uri") or "").strip()
            uri = str(record.get("uri") or "").strip()

            if not session_id and not source_doc_id:
                continue

            # Defensive: skip per-message immediates that slipped through
            meta_layer = (record.get("meta") or {}).get("layer")
            if meta_layer == "immediate":
                continue

            # Skip anchor projection records — they are not containers
            if record.get("retrieval_surface") == "anchor_projection":
                continue

            if session_id:
                starting_points.append(record)
                continue

            if source_doc_id:
                is_doc_root = False
                if not parent_uri:
                    is_doc_root = True
                else:
                    uri_parts = uri.rstrip("/").split("/")
                    if (
                        len(uri_parts) >= 2
                        and uri_parts[-1] == source_doc_id
                        and uri_parts[-2] == "documents"
                    ):
                        is_doc_root = True
                if is_doc_root:
                    starting_points.append(record)

        seen: set[str] = set()
        deduped: list[Dict[str, Any]] = []
        for record in sorted(
            starting_points,
            key=lambda r: self._record_score(r) or 0.0,
            reverse=True,
        ):
            uri = str(record.get("uri") or "")
            if uri and uri not in seen:
                seen.add(uri)
                deduped.append(record)
                if len(deduped) >= self._top_k:
                    break
        return deduped

    async def _select_scope_bucket(
        self,
        *,
        query: str,
        query_vector: Optional[list[float]],
        base_filter: Optional[Dict[str, Any]],
        scope_input: ProbeScopeInput,
    ) -> tuple[
        Optional[Dict[str, Any]],
        ProbeScopeSource,
        bool,
        ScopeLevel,
        list[str],
        list[Dict[str, Any]],
        float,
    ]:
        """Select the active scope bucket before object/anchor retrieval."""
        selected_filter = base_filter
        selected_scope_source = scope_input.source
        selected_scope_authoritative = bool(scope_input.authoritative)
        selected_scope_level = ScopeLevel.GLOBAL
        selected_root_uris: list[str] = []
        starting_point_records: list[Dict[str, Any]] = []
        starting_point_latency_ms = 0.0

        if selected_scope_source == ProbeScopeSource.TARGET_URI:
            selected_scope_level = ScopeLevel.CONTAINER_SCOPED
            if scope_input.target_uri:
                selected_root_uris = [scope_input.target_uri]
            return (
                selected_filter,
                selected_scope_source,
                selected_scope_authoritative,
                selected_scope_level,
                selected_root_uris,
                starting_point_records,
                starting_point_latency_ms,
            )

        if selected_scope_source in {
            ProbeScopeSource.SESSION_ID,
            ProbeScopeSource.SOURCE_DOC_ID,
            ProbeScopeSource.GLOBAL_ROOT,
        }:
            starting_point_records, starting_point_latency_ms = await self._timed_probe(
                self._starting_point_probe(
                    query=query,
                    query_vector=query_vector,
                    base_filter=base_filter,
                )
            )

        if selected_scope_source == ProbeScopeSource.SESSION_ID:
            selected_scope_level = ScopeLevel.SESSION_ONLY
            selected_root_uris = [
                str(record.get("uri") or "")
                for record in starting_point_records
                if str(record.get("uri") or "").strip()
            ][: self._top_k]
            return (
                selected_filter,
                selected_scope_source,
                selected_scope_authoritative,
                selected_scope_level,
                selected_root_uris,
                starting_point_records,
                starting_point_latency_ms,
            )

        if selected_scope_source == ProbeScopeSource.SOURCE_DOC_ID:
            selected_scope_level = ScopeLevel.DOCUMENT_ONLY
            selected_root_uris = [
                str(record.get("uri") or "")
                for record in starting_point_records
                if str(record.get("uri") or "").strip()
            ][: self._top_k]
            return (
                selected_filter,
                selected_scope_source,
                selected_scope_authoritative,
                selected_scope_level,
                selected_root_uris,
                starting_point_records,
                starting_point_latency_ms,
            )

        if selected_scope_source == ProbeScopeSource.CONTEXT_TYPE:
            return (
                selected_filter,
                selected_scope_source,
                selected_scope_authoritative,
                selected_scope_level,
                selected_root_uris,
                starting_point_records,
                starting_point_latency_ms,
            )

        if starting_point_records:
            top_record = starting_point_records[0]
            session_id = str(top_record.get("session_id") or "").strip()
            source_doc_id = str(top_record.get("source_doc_id") or "").strip()
            if session_id:
                selected_scope_source = ProbeScopeSource.SESSION_ID
                selected_scope_level = ScopeLevel.SESSION_ONLY
                starting_point_records = [
                    record
                    for record in starting_point_records
                    if str(record.get("session_id") or "").strip() == session_id
                ][: self._top_k]
                selected_root_uris = [
                    str(record.get("uri") or "")
                    for record in starting_point_records
                    if str(record.get("uri") or "").strip()
                ]
                selected_filter = _merge_filters(
                    base_filter,
                    {"op": "must", "field": "session_id", "conds": [session_id]},
                )
            elif source_doc_id:
                selected_scope_source = ProbeScopeSource.SOURCE_DOC_ID
                selected_scope_level = ScopeLevel.DOCUMENT_ONLY
                starting_point_records = [
                    record
                    for record in starting_point_records
                    if str(record.get("source_doc_id") or "").strip() == source_doc_id
                ][: self._top_k]
                selected_root_uris = [
                    str(record.get("uri") or "")
                    for record in starting_point_records
                    if str(record.get("uri") or "").strip()
                ]
                selected_filter = _merge_filters(
                    base_filter,
                    {"op": "must", "field": "source_doc_id", "conds": [source_doc_id]},
                )

        return (
            selected_filter,
            selected_scope_source,
            selected_scope_authoritative,
            selected_scope_level,
            selected_root_uris,
            starting_point_records,
            starting_point_latency_ms,
        )

    def _build_probe_result(
        self,
        *,
        query: str,
        object_records: list[Dict[str, Any]],
        anchor_records: list[Dict[str, Any]],
        starting_point_records: list[Dict[str, Any]],
        latency_ms: float,
        object_latency_ms: float,
        anchor_latency_ms: float,
        starting_point_latency_ms: float,
        scope_source: ProbeScopeSource,
        scope_authoritative: bool,
        selected_root_uris: list[str],
        selected_scope_level: ScopeLevel,
    ) -> SearchResult:
        candidate_entries_by_uri: Dict[str, SearchCandidate] = {}
        anchor_values: list[str] = []

        def _merge_record(record: Dict[str, Any], *, anchor_first: bool) -> None:
            object_view = memory_object_view_from_record(
                self._candidate_record_payload(record)
            )
            normalized_score = self._record_score(record)

            anchors = self._candidate_anchors(
                object_view,
                extra_terms=record.get("_anchor_terms"),
            )
            for anchor in anchors:
                if anchor not in anchor_values:
                    anchor_values.append(anchor)

            existing = candidate_entries_by_uri.get(object_view.uri)
            if existing is None:
                candidate_entries_by_uri[object_view.uri] = SearchCandidate(
                    uri=object_view.uri,
                    memory_kind=object_view.memory_kind,
                    context_type=object_view.context_type,
                    category=object_view.category,
                    score=normalized_score,
                    abstract=object_view.abstract,
                    overview=object_view.overview,
                    anchors=anchors,
                )
                return
            existing.score = max(
                existing.score or 0.0,
                normalized_score or 0.0,
            )
            merged_anchors = list(existing.anchors)
            sources = anchors + existing.anchors if anchor_first else existing.anchors + anchors
            for anchor in sources:
                if anchor not in merged_anchors:
                    merged_anchors.append(anchor)
            existing.anchors = merged_anchors[:8]
            if not existing.abstract and object_view.abstract:
                existing.abstract = object_view.abstract
            if existing.overview is None and object_view.overview:
                existing.overview = object_view.overview

        for record in object_records:
            _merge_record(record, anchor_first=False)
        for record in anchor_records:
            _merge_record(record, anchor_first=True)

        candidate_entries = list(candidate_entries_by_uri.values())
        candidate_entries.sort(key=lambda candidate: candidate.score or 0.0, reverse=True)
        candidate_entries = candidate_entries[: self._top_k]
        scores = [
            float(candidate.score)
            for candidate in candidate_entries
            if candidate.score is not None
        ]
        scores.sort(reverse=True)
        object_scores = [
            float(score)
            for score in (
                self._record_score(record)
                for record in object_records
            )
            if score is not None
        ]
        object_scores.sort(reverse=True)
        anchor_scores = [
            float(score)
            for score in (
                self._record_score(record)
                for record in anchor_records
            )
            if score is not None
        ]
        anchor_scores.sort(reverse=True)

        top_score = scores[0] if scores else None
        score_gap = None
        if len(scores) >= 2:
            score_gap = round(scores[0] - scores[1], 4)

        query_entities = self._query_anchor_terms(query)

        starting_points: list[StartingPoint] = []
        starting_point_anchors: list[str] = []
        scope_levels: set[ScopeLevel] = set()

        for record in starting_point_records:
            session_id = str(record.get("session_id") or "").strip() or None
            source_doc_id = str(record.get("source_doc_id") or "").strip() or None
            parent_uri = str(record.get("parent_uri") or "").strip() or None
            uri = str(record.get("uri") or "").strip()

            structured_slots = record.get("structured_slots", {})
            if not structured_slots and "entities" in record:
                structured_slots = {
                    "entities": record.get("entities", []),
                    "time_refs": record.get("time_refs", []),
                }
            entities = [
                str(v).strip()
                for v in structured_slots.get("entities", [])
                if str(v).strip()
            ]
            time_refs = [
                str(v).strip()
                for v in structured_slots.get("time_refs", [])
                if str(v).strip()
            ]
            score = self._record_score(record) or 0.0

            starting_points.append(
                StartingPoint(
                    uri=uri,
                    session_id=session_id,
                    source_doc_id=source_doc_id,
                    parent_uri=parent_uri,
                    entities=entities,
                    time_refs=time_refs,
                    score=score,
                )
            )

            for value in entities + time_refs:
                if value and value not in starting_point_anchors:
                    starting_point_anchors.append(value)

            if session_id:
                # All session-scoped records currently map to SESSION_ONLY
                # because the storage hierarchy does not support reliable
                # parent_uri-based child retrieval for arbitrary memory records.
                scope_levels.add(ScopeLevel.SESSION_ONLY)
            elif source_doc_id:
                scope_levels.add(ScopeLevel.DOCUMENT_ONLY)
            else:
                scope_levels.add(ScopeLevel.GLOBAL)

        # Most specific scope level wins
        scope_level = ScopeLevel.GLOBAL
        if ScopeLevel.CONTAINER_SCOPED in scope_levels:
            scope_level = ScopeLevel.CONTAINER_SCOPED
        elif ScopeLevel.SESSION_ONLY in scope_levels:
            scope_level = ScopeLevel.SESSION_ONLY
        elif ScopeLevel.DOCUMENT_ONLY in scope_levels:
            scope_level = ScopeLevel.DOCUMENT_ONLY

        if not starting_points and selected_scope_level == ScopeLevel.GLOBAL:
            scope_level = ScopeLevel.GLOBAL
        if selected_scope_level != ScopeLevel.GLOBAL:
            scope_level = selected_scope_level

        fallback_ready = False

        return SearchResult(
            should_recall=True,
            anchor_hits=anchor_values[:8],
            candidate_entries=candidate_entries,
            starting_points=starting_points,
            query_entities=query_entities,
            starting_point_anchors=starting_point_anchors[:8],
            scope_level=scope_level,
            scope_source=scope_source,
            scope_authoritative=scope_authoritative,
            selected_root_uris=selected_root_uris[: self._top_k],
            fallback_ready=fallback_ready,
            evidence=SearchEvidence(
                top_score=top_score,
                score_gap=score_gap,
                object_top_score=object_scores[0] if object_scores else None,
                anchor_top_score=anchor_scores[0] if anchor_scores else None,
                candidate_count=len(candidate_entries),
                object_candidate_count=len(object_records),
                anchor_candidate_count=len(anchor_records),
                anchor_hit_count=len(anchor_values),
            ),
            trace=MemoryProbeTrace(
                backend="local_probe",
                model=self._model_name(),
                top_k=self._top_k,
                latency_ms=round(latency_ms, 4),
                object_latency_ms=round(object_latency_ms, 4),
                anchor_latency_ms=round(anchor_latency_ms, 4),
                starting_points=len(starting_points),
                object_candidates=len(object_records),
                anchor_candidates=len(anchor_records),
                selected_bucket_source=scope_source,
                scope_authoritative=scope_authoritative,
                selected_root_uris=selected_root_uris[: self._top_k],
                fallback_ready=fallback_ready,
            ),
        )

    @staticmethod
    def _candidate_record_payload(record: Dict[str, Any]) -> Dict[str, Any]:
        """Map anchor projection hits back to the source object payload."""
        if record.get("retrieval_surface") != "anchor_projection":
            return record

        source_uri = (
            str(record.get("projection_target_uri", "") or "")
            or str(record.get("parent_uri", "") or "")
            or str(record.get("uri", "") or "")
        )
        return {
            "uri": source_uri,
            "parent_uri": record.get("parent_uri", ""),
            "category": record.get("category", ""),
            "context_type": record.get("context_type", ""),
            "abstract": record.get("projection_target_abstract", ""),
            "overview": record.get("projection_target_overview", ""),
            "content": record.get("projection_target_content", ""),
            "entities": record.get("entities", []),
            "meta": record.get("meta", {}),
        }

    def _candidate_anchors(
        self,
        object_view: Any,
        *,
        extra_terms: Optional[list[str]] = None,
    ) -> list[str]:
        anchors: list[str] = []
        for value in extra_terms or []:
            normalized = str(value).strip()
            if normalized and normalized not in anchors:
                anchors.append(normalized)
            if len(anchors) >= 6:
                return anchors
        slot_groups = (
            object_view.structured_slots.entities,
            object_view.structured_slots.time_refs,
            object_view.structured_slots.topics,
            object_view.structured_slots.preferences,
            object_view.structured_slots.constraints,
            object_view.structured_slots.relations,
        )
        for slot_group in slot_groups:
            for value in slot_group:
                normalized = str(value).strip()
                if normalized and normalized not in anchors:
                    anchors.append(normalized)
                if len(anchors) >= 6:
                    return anchors
        return anchors

    @staticmethod
    def _record_score(record: Dict[str, Any]) -> Optional[float]:
        for key in ("_score", "score", "_text_score"):
            raw_value = record.get(key)
            if raw_value is not None:
                return float(raw_value)
        return None

    def _query_anchor_terms(self, query: str) -> list[str]:
        values: list[str] = []

        def _add(candidate: str) -> None:
            normalized = _WHITESPACE_RE.sub(" ", str(candidate or "").strip())
            if len(normalized) < _MIN_ANCHOR_QUERY_LENGTH:
                return
            if normalized.lower() in _QUERY_STOPWORDS:
                return
            if normalized not in values:
                values.append(normalized)

        for match in _QUOTED_PHRASE_RE.findall(query):
            _add(match)
        for match in _TIME_TOKEN_RE.findall(query):
            _add(match)
        for match in _CAPITALIZED_PHRASE_RE.findall(query):
            _add(match)
        for match in re.findall(r"[\u4e00-\u9fff]{2,}", query):
            _add(match)
        for match in _CJK_TOKEN_RE.findall(query):
            _add(match)
        for match in _PATH_SYMBOL_RE.findall(query):
            _add(match)
        for match in _WORD_TOKEN_RE.findall(query):
            _add(match)
            if len(values) >= _MAX_ANCHOR_TERMS:
                break

        return values[:_MAX_ANCHOR_TERMS]

    def _embed_query(self, query: str) -> Optional[list[float]]:
        if self._embedder is None:
            return None
        if hasattr(self._embedder, "is_available") and not self._embedder.is_available:
            return None
        result = self._embedder.embed_query(query)
        return getattr(result, "dense_vector", None)

    def _model_name(self) -> Optional[str]:
        return getattr(self._embedder, "model_name", None)

    @staticmethod
    def _cache_key(
        *,
        query: str,
        scope_filter: Optional[Dict[str, Any]],
        scope_input: ProbeScopeInput,
    ) -> str:
        return json.dumps(
            {
                "query": query,
                "scope_filter": scope_filter or {},
                "scope_input": scope_input.to_dict(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _decision_cache_get(self, key: str) -> Optional[SearchResult]:
        if key not in self._decision_cache:
            return None
        result, trace, timestamp = self._decision_cache[key]
        if time.monotonic() - timestamp > _CACHE_TTL_SECONDS:
            self._decision_cache.pop(key, None)
            return None
        self._decision_cache.move_to_end(key)
        self._last_probe_trace = _copy_trace(trace)
        return result.model_copy(deep=True)

    def _decision_cache_put(
        self,
        key: str,
        result: SearchResult,
        trace: Dict[str, Any],
    ) -> None:
        self._decision_cache[key] = (
            result.model_copy(deep=True),
            _copy_trace(trace),
            time.monotonic(),
        )
        self._decision_cache.move_to_end(key)
        while len(self._decision_cache) > _CACHE_MAX_SIZE:
            self._decision_cache.popitem(last=False)
