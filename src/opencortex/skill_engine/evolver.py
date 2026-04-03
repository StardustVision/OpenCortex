"""
SkillEvolver — generates new/improved skills via LLM.

Mirrors OpenSpace's three evolution types: CAPTURED, DERIVED, FIX.
FIX creates a new CANDIDATE version (not in-place) per spec section 4.7.
"""

import asyncio
import logging
import uuid
from typing import List, Optional

from opencortex.skill_engine.types import (
    SkillRecord, SkillLineage, SkillOrigin, SkillStatus,
    SkillCategory, SkillVisibility, EvolutionSuggestion,
    make_skill_uri, make_source_fingerprint,
)
from opencortex.skill_engine.prompts import (
    SKILL_EVOLVE_CAPTURED_PROMPT,
    SKILL_EVOLVE_DERIVED_PROMPT,
    SKILL_EVOLVE_FIX_PROMPT,
)

logger = logging.getLogger(__name__)

EVOLUTION_COMPLETE = "<EVOLUTION_COMPLETE>"
EVOLUTION_FAILED = "<EVOLUTION_FAILED>"
MAX_ITERATIONS = 5


class SkillEvolver:
    def __init__(self, llm, store):
        self._llm = llm
        self._store = store
        self._semaphore = asyncio.Semaphore(3)

    async def evolve(
        self, suggestion: EvolutionSuggestion,
        tenant_id: str, user_id: str,
    ) -> Optional[SkillRecord]:
        """Route to appropriate evolution method."""
        async with self._semaphore:
            match suggestion.evolution_type:
                case SkillOrigin.CAPTURED:
                    return await self._evolve_captured(suggestion, tenant_id, user_id)
                case SkillOrigin.DERIVED:
                    return await self._evolve_derived(suggestion, tenant_id, user_id)
                case SkillOrigin.FIXED:
                    return await self._evolve_fix(suggestion, tenant_id, user_id)
            return None

    async def process_suggestions(
        self, suggestions: List[EvolutionSuggestion],
        tenant_id: str, user_id: str,
    ) -> List[SkillRecord]:
        """Process all suggestions with concurrency control."""
        tasks = [self.evolve(s, tenant_id, user_id) for s in suggestions]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, SkillRecord)]

    async def _evolve_captured(self, s, tid, uid) -> Optional[SkillRecord]:
        prompt = SKILL_EVOLVE_CAPTURED_PROMPT.format(
            direction=s.direction,
            category=s.category.value,
            source_context=f"Pattern from {len(s.source_memory_ids)} memories",
        )
        content = await self._run_evolution_loop(prompt)
        if not content:
            return None

        # Deterministic skill_id from fingerprint — ensures upsert idempotency
        fp = make_source_fingerprint(s.source_memory_ids)
        skill_id = f"sk-{fp}"
        name = s.direction or "unnamed-skill"
        return SkillRecord(
            skill_id=skill_id, name=name,
            description=f"Extracted: {name}",
            content=content, category=s.category,
            status=SkillStatus.CANDIDATE,
            visibility=SkillVisibility.PRIVATE,
            lineage=SkillLineage(
                origin=SkillOrigin.CAPTURED,
                source_memory_ids=s.source_memory_ids,
                created_by="skill-evolver",
            ),
            tenant_id=tid, user_id=uid,
            uri=make_skill_uri(tid, uid, skill_id, visibility="private", category=s.category.value),
            abstract=name,
            source_fingerprint=fp,
        )

    async def _evolve_derived(self, s, tid, uid) -> Optional[SkillRecord]:
        parent = None
        if s.target_skill_ids:
            parent = await self._store.load_record(s.target_skill_ids[0])
        parent_content = parent.content if parent else "(no parent)"

        prompt = SKILL_EVOLVE_DERIVED_PROMPT.format(
            parent_content=parent_content,
            direction=s.direction,
        )
        content = await self._run_evolution_loop(prompt)
        if not content:
            return None

        skill_id = f"sk-{uuid.uuid4().hex[:12]}"
        gen = (parent.lineage.generation + 1) if parent else 0
        return SkillRecord(
            skill_id=skill_id, name=s.direction or "derived-skill",
            description=f"Derived: {s.direction}",
            content=content, category=s.category,
            status=SkillStatus.CANDIDATE,
            visibility=SkillVisibility.PRIVATE,
            lineage=SkillLineage(
                origin=SkillOrigin.DERIVED,
                generation=gen,
                parent_skill_ids=s.target_skill_ids,
                source_memory_ids=s.source_memory_ids,
                created_by="skill-evolver",
            ),
            tenant_id=tid, user_id=uid,
            uri=make_skill_uri(tid, uid, skill_id, visibility="private", category=s.category.value),
            abstract=s.direction or "",
        )

    async def _evolve_fix(self, s, tid, uid) -> Optional[SkillRecord]:
        if not s.target_skill_ids:
            return None
        parent = await self._store.load_record(s.target_skill_ids[0])
        if not parent:
            return None

        prompt = SKILL_EVOLVE_FIX_PROMPT.format(
            current_content=parent.content,
            direction=s.direction,
        )
        content = await self._run_evolution_loop(prompt)
        if not content:
            return None

        skill_id = f"sk-{uuid.uuid4().hex[:12]}"
        return SkillRecord(
            skill_id=skill_id, name=parent.name,
            description=parent.description,
            content=content, category=parent.category,
            status=SkillStatus.CANDIDATE,
            visibility=parent.visibility,
            lineage=SkillLineage(
                origin=SkillOrigin.FIXED,
                generation=parent.lineage.generation + 1,
                parent_skill_ids=[parent.skill_id],
                created_by="skill-evolver",
                change_summary=s.direction,
            ),
            tenant_id=tid, user_id=uid,
            uri=make_skill_uri(tid, uid, skill_id, visibility=parent.visibility.value, category=parent.category.value),
            abstract=parent.abstract,
            tags=parent.tags,
        )

    async def _run_evolution_loop(self, initial_prompt: str) -> Optional[str]:
        """LLM evolution loop: max 5 iterations, termination tokens."""
        messages = [{"role": "user", "content": initial_prompt}]

        for i in range(MAX_ITERATIONS):
            try:
                response = await self._llm.complete(messages)
            except Exception as exc:
                logger.warning("[SkillEvolver] LLM failed iteration %d: %s", i, exc)
                return None

            if EVOLUTION_COMPLETE in response:
                return response.replace(EVOLUTION_COMPLETE, "").strip()
            if EVOLUTION_FAILED in response:
                logger.info("[SkillEvolver] Evolution failed: %s", response)
                return None

            messages.append({"role": "assistant", "content": response})
            if i < MAX_ITERATIONS - 1:
                messages.append({"role": "user", "content":
                    f"Iteration {i+1}/{MAX_ITERATIONS}. "
                    f"End with {EVOLUTION_COMPLETE} or {EVOLUTION_FAILED}."
                })

        return None
