"""
SkillStore — CRUD + lifecycle management for skills.

Mirrors OpenSpace SkillStore interface, delegates to StorageAdapter.
"""

import logging
from typing import List, Optional

from opencortex.skill_engine.types import SkillRecord, SkillStatus

logger = logging.getLogger(__name__)


class SkillStore:
    def __init__(self, storage_adapter):
        self._storage = storage_adapter

    async def save_record(self, record: SkillRecord) -> None:
        await self._storage.save(record)

    async def load_record(self, skill_id: str) -> Optional[SkillRecord]:
        return await self._storage.load(skill_id)

    async def load_active(self, tenant_id: str, user_id: str) -> List[SkillRecord]:
        return await self._storage.load_all(tenant_id, user_id, status=SkillStatus.ACTIVE)

    async def load_by_status(self, tenant_id: str, user_id: str,
                              status: SkillStatus) -> List[SkillRecord]:
        return await self._storage.load_all(tenant_id, user_id, status=status)

    async def activate(self, skill_id: str) -> None:
        await self._storage.update_status(skill_id, SkillStatus.ACTIVE)

    async def deprecate(self, skill_id: str) -> None:
        await self._storage.update_status(skill_id, SkillStatus.DEPRECATED)

    async def evolve_skill(self, new: SkillRecord, parent_ids: List[str]) -> None:
        """Save new skill version and deprecate parents (for FIX evolution)."""
        await self._storage.save(new)
        for pid in parent_ids:
            await self._storage.update_status(pid, SkillStatus.DEPRECATED)

    async def record_selection(self, skill_id: str) -> None:
        await self._storage.update_metrics(skill_id, total_selections=1)

    async def record_application(self, skill_id: str, completed: bool) -> None:
        counters = {"total_applied": 1}
        if completed:
            counters["total_completions"] = 1
        else:
            counters["total_fallbacks"] = 1
        await self._storage.update_metrics(skill_id, **counters)

    async def search(self, query: str, tenant_id: str, user_id: str,
                     top_k: int = 5) -> List[SkillRecord]:
        return await self._storage.search(query, tenant_id, user_id, top_k=top_k)

    async def find_by_fingerprint(self, fingerprint: str) -> Optional[SkillRecord]:
        return await self._storage.find_by_fingerprint(fingerprint)
