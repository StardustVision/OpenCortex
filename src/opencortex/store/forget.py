# SPDX-License-Identifier: Apache-2.0
"""Memory forget flow for semantic and URI-based deletion."""

from __future__ import annotations

from typing import Any

from opencortex.core.identity import IdentityProfile
from opencortex.store.schemas import MemoryForgetRequest, MemoryForgetResult
from opencortex.vector.retrieval import MemoryRetriever, RetrievalRequest


class MemoryForgetter:
    """Forget one recalled memory and its derived retrieval projections."""

    def __init__(
        self,
        *,
        vector_store: Any,
        collection_resolver: Any,
        cortex_storage: Any,
        retriever: MemoryRetriever,
    ) -> None:
        self.vector_store = vector_store
        self.collection_resolver = collection_resolver
        self.cortex_storage = cortex_storage
        self.retriever = retriever

    async def forget(
        self,
        request: MemoryForgetRequest,
        *,
        profile: IdentityProfile,
    ) -> MemoryForgetResult:
        """Forget the top semantic match, or the explicit URI when provided."""
        if request.query.strip():
            uri = await self.semantic_uri(request.query, profile=profile)
            if not uri:
                return MemoryForgetResult(matched_by="query")
            return await self.forget_uri(uri, matched_by="query")
        return await self.forget_uri(request.uri, matched_by="uri")

    async def semantic_uri(self, query: str, *, profile: IdentityProfile) -> str:
        """Return the URI of the best semantic forget candidate."""
        result = await self.retriever.search(
            RetrievalRequest(query=query, limit=1),
            profile=profile,
        )
        if not result.results:
            return ""
        return result.results[0].uri

    async def forget_uri(self, uri: str, *, matched_by: str) -> MemoryForgetResult:
        """Delete one URI subtree from vector storage and CFS."""
        target_uri = uri.strip()
        if not target_uri:
            raise ValueError("forget uri is required")
        if not target_uri.startswith("opencortex://"):
            raise ValueError("forget uri must start with opencortex://")

        removed = await self.vector_store.remove_by_uri(
            self.collection_resolver(),
            target_uri,
        )
        qdrant_removed = bool(removed)
        fs_removed = await self.remove_fs(target_uri)
        return MemoryForgetResult(
            forgotten=1 if qdrant_removed or fs_removed else 0,
            uri=target_uri,
            matched_by=matched_by,
            qdrant_removed=qdrant_removed,
            fs_removed=fs_removed,
        )

    async def remove_fs(self, uri: str) -> bool:
        """Remove a CFS subtree if it exists."""
        try:
            await self.cortex_storage.rm(uri, recursive=True)
        except FileNotFoundError:
            return False
        return True
