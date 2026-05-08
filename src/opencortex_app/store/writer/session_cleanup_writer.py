# SPDX-License-Identifier: Apache-2.0
"""Writer for session cleanup side effects."""

from __future__ import annotations

from typing import Any

from opencortex_app.store.event.events import SessionMergedEvent


class SessionCleanupWriter:
    """Remove immediate records after they are merged."""

    def __init__(self, *, vector_store: Any, collection_resolver: Any) -> None:
        self.vector_store = vector_store
        self.collection_resolver = collection_resolver

    async def write(self, event: SessionMergedEvent) -> None:
        """Delete source immediate records after merged leaf content is stored."""
        collection = self.collection_resolver()
        for uri in event.source_uris:
            if uri:
                await self.vector_store.remove_by_uri(collection, uri)
