# SPDX-License-Identifier: Apache-2.0
"""Qdrant execution for opencortex recall."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from opencortex.core.identity import IdentityProfile
from opencortex.vector.retrieval.filters import retrieval_filter
from opencortex.vector.retrieval.records import source_uri
from opencortex.vector.retrieval.schemas import (
    RetrievalHit,
    RetrievalPlan,
    RetrievalSurface,
)
from opencortex.vector.retrieval.temporal import temporal_filter_conditions

logger = structlog.get_logger(__name__)


class RetrievalExecutor:
    """Execute one retrieval plan against vector surfaces."""

    def __init__(
        self,
        *,
        vector_store: Any,
        collection_resolver: Any,
        surface_timeout_seconds: float = 8.0,
    ) -> None:
        self.vector_store = vector_store
        self.collection_resolver = collection_resolver
        self.surface_timeout_seconds = max(0.1, float(surface_timeout_seconds))

    async def execute(
        self,
        *,
        plan: RetrievalPlan,
        profile: IdentityProfile,
    ) -> list[RetrievalHit]:
        """Run vector search over all retrieval surfaces."""
        if not plan.search_vectors:
            raise ValueError("Retrieval plan has no search vectors")
        collection = self.collection_resolver()
        results = await asyncio.gather(
            *[
                self.timed_search_surface(
                    collection=collection,
                    surface=surface,
                    query_vector=query_vector,
                    plan=plan,
                    profile=profile,
                )
                for surface in plan.surfaces
                for query_vector in plan.search_vectors
            ]
        )
        return [hit for surface_hits in results for hit in surface_hits]

    async def timed_search_surface(
        self,
        *,
        collection: str,
        surface: RetrievalSurface,
        query_vector: list[float],
        plan: RetrievalPlan,
        profile: IdentityProfile,
    ) -> list[RetrievalHit]:
        """Search one surface with bounded latency."""
        try:
            return await asyncio.wait_for(
                self.search_surface(
                    collection=collection,
                    surface=surface,
                    query_vector=query_vector,
                    plan=plan,
                    profile=profile,
                ),
                timeout=self.surface_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "retrieval_surface_timeout",
                surface=surface.value,
                timeout_seconds=self.surface_timeout_seconds,
            )
            return []

    async def search_surface(
        self,
        *,
        collection: str,
        surface: RetrievalSurface,
        query_vector: list[float],
        plan: RetrievalPlan,
        profile: IdentityProfile,
    ) -> list[RetrievalHit]:
        """Search one retrieval surface."""
        filters = retrieval_filter(
            profile=profile,
            surface=surface.value,
        )
        temporal_conditions = temporal_filter_conditions(plan.temporal)
        if temporal_conditions:
            filters.must = list(filters.must or []) + temporal_conditions
        records = await self.vector_store.search(
            collection,
            query_vector=query_vector,
            filters=filters,
            limit=plan.surface_limits.get(surface, plan.candidate_limit),
        )
        return [
            RetrievalHit(
                record=record,
                score=float(record.get("_score", 0.0) or 0.0),
                surface=surface,
                source_uri=source_uri(record),
            )
            for record in records
        ]
