# SPDX-License-Identifier: Apache-2.0
"""Cortex URI namespace helpers."""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

from opencortex.http.request_context import (
    get_effective_identity,
    get_effective_project_id,
)
from opencortex.retrieve.types import ContextType
from opencortex.services.memory_filters import FilterExpr
from opencortex.store.types import MemoryCategory
from opencortex.utils.semantic_name import semantic_node_name
from opencortex.utils.uri import CortexURI

_USER_MEMORY_CATEGORIES = {
    "profile",
    "preferences",
    "entities",
    str(MemoryCategory.EVENTS),
}
_MEMORY_SCOPE = "memories"


class CortexNamespace:
    """Resolve Cortex URIs and uniqueness inside one storage collection."""

    def __init__(self, *, storage: Any, collection_resolver: Any) -> None:
        self._storage = storage
        self._collection_resolver = collection_resolver

    async def resolve(
        self,
        *,
        context_type: ContextType,
        category: str,
        abstract: str,
    ) -> tuple[str, str]:
        """Return a unique URI and its parent URI."""
        uri = await self.unique_uri(
            self.auto_uri(
                context_type=context_type,
                category=category,
                abstract=abstract,
            )
        )
        return uri, self.parent_uri(uri)

    def auto_uri(
        self,
        *,
        context_type: ContextType,
        category: str,
        abstract: str = "",
    ) -> str:
        """Generate a URI based on context type, category, and abstract text."""
        tenant_id, user_id = get_effective_identity()
        node_name = semantic_node_name(abstract) if abstract else uuid4().hex

        if context_type == ContextType.MEMORY:
            resolved_category = (
                category
                if category in _USER_MEMORY_CATEGORIES
                else str(MemoryCategory.EVENTS)
            )
            return CortexURI.build_private(
                tenant_id,
                user_id,
                _MEMORY_SCOPE,
                resolved_category,
                node_name,
            )

        if context_type == ContextType.CASE:
            return CortexURI.build_shared(tenant_id, "shared", "cases", node_name)

        if context_type == ContextType.PATTERN:
            return CortexURI.build_shared(tenant_id, "shared", "patterns", node_name)

        if context_type == ContextType.RESOURCE:
            project_id = get_effective_project_id()
            if category:
                return CortexURI.build_shared(
                    tenant_id,
                    "resources",
                    project_id,
                    category,
                    node_name,
                )
            return CortexURI.build_shared(
                tenant_id,
                "resources",
                project_id,
                node_name,
            )

        if context_type == ContextType.STAGING:
            return CortexURI.build_private(tenant_id, user_id, "staging", node_name)

        return CortexURI.build_private(
            tenant_id,
            user_id,
            _MEMORY_SCOPE,
            str(MemoryCategory.EVENTS),
            node_name,
        )

    def session_events_parent(self, session_id: str) -> str:
        """Return the parent URI for session event records."""
        tenant_id, user_id = get_effective_identity()
        return CortexURI.build_private(
            tenant_id,
            user_id,
            _MEMORY_SCOPE,
            str(MemoryCategory.EVENTS),
            session_id,
        )

    def session_immediate_uri(self) -> str:
        """Return a URI for one immediate session message."""
        tenant_id, user_id = get_effective_identity()
        return CortexURI.build_private(
            tenant_id,
            user_id,
            _MEMORY_SCOPE,
            str(MemoryCategory.EVENTS),
            uuid4().hex,
        )

    def session_merged_uri(self, session_id: str, msg_range: list[int]) -> str:
        """Return a stable URI for one merged session message range."""
        tenant_id, user_id = get_effective_identity()
        session_hash = hashlib.md5(session_id.encode("utf-8")).hexdigest()[:12]
        node_name = (
            f"conversation-{session_hash}-{int(msg_range[0]):06d}-"
            f"{int(msg_range[1]):06d}"
        )
        return CortexURI.build_private(
            tenant_id,
            user_id,
            _MEMORY_SCOPE,
            str(MemoryCategory.EVENTS),
            node_name,
        )

    def session_final_uri(self, session_id: str) -> str:
        """Return the stable final primary record URI for one session."""
        tenant_id, user_id = get_effective_identity()
        return CortexURI.build_private(
            tenant_id,
            user_id,
            _MEMORY_SCOPE,
            str(MemoryCategory.EVENTS),
            f"{session_id}-final",
        )

    async def unique_uri(self, uri: str, max_attempts: int = 100) -> str:
        """Return a URI that does not already exist in storage."""
        if not await self.exists(uri):
            return uri
        for index in range(1, max_attempts + 1):
            candidate = f"{uri}_{index}"
            if not await self.exists(candidate):
                return candidate
        raise ValueError(
            f"URI conflict unresolved after {max_attempts} attempts: {uri}"
        )

    async def exists(self, uri: str) -> bool:
        """Return whether a URI already exists in the active collection."""
        try:
            results = await self._storage.filter(
                self._collection_resolver(),
                FilterExpr.eq("uri", uri).to_dict(),
                limit=1,
            )
            return len(results) > 0
        except Exception:
            return False

    @staticmethod
    def parent_uri(uri: str) -> str:
        """Return parent URI for one Cortex URI."""
        parent = CortexURI(uri).parent
        return parent.uri if parent is not None else ""
