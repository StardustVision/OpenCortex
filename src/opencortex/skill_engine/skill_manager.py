"""
SkillManager — top-level API for the Skill Engine.

Orchestrates search, approval, and listing. Extract/evolve will be added in Task 7.
"""

import logging
from typing import List, Optional

from opencortex.skill_engine.types import (
    SkillRecord, SkillStatus, SkillOrigin, SkillCategory, EvolutionSuggestion,
)

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

    # --- Extraction pipeline ---

    async def extract(self, tenant_id: str, user_id: str,
                      **filters) -> List[SkillRecord]:
        """Full pipeline: scan → analyze → evolve → save candidates."""
        if not self._analyzer or not self._evolver:
            return []

        suggestions = await self._analyzer.extract_candidates(
            tenant_id, user_id, **filters,
        )
        if not suggestions:
            return []

        candidates = await self._evolver.process_suggestions(
            suggestions, tenant_id, user_id,
        )

        saved = []
        for c in candidates:
            await self._store.save_record(c)
            saved.append(c)

        return saved

    # --- Manual evolution ---

    async def fix_skill(self, skill_id: str, tenant_id: str, user_id: str,
                        direction: str) -> Optional[SkillRecord]:
        """Trigger FIX evolution → new CANDIDATE linked to parent."""
        if not self._evolver:
            return None
        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.FIXED,
            target_skill_ids=[skill_id],
            category=SkillCategory.WORKFLOW,
            direction=direction,
        )
        result = await self._evolver.evolve(suggestion, tenant_id, user_id)
        if result:
            await self._store.save_record(result)
        return result

    async def derive_skill(self, skill_id: str, tenant_id: str, user_id: str,
                           direction: str) -> Optional[SkillRecord]:
        """Trigger DERIVED evolution → new CANDIDATE."""
        if not self._evolver:
            return None
        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.DERIVED,
            target_skill_ids=[skill_id],
            category=SkillCategory.WORKFLOW,
            direction=direction,
        )
        result = await self._evolver.evolve(suggestion, tenant_id, user_id)
        if result:
            await self._store.save_record(result)
        return result
