# SPDX-License-Identifier: Apache-2.0
"""Qdrant execution for opencortex recall."""

from __future__ import annotations

import asyncio
from typing import Any

from opencortex.core.identity import IdentityProfile
from opencortex.vector.retrieval.filters import retrieval_filter
from opencortex.vector.retrieval.records import source_uri
from opencortex.vector.retrieval.schemas import (
    RetrievalHit,
    RetrievalPlan,
    RetrievalSurface,
)


class RetrievalExecutor:
    """Execute one retrieval plan against vector surfaces."""

    def __init__(
        self,
        *,
        vector_store: Any,
        collection_resolver: Any,
    ) -> None:
        self.vector_store = vector_store
        self.collection_resolver = collection_resolver

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
                self.search_surface(
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
        records = await self.vector_store.search(
            collection,
            query_vector=query_vector,
            filters=retrieval_filter(
                profile=profile,
                surface=surface.value,
            ),
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
