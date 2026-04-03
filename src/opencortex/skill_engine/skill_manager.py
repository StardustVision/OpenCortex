"""
SkillManager — top-level API for the Skill Engine.

Orchestrates search, approval, and listing. Extract/evolve will be added in Task 7.
"""

import logging
from typing import List, Optional

from opencortex.skill_engine.types import SkillRecord, SkillStatus

logger = logging.getLogger(__name__)


class SkillManager:
    def __init__(self, store, analyzer=None, evolver=None):
        self._store = store
        self._analyzer = analyzer
        self._evolver = evolver

    async def search(self, query: str, tenant_id: str, user_id: str,
                     top_k: int = 5) -> List[SkillRecord]:
        return await self._store.search(query, tenant_id, user_id, top_k=top_k)

    async def approve(self, skill_id: str, tenant_id: str, user_id: str) -> None:
        await self._store.activate(skill_id)

    async def reject(self, skill_id: str, tenant_id: str, user_id: str) -> None:
        await self._store.deprecate(skill_id)

    async def deprecate(self, skill_id: str, tenant_id: str, user_id: str) -> None:
        await self._store.deprecate(skill_id)

    async def list_skills(self, tenant_id: str, user_id: str,
                          status: Optional[SkillStatus] = None) -> List[SkillRecord]:
        if status:
            return await self._store.load_by_status(tenant_id, user_id, status)
        active = await self._store.load_by_status(tenant_id, user_id, SkillStatus.ACTIVE)
        candidates = await self._store.load_by_status(tenant_id, user_id, SkillStatus.CANDIDATE)
        return active + candidates

    async def get_skill(self, skill_id: str, tenant_id: str,
                        user_id: str) -> Optional[SkillRecord]:
        return await self._store.load_record(skill_id)
