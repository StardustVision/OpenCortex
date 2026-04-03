"""
SkillEventStore — durable skill usage events in independent Qdrant collection.

Events keyed by (tenant_id, user_id, session_id) to prevent cross-user collision.
No vectors — pure metadata store.
"""

import logging
from typing import Any, Dict, List

from opencortex.skill_engine.types import SkillEvent

logger = logging.getLogger(__name__)

SKILL_EVENTS_COLLECTION = "skill_events"


class SkillEventStore:
    def __init__(self, storage, collection_name: str = SKILL_EVENTS_COLLECTION):
        self._storage = storage
        self._collection = collection_name

    async def init(self) -> None:
        """Create skill_events collection if not exists."""
        from opencortex.storage.collection_schemas import init_skill_events_collection
        await init_skill_events_collection(self._storage, self._collection)

    async def append(self, event: SkillEvent) -> None:
        """Persist a skill event."""
        payload = event.to_dict()
        payload["id"] = event.event_id
        await self._storage.upsert(self._collection, payload)

    async def list_by_session(
        self, session_id: str, tenant_id: str, user_id: str,
    ) -> List[SkillEvent]:
        """List events for a session (tenant+user+session isolated)."""
        filter_expr = {"op": "and", "conds": [
            {"op": "must", "field": "session_id", "conds": [session_id]},
            {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
            {"op": "must", "field": "user_id", "conds": [user_id]},
        ]}
        results = await self._storage.filter(self._collection, filter_expr, limit=200)
        return [self._dict_to_event(r) for r in results]

    async def mark_evaluated(self, event_ids: List[str]) -> None:
        """Mark events as evaluated (idempotency guard)."""
        if not event_ids:
            return
        import asyncio
        await asyncio.gather(*[
            self._storage.update(self._collection, eid, {"evaluated": True})
            for eid in event_ids
        ])

    async def list_unevaluated(
        self, tenant_id: str, limit: int = 100,
    ) -> List[SkillEvent]:
        """List unevaluated events for crash recovery sweeper."""
        conds = [{"op": "must", "field": "evaluated", "conds": [False]}]
        if tenant_id:
            conds.append({"op": "must", "field": "tenant_id", "conds": [tenant_id]})
        filter_expr = {"op": "and", "conds": conds} if len(conds) > 1 else conds[0]
        results = await self._storage.filter(self._collection, filter_expr, limit=limit)
        return [self._dict_to_event(r) for r in results]

    def _dict_to_event(self, d: Dict[str, Any]) -> SkillEvent:
        return SkillEvent(
            event_id=d.get("event_id", d.get("id", "")),
            session_id=d.get("session_id", ""),
            turn_id=d.get("turn_id", ""),
            skill_id=d.get("skill_id", ""),
            skill_uri=d.get("skill_uri", ""),
            tenant_id=d.get("tenant_id", ""),
            user_id=d.get("user_id", ""),
            event_type=d.get("event_type", ""),
            outcome=d.get("outcome", ""),
            timestamp=d.get("timestamp", ""),
            evaluated=d.get("evaluated", False),
        )
