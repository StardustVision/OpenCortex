"""Storage adapter — writes to independent skills Qdrant collection."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from opencortex.skill_engine.types import (
    SkillRecord, SkillStatus, SkillVisibility, SkillLineage, SkillOrigin,
    SkillCategory,
)

logger = logging.getLogger(__name__)

SKILLS_COLLECTION = "skills"


class SkillStorageAdapter:
    """Qdrant-backed storage for skills in an independent collection."""

    def __init__(self, storage, embedder, collection_name: str = SKILLS_COLLECTION,
                 embedding_dim: int = 1024):
        self._storage = storage
        self._embedder = embedder
        self._collection = collection_name
        self._dim = embedding_dim

    async def initialize(self) -> None:
        """Ensure skills collection exists."""
        from opencortex.storage.collection_schemas import init_skills_collection
        await init_skills_collection(self._storage, self._collection, self._dim)

    async def save(self, record: SkillRecord) -> None:
        """Upsert a skill record with embedding."""
        embed_text = f"{record.name} {record.description} {record.abstract}"
        embed_result = self._embedder.embed(embed_text)

        payload = record.to_dict()
        payload["id"] = record.skill_id
        payload["vector"] = embed_result.dense_vector

        await self._storage.upsert(self._collection, payload)

    async def load(self, skill_id: str) -> Optional[SkillRecord]:
        """Load a single skill by ID."""
        results = await self._storage.get(self._collection, [skill_id])
        if not results:
            return None
        return self._dict_to_record(results[0])

    async def load_all(self, tenant_id: str, user_id: str,
                       status: Optional[SkillStatus] = None) -> List[SkillRecord]:
        """Load all skills visible to this user."""
        conds = [self._visibility_filter(tenant_id, user_id)]
        if status:
            conds.append({"op": "must", "field": "status", "conds": [status.value]})
        filter_expr = {"op": "and", "conds": conds}
        results = await self._storage.filter(self._collection, filter_expr, limit=500)
        return [self._dict_to_record(r) for r in results]

    async def update_status(self, skill_id: str, status: SkillStatus) -> None:
        """Update skill status."""
        now = datetime.now(timezone.utc).isoformat()
        await self._storage.update(
            self._collection, skill_id,
            {"status": status.value, "updated_at": now},
        )

    async def update_metrics(self, skill_id: str, **counters) -> None:
        """Increment quality counters."""
        existing = await self.load(skill_id)
        if not existing:
            return
        updates = {}
        for k, v in counters.items():
            if hasattr(existing, k):
                updates[k] = getattr(existing, k) + v
        if updates:
            await self._storage.update(self._collection, skill_id, updates)

    async def search(self, query: str, tenant_id: str, user_id: str,
                     top_k: int = 5,
                     status: Optional[SkillStatus] = None) -> List[SkillRecord]:
        """Vector search with visibility + status filter."""
        embed_result = self._embedder.embed(query)
        conds = [self._visibility_filter(tenant_id, user_id)]
        if status:
            conds.append({"op": "must", "field": "status", "conds": [status.value]})
        else:
            conds.append({"op": "must", "field": "status", "conds": [SkillStatus.ACTIVE.value]})
        filter_expr = {"op": "and", "conds": conds}
        results = await self._storage.search(
            self._collection, embed_result.dense_vector, filter_expr, limit=top_k,
        )
        return [self._dict_to_record(r) for r in results]

    async def find_by_fingerprint(self, fingerprint: str) -> Optional[SkillRecord]:
        """Find skill by source fingerprint (for extraction idempotency)."""
        filter_expr = {"op": "must", "field": "source_fingerprint", "conds": [fingerprint]}
        results = await self._storage.filter(self._collection, filter_expr, limit=1)
        if not results:
            return None
        return self._dict_to_record(results[0])

    async def delete(self, skill_id: str) -> None:
        """Delete a skill by ID."""
        await self._storage.delete(self._collection, [skill_id])

    def _visibility_filter(self, tenant_id: str, user_id: str) -> Dict[str, Any]:
        """Build scope filter: SHARED visible to tenant, PRIVATE to owner only."""
        return {"op": "or", "conds": [
            {"op": "and", "conds": [
                {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
                {"op": "must", "field": "visibility", "conds": [SkillVisibility.SHARED.value]},
            ]},
            {"op": "and", "conds": [
                {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
                {"op": "must", "field": "visibility", "conds": [SkillVisibility.PRIVATE.value]},
                {"op": "must", "field": "user_id", "conds": [user_id]},
            ]},
        ]}

    def _dict_to_record(self, d: Dict[str, Any]) -> SkillRecord:
        """Convert Qdrant payload dict to SkillRecord."""
        lineage_data = d.get("lineage", {})
        if isinstance(lineage_data, str):
            lineage_data = {}
        lineage = SkillLineage(
            origin=SkillOrigin(lineage_data.get("origin", "captured")),
            generation=lineage_data.get("generation", 0),
            parent_skill_ids=lineage_data.get("parent_skill_ids", []),
            source_memory_ids=lineage_data.get("source_memory_ids", []),
            change_summary=lineage_data.get("change_summary", ""),
            content_diff=lineage_data.get("content_diff", ""),
            content_snapshot=lineage_data.get("content_snapshot", {}),
            created_by=lineage_data.get("created_by", ""),
            created_at=lineage_data.get("created_at", ""),
        )
        return SkillRecord(
            skill_id=d.get("skill_id", d.get("id", "")),
            name=d.get("name", ""),
            description=d.get("description", ""),
            content=d.get("content", ""),
            category=SkillCategory(d.get("category", "workflow")),
            status=SkillStatus(d.get("status", "candidate")),
            visibility=SkillVisibility(d.get("visibility", "private")),
            lineage=lineage,
            tags=d.get("tags", []),
            tenant_id=d.get("tenant_id", ""),
            user_id=d.get("user_id", ""),
            uri=d.get("uri", ""),
            total_selections=d.get("total_selections", 0),
            total_applied=d.get("total_applied", 0),
            total_completions=d.get("total_completions", 0),
            total_fallbacks=d.get("total_fallbacks", 0),
            abstract=d.get("abstract", ""),
            overview=d.get("overview", ""),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            source_fingerprint=d.get("source_fingerprint", ""),
        )
