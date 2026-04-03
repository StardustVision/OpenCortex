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
        await self._authorized_status_change(skill_id, tenant_id, user_id, SkillStatus.ACTIVE)

    async def reject(self, skill_id: str, tenant_id: str, user_id: str) -> None:
        await self._authorized_status_change(skill_id, tenant_id, user_id, SkillStatus.DEPRECATED)

    async def deprecate(self, skill_id: str, tenant_id: str, user_id: str) -> None:
        await self._authorized_status_change(skill_id, tenant_id, user_id, SkillStatus.DEPRECATED)

    async def promote(self, skill_id: str, tenant_id: str, user_id: str) -> None:
        """Promote skill from PRIVATE to SHARED visibility.

        Only the owner can promote. Regenerates URI to shared format.
        """
        from opencortex.skill_engine.types import SkillVisibility, make_skill_uri
        record = await self.get_skill(skill_id, tenant_id, user_id)
        if not record:
            raise ValueError(f"Skill {skill_id} not found or not authorized")
        if record.visibility == SkillVisibility.SHARED:
            raise ValueError(f"Skill {skill_id} is already shared")
        if record.user_id != user_id:
            raise ValueError(f"Only the owner can promote a skill")
        # Regenerate URI from private to shared format
        new_uri = make_skill_uri(
            tenant_id, user_id, skill_id,
            visibility="shared", category=record.category.value,
        )
        await self._store.update_visibility(skill_id, SkillVisibility.SHARED, new_uri)

    async def list_skills(self, tenant_id: str, user_id: str,
                          status: Optional[SkillStatus] = None) -> List[SkillRecord]:
        if status:
            return await self._store.load_by_status(tenant_id, user_id, status)
        active = await self._store.load_by_status(tenant_id, user_id, SkillStatus.ACTIVE)
        candidates = await self._store.load_by_status(tenant_id, user_id, SkillStatus.CANDIDATE)
        return active + candidates

    async def get_skill(self, skill_id: str, tenant_id: str,
                        user_id: str) -> Optional[SkillRecord]:
        """Get skill with visibility check — returns None if not authorized."""
        record = await self._store.load_record(skill_id)
        if not record:
            return None
        if not self._is_visible(record, tenant_id, user_id):
            return None
        return record

    # --- Authorization helpers ---

    def _is_visible(self, record: SkillRecord, tenant_id: str, user_id: str) -> bool:
        """Check if a skill is visible to the given tenant/user."""
        from opencortex.skill_engine.types import SkillVisibility
        if record.tenant_id != tenant_id:
            return False
        if record.visibility == SkillVisibility.SHARED:
            return True
        # PRIVATE: only visible to owner
        return record.user_id == user_id

    async def _authorized_status_change(
        self, skill_id: str, tenant_id: str, user_id: str,
        new_status: SkillStatus,
    ) -> None:
        """Load skill with visibility check, then change status."""
        record = await self.get_skill(skill_id, tenant_id, user_id)
        if not record:
            raise ValueError(f"Skill {skill_id} not found or not authorized")
        await self._store.update_status(skill_id, new_status)

    # --- Extraction pipeline ---

    @property
    def extraction_available(self) -> bool:
        """Whether the extraction pipeline is configured."""
        return self._analyzer is not None and self._evolver is not None

    async def extract(self, tenant_id: str, user_id: str,
                      **filters) -> List[SkillRecord]:
        """Full pipeline: scan → analyze → evolve → save candidates."""
        if not self.extraction_available:
            raise RuntimeError("Extraction pipeline not available: SourceAdapter not configured")

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
        # Authorize: must be able to see the parent skill
        parent = await self.get_skill(skill_id, tenant_id, user_id)
        if not parent:
            raise ValueError(f"Skill {skill_id} not found or not authorized")
        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.FIXED,
            target_skill_ids=[skill_id],
            category=parent.category,
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
        # Authorize: must be able to see the parent skill
        parent = await self.get_skill(skill_id, tenant_id, user_id)
        if not parent:
            raise ValueError(f"Skill {skill_id} not found or not authorized")
        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.DERIVED,
            target_skill_ids=[skill_id],
            category=parent.category,
            direction=direction,
        )
        result = await self._evolver.evolve(suggestion, tenant_id, user_id)
        if result:
            await self._store.save_record(result)
        return result
