"""
SkillAnalyzer — extract operational skill candidates from memory clusters.

Core divergence from OpenSpace: analyzes memories instead of execution recordings.
"""

import logging
from typing import List, Optional

import orjson

from opencortex.skill_engine.types import (
    EvolutionSuggestion, SkillOrigin, SkillCategory,
    make_source_fingerprint,
)
from opencortex.skill_engine.prompts import SKILL_EXTRACT_PROMPT
from opencortex.skill_engine.memory_formatter import (
    format_cluster_for_extraction, format_existing_skills,
)

logger = logging.getLogger(__name__)


class SkillAnalyzer:
    def __init__(self, source, llm, store):
        self._source = source
        self._llm = llm
        self._store = store

    async def extract_candidates(
        self, tenant_id: str, user_id: str,
        context_types: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
    ) -> List[EvolutionSuggestion]:
        """Full extraction pipeline: scan -> cluster -> analyze -> dedup."""
        clusters = await self._source.scan_memories(
            tenant_id, user_id,
            context_types=context_types,
            categories=categories,
        )

        all_suggestions = []
        for cluster in clusters:
            fp = make_source_fingerprint(cluster.memory_ids)
            existing = await self._store.find_by_fingerprint(fp)
            if existing:
                logger.debug(
                    "[SkillAnalyzer] Cluster %s already extracted (fp=%s)",
                    cluster.cluster_id, fp,
                )
                continue

            suggestions = await self._analyze_cluster(
                cluster, tenant_id, user_id, fp,
            )
            if suggestions:
                all_suggestions.extend(suggestions)

        return all_suggestions

    async def _analyze_cluster(
        self, cluster, tenant_id: str, user_id: str,
        fingerprint: str,
    ) -> List[EvolutionSuggestion]:
        """Analyze a single cluster via LLM."""
        memories = await self._source.get_cluster_memories(cluster)
        if not memories:
            return []

        existing_active = await self._store.load_active(tenant_id, user_id)

        cluster_content = format_cluster_for_extraction(memories)
        existing_text = format_existing_skills(existing_active)

        prompt = SKILL_EXTRACT_PROMPT.format(
            cluster_content=cluster_content,
            existing_skills=existing_text,
        )

        try:
            response = await self._llm.complete([
                {"role": "user", "content": prompt},
            ])
            items = orjson.loads(response)
        except Exception as exc:
            logger.warning("[SkillAnalyzer] LLM/parse failed for cluster %s: %s",
                           cluster.cluster_id, exc)
            return []

        if not isinstance(items, list):
            return []

        suggestions = []
        for item in items:
            cat_str = item.get("category", "workflow")
            try:
                category = SkillCategory(cat_str)
            except ValueError:
                category = SkillCategory.WORKFLOW

            suggestions.append(EvolutionSuggestion(
                evolution_type=SkillOrigin.CAPTURED,
                target_skill_ids=[],
                category=category,
                direction=item.get("name", ""),
                confidence=item.get("confidence", 0.0),
                source_memory_ids=item.get("source_memory_ids", cluster.memory_ids),
            ))

        return suggestions
