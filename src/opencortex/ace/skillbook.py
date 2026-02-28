# SPDX-License-Identifier: Apache-2.0
"""Skillbook — CRUD + vector search + CortexFS three-layer persistence."""

import logging
from typing import Any, Dict, List, Optional

from opencortex.ace.types import Skill, UpdateOperation
from opencortex.models.embedder.base import EmbedderBase
from opencortex.storage.collection_schemas import init_skillbook_collection
from opencortex.storage.cortex_fs import CortexFS
from opencortex.storage.vikingdb_interface import VikingDBInterface
from opencortex.utils.time_utils import get_current_timestamp

logger = logging.getLogger(__name__)


class Skillbook:
    """ACE Skillbook: manages learned skills with vector search and filesystem persistence."""

    COLLECTION = "skillbooks"

    def __init__(
        self,
        storage: VikingDBInterface,
        embedder: EmbedderBase,
        cortex_fs: CortexFS,
        prefix: str,
        embedding_dim: int = 1024,
    ):
        self._storage = storage
        self._embedder = embedder
        self._fs = cortex_fs
        self._prefix = prefix  # "opencortex://{t}/user/{u}/skillbooks"
        self._dim = embedding_dim
        self._counters: Dict[str, int] = {}  # section -> next id number

    async def init(self) -> None:
        """Create the skillbooks collection if it doesn't exist."""
        await init_skillbook_collection(self._storage, self.COLLECTION, self._dim)

    # =========================================================================
    # CRUD
    # =========================================================================

    async def add_skill(self, section: str, content: str, **kwargs: Any) -> Skill:
        """Add a new skill to the Skillbook.

        Args:
            section: Skill section (strategies, error_fixes, patterns, general)
            content: L0 imperative sentence
            **kwargs: justification, evidence, trace, etc.

        Returns:
            The created Skill
        """
        counter = self._counters.get(section, 0)
        counter += 1
        self._counters[section] = counter

        prefix = section[:5]
        skill_id = f"{prefix}-{counter:05d}"

        now = get_current_timestamp()
        skill = Skill(
            id=skill_id,
            section=section,
            content=content,
            justification=kwargs.get("justification"),
            evidence=kwargs.get("evidence"),
            status=kwargs.get("status", "active"),
            created_at=now,
            updated_at=now,
        )

        await self._persist_skill(skill, trace=kwargs.get("trace", ""))
        return skill

    async def update_skill(
        self, skill_id: str, content: Optional[str] = None, **kwargs: Any
    ) -> Skill:
        """Update an existing skill.

        Args:
            skill_id: Skill ID to update
            content: New content (if changed, re-embeds)
            **kwargs: Other fields to update

        Returns:
            Updated Skill
        """
        records = await self._storage.get(self.COLLECTION, [skill_id])
        if not records:
            raise ValueError(f"Skill not found: {skill_id}")

        record = records[0]
        skill = Skill.from_dict(record)

        if content is not None:
            skill.content = content
        if "justification" in kwargs:
            skill.justification = kwargs["justification"]
        if "evidence" in kwargs:
            skill.evidence = kwargs["evidence"]
        if "status" in kwargs:
            skill.status = kwargs["status"]

        skill.updated_at = get_current_timestamp()
        await self._persist_skill(skill, trace=kwargs.get("trace", ""))
        return skill

    async def tag_skill(self, skill_id: str, tag: str, increment: int = 1) -> None:
        """Increment a skill's feedback counter.

        Args:
            skill_id: Skill ID
            tag: One of "helpful", "harmful", "neutral"
            increment: Amount to increment
        """
        records = await self._storage.get(self.COLLECTION, [skill_id])
        if not records:
            raise ValueError(f"Skill not found: {skill_id}")

        record = records[0]
        new_value = record.get(tag, 0) + increment
        total = (
            (new_value if tag == "helpful" else record.get("helpful", 0))
            + (new_value if tag == "harmful" else record.get("harmful", 0))
            + (new_value if tag == "neutral" else record.get("neutral", 0))
        )
        now = get_current_timestamp()

        await self._storage.update(
            self.COLLECTION,
            skill_id,
            {tag: new_value, "updated_at": now, "active_count": total},
        )

        # Update VikingFS L1 (overview)
        skill = Skill.from_dict(record)
        setattr(skill, tag, new_value)
        skill.updated_at = now
        uri = f"{self._prefix}/{skill.section}/{skill.id}"
        overview = self._build_overview(skill)
        await self._fs.write_context(uri=uri, overview=overview)

    async def remove_skill(self, skill_id: str) -> None:
        """Remove a skill from the Skillbook.

        Args:
            skill_id: Skill ID to remove
        """
        records = await self._storage.get(self.COLLECTION, [skill_id])
        if records:
            record = records[0]
            uri = record.get("uri", "")
            await self._storage.delete(self.COLLECTION, [skill_id])
            if uri:
                try:
                    await self._fs.rm(uri, recursive=True)
                except Exception:
                    logger.debug(f"[Skillbook] Could not clean VikingFS for {uri}")

    # =========================================================================
    # Search
    # =========================================================================

    async def search(
        self, query: str, limit: int = 5, section: Optional[str] = None
    ) -> List[Skill]:
        """Vector search for skills.

        Args:
            query: Search query text
            limit: Max results
            section: Optional section filter

        Returns:
            List of matching Skills
        """
        embed_result = self._embedder.embed(query)
        filter_cond: Dict[str, Any] = {
            "op": "must",
            "field": "context_type",
            "conds": ["ace_skill"],
        }
        if section:
            filter_cond = {
                "op": "and",
                "conds": [
                    filter_cond,
                    {"op": "must", "field": "type", "conds": [section]},
                ],
            }

        results = await self._storage.search(
            self.COLLECTION,
            query_vector=embed_result.dense_vector,
            filter=filter_cond,
            limit=limit,
        )
        skills = []
        for r in results:
            skill = Skill.from_dict(r)
            # Preserve vector search score for downstream use
            skill._score = r.get("_score", 0.0)
            skills.append(skill)
        return skills

    async def get_by_section(self, section: str) -> List[Skill]:
        """Get all skills in a section.

        Args:
            section: Section name

        Returns:
            List of Skills in the section
        """
        results = await self._storage.filter(
            self.COLLECTION,
            filter={
                "op": "and",
                "conds": [
                    {"op": "must", "field": "context_type", "conds": ["ace_skill"]},
                    {"op": "must", "field": "type", "conds": [section]},
                ],
            },
            limit=10000,
        )
        return [Skill.from_dict(r) for r in results]

    # =========================================================================
    # Prompt & Stats
    # =========================================================================

    async def as_prompt(self) -> str:
        """Return all active skills as a tab-separated table for LLM prompt injection."""
        all_skills = await self._storage.filter(
            self.COLLECTION,
            filter={
                "op": "and",
                "conds": [
                    {"op": "must", "field": "context_type", "conds": ["ace_skill"]},
                    {"op": "must", "field": "status", "conds": ["active"]},
                ],
            },
            limit=10000,
        )
        lines = ["ID\tSection\tContent\tHelpful\tHarmful"]
        for r in all_skills:
            skill = Skill.from_dict(r)
            lines.append(
                f"{skill.id}\t{skill.section}\t{skill.content}\t{skill.helpful}\t{skill.harmful}"
            )
        return "\n".join(lines)

    async def stats(self) -> Dict[str, Any]:
        """Return skill statistics."""
        total = await self._storage.count(
            self.COLLECTION,
            filter={"op": "must", "field": "context_type", "conds": ["ace_skill"]},
        )

        by_section: Dict[str, int] = {}
        for section in ["strategies", "error_fixes", "patterns", "general"]:
            count = await self._storage.count(
                self.COLLECTION,
                filter={
                    "op": "and",
                    "conds": [
                        {"op": "must", "field": "context_type", "conds": ["ace_skill"]},
                        {"op": "must", "field": "type", "conds": [section]},
                    ],
                },
            )
            if count > 0:
                by_section[section] = count

        return {"total": total, "by_section": by_section}

    # =========================================================================
    # Apply (dispatch UpdateOperation)
    # =========================================================================

    async def apply(self, op: UpdateOperation, trace: str = "") -> Optional[Skill]:
        """Apply an UpdateOperation, dispatching to the appropriate method.

        Args:
            op: The operation to apply
            trace: Optional trace/evidence text

        Returns:
            The affected Skill (or None for REMOVE)
        """
        if op.type == "ADD":
            return await self.add_skill(
                section=op.section,
                content=op.content or "",
                justification=op.justification,
                evidence=op.evidence,
                trace=trace,
            )
        elif op.type == "UPDATE":
            if not op.skill_id:
                raise ValueError("UPDATE requires skill_id")
            return await self.update_skill(
                skill_id=op.skill_id,
                content=op.content,
                justification=op.justification,
                evidence=op.evidence,
                trace=trace,
            )
        elif op.type == "TAG":
            if not op.skill_id:
                raise ValueError("TAG requires skill_id")
            for tag, increment in op.metadata.items():
                await self.tag_skill(op.skill_id, tag, increment)
            records = await self._storage.get(self.COLLECTION, [op.skill_id])
            return Skill.from_dict(records[0]) if records else None
        elif op.type == "REMOVE":
            if not op.skill_id:
                raise ValueError("REMOVE requires skill_id")
            await self.remove_skill(op.skill_id)
            return None
        else:
            raise ValueError(f"Unknown operation type: {op.type}")

    # =========================================================================
    # Section Summary
    # =========================================================================

    async def update_section_summary(self, section: str) -> None:
        """Update the section directory's L0/L1 summary."""
        skills = await self.get_by_section(section)
        section_uri = f"{self._prefix}/{section}"

        abstract = f"{section}: {len(skills)} skills"
        overview_lines = [f"# {section.title()} ({len(skills)} skills)\n"]
        for s in skills:
            status_mark = "" if s.status == "active" else f" [{s.status}]"
            overview_lines.append(
                f"- **{s.id}**: {s.content} (helpful:{s.helpful} harmful:{s.harmful}){status_mark}"
            )

        await self._fs.write_context(
            uri=section_uri,
            abstract=abstract,
            overview="\n".join(overview_lines),
        )

    # =========================================================================
    # Internal
    # =========================================================================

    async def _persist_skill(self, skill: Skill, trace: str = "") -> None:
        """Dual-write: Qdrant + VikingFS."""
        uri = f"{self._prefix}/{skill.section}/{skill.id}"

        # VikingFS L0/L1/L2
        overview = self._build_overview(skill)
        await self._fs.write_context(
            uri=uri,
            abstract=skill.content,
            overview=overview,
            content=trace,
            is_leaf=True,
        )

        # Qdrant vector write
        embed_result = self._embedder.embed(skill.content)
        await self._storage.upsert(
            self.COLLECTION,
            {
                "id": skill.id,
                "uri": uri,
                "abstract": skill.content,
                "context_type": "ace_skill",
                "type": skill.section,
                "vector": embed_result.dense_vector,
                "active_count": skill.helpful + skill.harmful + skill.neutral,
                "is_leaf": True,
                "helpful": skill.helpful,
                "harmful": skill.harmful,
                "neutral": skill.neutral,
                "status": skill.status,
                "created_at": skill.created_at,
                "updated_at": skill.updated_at,
            },
        )

    @staticmethod
    def _build_overview(skill: Skill) -> str:
        """Build L1 overview from a Skill."""
        lines = []
        if skill.justification:
            lines.append(f"## Justification\n{skill.justification}")
        if skill.evidence:
            lines.append(f"## Evidence\n{skill.evidence}")
        lines.append(
            f"## Tags\nhelpful: {skill.helpful} | harmful: {skill.harmful} | neutral: {skill.neutral}"
        )
        return "\n\n".join(lines)
