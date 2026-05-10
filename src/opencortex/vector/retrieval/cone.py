# SPDX-License-Identifier: Apache-2.0
"""Cone expansion over ranked recall seeds."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from qdrant_client import models

from opencortex.core.identity import IdentityProfile
from opencortex.storage.cortex_storage import CortexStorage
from opencortex.vector.retrieval.filters import field_match, retrieval_filter
from opencortex.vector.retrieval.schemas import (
    RetrievalHit,
    RetrievalPlan,
    RetrievalSurface,
)


class ConeExpansionResult(BaseModel):
    """Expanded retrieval hits."""

    hits: list[RetrievalHit] = Field(default_factory=list)


class ConeExpander:
    """Expand ranked seed hits through stored relation and cone-neighbor edges."""

    def __init__(
        self,
        *,
        vector_store: Any,
        collection_resolver: Any,
        cortex_storage: CortexStorage,
    ) -> None:
        self.vector_store = vector_store
        self.collection_resolver = collection_resolver
        self.cortex_storage = cortex_storage

    async def expand(
        self,
        *,
        hits: list[RetrievalHit],
        plan: RetrievalPlan,
        profile: IdentityProfile,
    ) -> ConeExpansionResult:
        """Return primary records adjacent to top ranked seeds."""
        if not plan.cone_expansion.enabled:
            return ConeExpansionResult()
        seed_uris = [
            hit.source_uri
            for hit in hits[: plan.cone_expansion.max_seeds]
            if hit.source_uri
        ]
        neighbor_uris = await self.neighbor_uris(seed_uris, hits)
        if not neighbor_uris:
            return ConeExpansionResult()
        records = await self.primary_records(
            neighbor_uris[
                : plan.cone_expansion.max_seeds
                * plan.cone_expansion.max_neighbors_per_seed
            ],
            profile=profile,
        )
        expanded_hits = [
            RetrievalHit(
                record=record,
                score=plan.cone_expansion.bonus,
                surface=RetrievalSurface.L0_OBJECT,
                source_uri=uri,
            )
            for uri, record in records.items()
        ]
        return ConeExpansionResult(hits=expanded_hits)

    async def neighbor_uris(
        self,
        seed_uris: list[str],
        hits: list[RetrievalHit],
    ) -> list[str]:
        """Return bounded neighbors for seed URIs."""
        seen = set(seed_uris)
        neighbors: list[str] = []
        by_uri = {hit.source_uri: hit for hit in hits if hit.source_uri}
        for seed_uri in seed_uris:
            hit = by_uri.get(seed_uri)
            if hit is not None:
                for uri in hit.record.get("cone_neighbors", []):
                    append_unique_neighbor(neighbors, seen, uri)
            for relation in await self.cortex_storage.get_relations(seed_uri):
                append_unique_neighbor(neighbors, seen, relation)
        return neighbors

    async def primary_records(
        self,
        uris: list[str],
        *,
        profile: IdentityProfile,
    ) -> dict[str, dict[str, Any]]:
        """Load l0_object records for expanded URIs."""
        if not uris:
            return {}
        uri_conditions = [field_match("uri", uri) for uri in uris]
        filters = retrieval_filter(
            profile=profile,
            surface=RetrievalSurface.L0_OBJECT.value,
        )
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
            limit=len(uris),
        )
        return {str(record.get("uri", "")): record for record in records}


def append_unique_neighbor(values: list[str], seen: set[str], uri: str) -> None:
    """Append one unseen neighbor URI."""
    text = str(uri or "").strip()
    if text and text not in seen:
        values.append(text)
        seen.add(text)
