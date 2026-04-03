"""
Knowledge Store — persistent storage for knowledge items via Qdrant + CortexFS.

Supports CRUD operations and type-filtered vector search.
Only knowledge with status=active is returned by search (Design doc §8.4).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from opencortex.alpha.types import Knowledge, KnowledgeStatus, KnowledgeScope, SEARCHABLE_STATUSES

logger = logging.getLogger(__name__)


class KnowledgeStore:
    def __init__(
        self,
        storage,       # StorageInterface
        embedder,      # EmbedderBase
        cortex_fs,     # CortexFS
        collection_name: str = "knowledge",
        embedding_dim: int = 1024,
    ):
        self._storage = storage
        self._embedder = embedder
        self._fs = cortex_fs
        self._collection = collection_name
        self._dim = embedding_dim

    async def init(self) -> "KnowledgeStore":
        """Ensure collection exists."""
        from opencortex.storage.collection_schemas import init_knowledge_collection
        await init_knowledge_collection(self._storage, self._collection, self._dim)
        return self

    async def save(self, knowledge: Knowledge) -> str:
        """Save knowledge to Qdrant + CortexFS."""
        embed_text = knowledge.abstract or knowledge.statement or knowledge.knowledge_id
        embed_result = self._embedder.embed(embed_text)
        vector = embed_result.dense_vector

        record = {
            "id": knowledge.knowledge_id,
            "knowledge_id": knowledge.knowledge_id,
            "knowledge_type": knowledge.knowledge_type.value,
            "tenant_id": knowledge.tenant_id,
            "user_id": knowledge.user_id,
            "scope": knowledge.scope.value,
            "status": knowledge.status.value,
            "confidence": knowledge.confidence or 0.0,
            "training_ready": knowledge.training_ready,
            "abstract": knowledge.abstract or "",
            "overview": knowledge.overview or "",
            "vector": vector,
            "created_at": knowledge.created_at,
            "updated_at": knowledge.updated_at,
        }
        await self._storage.upsert(self._collection, record)

        # Write to CortexFS
        if self._fs:
            uri = (f"opencortex://{knowledge.tenant_id}/"
                   f"{knowledge.user_id}/knowledge/{knowledge.knowledge_id}")
            if knowledge.overview:
                await self._fs.write(uri, knowledge.overview, layer="overview")
            if knowledge.abstract:
                await self._fs.write(uri, knowledge.abstract, layer="abstract")

        return knowledge.knowledge_id

    async def search(
        self, query: str, tenant_id: str, user_id: str,
        types: Optional[List[str]] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Vector search over knowledge — only active items returned.

        Scope visibility:
        - USER scope: only visible to the owning user_id
        - TENANT/GLOBAL scope: visible to all users in the tenant
        """
        embed_result = self._embedder.embed_query(query)

        must_conds = [
            {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
            {"op": "must", "field": "status",
             "conds": [s.value for s in SEARCHABLE_STATUSES]},
        ]

        if types:
            must_conds.append(
                {"op": "must", "field": "knowledge_type", "conds": types}
            )

        # Scope isolation: user-scope only visible to owner
        scope_filter = {"op": "or", "conds": [
            {"op": "must", "field": "scope", "conds": [
                KnowledgeScope.TENANT.value,
                KnowledgeScope.GLOBAL.value,
            ]},
            {"op": "and", "conds": [
                {"op": "must", "field": "scope",
                 "conds": [KnowledgeScope.USER.value]},
                {"op": "must", "field": "user_id", "conds": [user_id]},
            ]},
        ]}
        must_conds.append(scope_filter)

        filter_expr = {"op": "and", "conds": must_conds}
        return await self._storage.search(
            self._collection, embed_result.dense_vector, filter_expr,
            limit=limit,
        )

    async def get(self, knowledge_id: str) -> Optional[Dict[str, Any]]:
        """Get knowledge by ID."""
        results = await self._storage.get(self._collection, [knowledge_id])
        return results[0] if results else None

    async def _update_status(self, knowledge_id: str, new_status: KnowledgeStatus) -> bool:
        """Update knowledge status."""
        existing = await self.get(knowledge_id)
        if not existing:
            return False
        existing["status"] = new_status.value
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        await self._storage.upsert(self._collection, existing)
        return True

    async def approve(self, knowledge_id: str) -> bool:
        """Transition knowledge to active status."""
        return await self._update_status(knowledge_id, KnowledgeStatus.ACTIVE)

    async def reject(self, knowledge_id: str) -> bool:
        """Deprecate a knowledge candidate."""
        return await self._update_status(knowledge_id, KnowledgeStatus.DEPRECATED)

    async def deprecate(self, knowledge_id: str) -> bool:
        """Deprecate knowledge."""
        return await self._update_status(knowledge_id, KnowledgeStatus.DEPRECATED)

    async def promote(self, knowledge_id: str, new_scope: str) -> bool:
        """Promote knowledge to a wider scope."""
        existing = await self.get(knowledge_id)
        if not existing:
            return False
        existing["scope"] = new_scope
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        await self._storage.upsert(self._collection, existing)
        return True

    async def list_candidates(
        self, tenant_id: str, user_id: str,
    ) -> List[Dict[str, Any]]:
        """List knowledge items pending approval.

        Scope visibility same as search(): user-scope candidates only
        visible to the owning user_id.
        """
        must_conds = [
            {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
            {"op": "must", "field": "status", "conds": [
                KnowledgeStatus.CANDIDATE.value,
                KnowledgeStatus.VERIFIED.value,
            ]},
        ]
        # Scope isolation: user-scope only visible to owner
        scope_filter = {"op": "or", "conds": [
            {"op": "must", "field": "scope", "conds": [
                KnowledgeScope.TENANT.value,
                KnowledgeScope.GLOBAL.value,
            ]},
            {"op": "and", "conds": [
                {"op": "must", "field": "scope",
                 "conds": [KnowledgeScope.USER.value]},
                {"op": "must", "field": "user_id", "conds": [user_id]},
            ]},
        ]}
        must_conds.append(scope_filter)
        filter_expr = {"op": "and", "conds": must_conds}
        return await self._storage.filter(self._collection, filter_expr)
