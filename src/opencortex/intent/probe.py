# SPDX-License-Identifier: Apache-2.0
"""Phase 1 bootstrap probe: query -> cheap L0 evidence."""

from __future__ import annotations

import copy
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional, Tuple

from opencortex.intent.types import (
    MemoryProbeTrace,
    SearchCandidate,
    SearchEvidence,
    SearchResult,
)
from opencortex.memory import memory_object_view_from_record

logger = logging.getLogger(__name__)

_CAMEL_CASE_RE = re.compile(r"[a-z]+[A-Z][a-zA-Z]*")
_ALL_CAPS_RE = re.compile(r"\b[A-Z]{2,}\b")
_PATH_SYMBOL_RE = re.compile(r"[a-zA-Z0-9]+[_./-][a-zA-Z0-9]+")
_WHITESPACE_RE = re.compile(r"\s+")

_HARD_KEYWORD_LEXICAL_BOOST = 0.55
_DEFAULT_LEXICAL_BOOST = 0.3

_CACHE_TTL_SECONDS = 60.0
_CACHE_MAX_SIZE = 128


def _copy_trace(trace: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(trace)


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

    async def probe(self, query: str) -> SearchResult:
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

        cached = self._decision_cache_get(query_stripped)
        if cached is not None:
            return cached

        started = time.perf_counter()
        try:
            query_vector = self._embed_query(query_stripped)
            records = await self._storage.search(
                collection=self._collection_resolver(),
                query_vector=query_vector,
                filter=self._filter_builder(),
                limit=self._top_k,
                text_query=query_stripped,
            )
            result = self._build_probe_result(
                records=records,
                latency_ms=(time.perf_counter() - started) * 1000.0,
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

        self._last_probe_trace = result.trace.to_dict()
        self._decision_cache_put(query_stripped, result, self._last_probe_trace)
        return result

    def _build_probe_result(
        self,
        *,
        records: list[Dict[str, Any]],
        latency_ms: float,
    ) -> SearchResult:
        candidate_entries: list[SearchCandidate] = []
        anchor_values: list[str] = []
        scores: list[float] = []

        for record in records:
            object_view = memory_object_view_from_record(record)
            score = record.get("_score")
            if score is None:
                score = record.get("score")
            normalized_score = float(score) if score is not None else None
            if normalized_score is not None:
                scores.append(normalized_score)

            anchors = self._candidate_anchors(object_view)
            for anchor in anchors:
                if anchor not in anchor_values:
                    anchor_values.append(anchor)

            candidate_entries.append(
                SearchCandidate(
                    uri=object_view.uri,
                    memory_kind=object_view.memory_kind,
                    context_type=object_view.context_type,
                    category=object_view.category,
                    score=normalized_score,
                    abstract=object_view.abstract,
                    overview=object_view.overview,
                    anchors=anchors,
                )
            )

        top_score = scores[0] if scores else None
        score_gap = None
        if len(scores) >= 2:
            score_gap = round(scores[0] - scores[1], 4)

        return SearchResult(
            should_recall=True,
            anchor_hits=anchor_values[:8],
            candidate_entries=candidate_entries,
            evidence=SearchEvidence(
                top_score=top_score,
                score_gap=score_gap,
                candidate_count=len(candidate_entries),
            ),
            trace=MemoryProbeTrace(
                backend="local_probe",
                model=self._model_name(),
                top_k=self._top_k,
                latency_ms=round(latency_ms, 4),
            ),
        )

    def _candidate_anchors(self, object_view: Any) -> list[str]:
        anchors: list[str] = []
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

    def _embed_query(self, query: str) -> Optional[list[float]]:
        if self._embedder is None:
            return None
        if hasattr(self._embedder, "is_available") and not self._embedder.is_available:
            return None
        result = self._embedder.embed_query(query)
        return getattr(result, "dense_vector", None)

    def _model_name(self) -> Optional[str]:
        return getattr(self._embedder, "model_name", None)

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
