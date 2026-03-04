# SPDX-License-Identifier: Apache-2.0
"""Skillbook — CRUD + vector search + CortexFS three-layer persistence."""

import asyncio
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from opencortex.ace.types import Skill, UpdateOperation
from opencortex.http.request_context import get_effective_ace_config
from opencortex.models.embedder.base import EmbedderBase
from opencortex.storage.collection_schemas import init_skillbook_collection
from opencortex.storage.cortex_fs import CortexFS
from opencortex.storage.vikingdb_interface import VikingDBInterface
from opencortex.utils.time_utils import get_current_timestamp

logger = logging.getLogger(__name__)


def validate_skill_meta(record: dict) -> dict:
    """Read-time compat: fill missing evolution fields."""
    record.setdefault("confidence_score", 0.5)
    record.setdefault("version", 1)
    record.setdefault("trigger_conditions", [])
    record.setdefault("action_template", [])
    record.setdefault("success_metric", "")
    record.setdefault("source_case_uris", [])
    record.setdefault("supersedes_uri", "")
    record.setdefault("superseded_by_uri", "")
    return record


class SkillAuthorizationError(ValueError):
    """Raised when a user lacks permission to modify a skill."""


class Skillbook:
    """ACE Skillbook: manages learned skills with vector search and filesystem persistence."""

    COLLECTION = "skillbooks"

    def __init__(
        self,
        storage: VikingDBInterface,
        embedder: EmbedderBase,
        cortex_fs: CortexFS,
        prefix: str = "",
        embedding_dim: int = 1024,
    ):
        self._storage = storage
        self._embedder = embedder
        self._fs = cortex_fs
        self._prefix = prefix  # "opencortex://{t}/shared/skills"
        self._dim = embedding_dim

    async def init(self) -> None:
        """Create the skillbooks collection if it doesn't exist."""
        await init_skillbook_collection(self._storage, self.COLLECTION, self._dim)

    # =========================================================================
    # CRUD
    # =========================================================================

    async def add_skill(
        self,
        section: str,
        content: str,
        tenant_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> Skill:
        """Add a new skill to the Skillbook.

        Args:
            section: Skill section (strategies, error_fixes, patterns, general)
            content: L0 imperative sentence
            tenant_id: Tenant ID for scope isolation
            user_id: User ID for scope isolation
            **kwargs: justification, evidence, trace, etc.

        Returns:
            The created Skill
        """
        skill_id = str(uuid.uuid4())

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
            confidence_score=kwargs.get("confidence_score", 0.5),
            version=kwargs.get("version", 1),
            trigger_conditions=kwargs.get("trigger_conditions", []),
            action_template=kwargs.get("action_template", []),
            success_metric=kwargs.get("success_metric", ""),
            source_case_uris=kwargs.get("source_case_uris", []),
            supersedes_uri=kwargs.get("supersedes_uri"),
            superseded_by_uri=kwargs.get("superseded_by_uri"),
            tenant_id=tenant_id,
            owner_user_id=user_id,
            scope="private",
            share_status="private_only",
        )

        # Run sharing judgment if enabled
        ace_cfg = kwargs.get("_config") or get_effective_ace_config()
        share_status, share_score, share_reason = self._should_promote_to_shared(
            skill=skill,
            share_skills_to_team=ace_cfg.share_skills_to_team,
            skill_share_mode=ace_cfg.skill_share_mode,
            threshold=ace_cfg.skill_share_score_threshold,
        )
        skill.share_status = share_status
        skill.share_score = share_score
        skill.share_reason = share_reason

        # If promoted, update scope to shared (single-record promotion)
        if share_status == "promoted":
            skill.scope = "shared"

        prefix = self._resolve_prefix(tenant_id, user_id)
        await self._persist_skill(skill, prefix=prefix, trace=kwargs.get("trace", ""))
        return skill

    async def update_skill(
        self, skill_id: str, content: Optional[str] = None,
        tenant_id: str = "", user_id: str = "", **kwargs: Any,
    ) -> Skill:
        """Update an existing skill.

        Args:
            skill_id: Skill ID to update
            content: New content (if changed, re-embeds)
            tenant_id: Caller's tenant ID (for authorization)
            user_id: Caller's user ID (for authorization)
            **kwargs: Other fields to update

        Returns:
            Updated Skill

        Raises:
            SkillAuthorizationError: If enforcement is enabled and caller is not the owner.
        """
        records = await self._storage.get(self.COLLECTION, [skill_id])
        if not records:
            raise ValueError(f"Skill not found: {skill_id}")

        record = records[0]
        skill = Skill.from_dict(record)
        self._check_ownership(skill, user_id)

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

    async def tag_skill(
        self, skill_id: str, tag: str, increment: int = 1,
        user_id: str = "",
    ) -> None:
        """Increment a skill's feedback counter.

        Args:
            skill_id: Skill ID
            tag: One of "helpful", "harmful", "neutral"
            increment: Amount to increment
            user_id: Caller's user ID (for authorization)

        Raises:
            SkillAuthorizationError: If enforcement is enabled and caller is not the owner.
        """
        records = await self._storage.get(self.COLLECTION, [skill_id])
        if not records:
            raise ValueError(f"Skill not found: {skill_id}")
        self._check_ownership(Skill.from_dict(records[0]), user_id)

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

        # Update CortexFS L1 (overview)
        skill = Skill.from_dict(record)
        setattr(skill, tag, new_value)
        skill.updated_at = now
        prefix = self._resolve_prefix(skill.tenant_id, skill.owner_user_id)
        uri = f"{prefix}/{skill.section}/{skill.id}"
        overview = self._build_overview(skill)
        await self._fs.write_context(uri=uri, overview=overview)

    async def remove_skill(self, skill_id: str, user_id: str = "") -> None:
        """Remove a skill from the Skillbook.

        Args:
            skill_id: Skill ID to remove
            user_id: Caller's user ID (for authorization)

        Raises:
            SkillAuthorizationError: If enforcement is enabled and caller is not the owner.
        """
        records = await self._storage.get(self.COLLECTION, [skill_id])
        if records:
            self._check_ownership(Skill.from_dict(records[0]), user_id)
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
        self,
        query: str,
        limit: int = 5,
        section: Optional[str] = None,
        tenant_id: str = "",
        user_id: str = "",
    ) -> List[Skill]:
        """Vector search for skills with tenant/user scope isolation.

        Args:
            query: Search query text
            limit: Max results
            section: Optional section filter
            tenant_id: Tenant ID for scope filtering (dual-read: private + shared)
            user_id: User ID for scope filtering

        Returns:
            List of matching Skills
        """
        loop = asyncio.get_event_loop()
        embed_result = await loop.run_in_executor(
            None, self._embedder.embed, query
        )
        filter_cond = self._build_scope_filter(
            tenant_id=tenant_id, user_id=user_id, section=section,
        )

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

    async def get_by_section(
        self,
        section: str,
        tenant_id: str = "",
        user_id: str = "",
    ) -> List[Skill]:
        """Get all skills in a section with scope isolation.

        Args:
            section: Section name
            tenant_id: Tenant ID for scope filtering
            user_id: User ID for scope filtering

        Returns:
            List of Skills in the section
        """
        filter_cond = self._build_scope_filter(
            tenant_id=tenant_id, user_id=user_id, section=section,
        )
        results = await self._storage.filter(
            self.COLLECTION,
            filter=filter_cond,
            limit=10000,
        )
        return [Skill.from_dict(r) for r in results]

    # =========================================================================
    # Prompt & Stats
    # =========================================================================

    async def as_prompt(
        self,
        tenant_id: str = "",
        user_id: str = "",
    ) -> str:
        """Return all active skills as a tab-separated table for LLM prompt injection."""
        filter_cond = self._build_scope_filter(tenant_id=tenant_id, user_id=user_id)
        # Add active status filter
        filter_cond = {
            "op": "and",
            "conds": [
                filter_cond,
                {"op": "must", "field": "status", "conds": ["active"]},
            ],
        }
        all_skills = await self._storage.filter(
            self.COLLECTION,
            filter=filter_cond,
            limit=10000,
        )
        lines = ["ID\tSection\tContent\tHelpful\tHarmful"]
        for r in all_skills:
            skill = Skill.from_dict(r)
            lines.append(
                f"{skill.id}\t{skill.section}\t{skill.content}\t{skill.helpful}\t{skill.harmful}"
            )
        return "\n".join(lines)

    async def stats(
        self,
        tenant_id: str = "",
        user_id: str = "",
    ) -> Dict[str, Any]:
        """Return skill statistics with scope isolation."""
        base_filter = self._build_scope_filter(tenant_id=tenant_id, user_id=user_id)
        total = await self._storage.count(self.COLLECTION, filter=base_filter)

        by_section: Dict[str, int] = {}
        for section in ["strategies", "error_fixes", "patterns", "general"]:
            section_filter = {
                "op": "and",
                "conds": [
                    base_filter,
                    {"op": "must", "field": "type", "conds": [section]},
                ],
            }
            count = await self._storage.count(self.COLLECTION, filter=section_filter)
            if count > 0:
                by_section[section] = count

        return {"total": total, "by_section": by_section}

    # =========================================================================
    # Apply (dispatch UpdateOperation)
    # =========================================================================

    async def apply(
        self,
        op: UpdateOperation,
        trace: str = "",
        tenant_id: str = "",
        user_id: str = "",
    ) -> Optional[Skill]:
        """Apply an UpdateOperation, dispatching to the appropriate method.

        Args:
            op: The operation to apply
            trace: Optional trace/evidence text
            tenant_id: Tenant ID for scope isolation
            user_id: User ID for scope isolation

        Returns:
            The affected Skill (or None for REMOVE)
        """
        if op.type == "ADD":
            return await self.add_skill(
                section=op.section,
                content=op.content or "",
                tenant_id=tenant_id,
                user_id=user_id,
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
                tenant_id=tenant_id,
                user_id=user_id,
                justification=op.justification,
                evidence=op.evidence,
                trace=trace,
            )
        elif op.type == "TAG":
            if not op.skill_id:
                raise ValueError("TAG requires skill_id")
            for tag, increment in op.metadata.items():
                await self.tag_skill(op.skill_id, tag, increment, user_id=user_id)
            records = await self._storage.get(self.COLLECTION, [op.skill_id])
            return Skill.from_dict(records[0]) if records else None
        elif op.type == "REMOVE":
            if not op.skill_id:
                raise ValueError("REMOVE requires skill_id")
            await self.remove_skill(op.skill_id, user_id=user_id)
            return None
        else:
            raise ValueError(f"Unknown operation type: {op.type}")

    # =========================================================================
    # Section Summary
    # =========================================================================

    async def update_section_summary(
        self,
        section: str,
        tenant_id: str = "",
        user_id: str = "",
    ) -> None:
        """Update the section directory's L0/L1 summary."""
        skills = await self.get_by_section(section, tenant_id=tenant_id, user_id=user_id)
        prefix = self._resolve_prefix(tenant_id, user_id)
        section_uri = f"{prefix}/{section}"

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
    # Approval & Demotion
    # =========================================================================

    async def list_candidates(self, tenant_id: str = "") -> List[Skill]:
        """List skills with share_status=candidate for a tenant.

        Args:
            tenant_id: Tenant ID to filter candidates for.

        Returns:
            List of candidate Skills awaiting review.
        """
        filter_cond: Dict[str, Any] = {
            "op": "and",
            "conds": [
                {"op": "must", "field": "context_type", "conds": ["ace_skill"]},
                {"op": "must", "field": "share_status", "conds": ["candidate"]},
            ],
        }
        if tenant_id:
            filter_cond["conds"].append(
                {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
            )
        results = await self._storage.filter(self.COLLECTION, filter=filter_cond, limit=10000)
        return [Skill.from_dict(r) for r in results]

    async def review_skill(
        self,
        skill_id: str,
        decision: str,
        reviewer_user_id: str = "",
        tenant_id: str = "",
    ) -> Skill:
        """Approve or reject a candidate skill.

        Args:
            skill_id: Skill to review.
            decision: "approve" or "reject".
            reviewer_user_id: User performing the review.
            tenant_id: Tenant context.

        Returns:
            Updated Skill.

        Raises:
            ValueError: If skill not found or not a candidate.
        """
        records = await self._storage.get(self.COLLECTION, [skill_id])
        if not records:
            raise ValueError(f"Skill not found: {skill_id}")

        skill = Skill.from_dict(records[0])
        if skill.share_status != "candidate":
            raise ValueError(
                f"Skill '{skill_id}' is not a candidate (status: {skill.share_status})"
            )

        now = get_current_timestamp()
        if decision == "approve":
            skill.scope = "shared"
            skill.share_status = "promoted"
            skill.share_reason = f"approved_by_{reviewer_user_id}" if reviewer_user_id else "approved"
        elif decision == "reject":
            skill.share_status = "private_only"
            skill.share_reason = f"rejected_by_{reviewer_user_id}" if reviewer_user_id else "rejected"
        else:
            raise ValueError(f"Invalid decision: {decision}. Must be 'approve' or 'reject'.")

        skill.updated_at = now
        await self._storage.update(
            self.COLLECTION,
            skill_id,
            {
                "scope": skill.scope,
                "share_status": skill.share_status,
                "share_reason": skill.share_reason,
                "updated_at": now,
            },
        )
        return skill

    async def demote_skill(
        self,
        skill_id: str,
        reason: str = "",
        tenant_id: str = "",
        user_id: str = "",
    ) -> Skill:
        """Demote a shared/promoted skill back to private.

        Args:
            skill_id: Skill to demote.
            reason: Reason for demotion.
            tenant_id: Tenant context.
            user_id: Caller's user ID (for authorization).

        Returns:
            Updated Skill.

        Raises:
            ValueError: If skill not found or not shared/promoted.
            SkillAuthorizationError: If enforcement is enabled and caller is not the owner.
        """
        records = await self._storage.get(self.COLLECTION, [skill_id])
        if not records:
            raise ValueError(f"Skill not found: {skill_id}")

        skill = Skill.from_dict(records[0])
        if skill.scope != "shared" and skill.share_status != "promoted":
            raise ValueError(
                f"Skill '{skill_id}' is not shared/promoted (scope: {skill.scope}, status: {skill.share_status})"
            )

        self._check_ownership(skill, user_id)

        now = get_current_timestamp()
        skill.scope = "private"
        skill.share_status = "demoted"
        skill.share_reason = reason or "demoted"
        skill.updated_at = now

        await self._storage.update(
            self.COLLECTION,
            skill_id,
            {
                "scope": skill.scope,
                "share_status": skill.share_status,
                "share_reason": skill.share_reason,
                "updated_at": now,
            },
        )
        return skill

    # =========================================================================
    # Migration
    # =========================================================================

    async def migrate_legacy_skills(
        self,
        tenant_id: str = "default",
        owner_user_id: str = "default",
    ) -> Dict[str, Any]:
        """Backfill existing skills that lack scope fields.

        Scans all skills in the collection. For any skill where scope is empty
        or missing, sets scope="legacy", tenant_id, and owner_user_id.

        Args:
            tenant_id: Tenant to assign to legacy skills.
            owner_user_id: User to assign as owner of legacy skills.

        Returns:
            Dict with migration counts.
        """
        # Find all skills without scope set (scope="" or missing)
        all_records = await self._storage.filter(
            self.COLLECTION,
            filter={"op": "must", "field": "context_type", "conds": ["ace_skill"]},
            limit=100_000,
        )

        migrated = 0
        skipped = 0
        now = get_current_timestamp()

        for record in all_records:
            scope = record.get("scope", "")
            if scope and scope != "":
                skipped += 1
                continue

            await self._storage.update(
                self.COLLECTION,
                record["id"],
                {
                    "scope": "legacy",
                    "share_status": "private_only",
                    "share_score": 0.0,
                    "share_reason": "legacy_migration",
                    "tenant_id": record.get("tenant_id") or tenant_id,
                    "owner_user_id": record.get("owner_user_id") or owner_user_id,
                    "updated_at": now,
                },
            )
            migrated += 1

        return {
            "migrated": migrated,
            "skipped": skipped,
            "total": len(all_records),
        }

    # =========================================================================
    # Internal
    # =========================================================================

    async def _persist_skill(self, skill: Skill, prefix: str = "", trace: str = "") -> None:
        """Dual-write: Qdrant + CortexFS."""
        prefix = prefix or self._prefix
        uri = f"{prefix}/{skill.section}/{skill.id}"

        # CortexFS L0/L1/L2
        overview = self._build_overview(skill)
        await self._fs.write_context(
            uri=uri,
            abstract=skill.content,
            overview=overview,
            content=trace,
            is_leaf=True,
        )

        # Qdrant vector write
        loop = asyncio.get_event_loop()
        embed_result = await loop.run_in_executor(
            None, self._embedder.embed, skill.content
        )
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
                # Evolution fields
                "confidence_score": skill.confidence_score,
                "version": skill.version,
                "trigger_conditions": skill.trigger_conditions,
                "action_template": skill.action_template,
                "success_metric": skill.success_metric,
                "source_case_uris": skill.source_case_uris,
                "supersedes_uri": skill.supersedes_uri or "",
                "superseded_by_uri": skill.superseded_by_uri or "",
                # Multi-tenant scope fields
                "tenant_id": skill.tenant_id,
                "owner_user_id": skill.owner_user_id,
                "scope": skill.scope,
                "share_status": skill.share_status,
                "share_score": skill.share_score,
                "share_reason": skill.share_reason,
            },
        )

    def _check_ownership(self, skill: Skill, user_id: str) -> None:
        """Check if the caller is authorized to modify a skill.

        Raises SkillAuthorizationError when:
        - ace_scope_enforcement_enabled is True (per-request), AND
        - user_id is provided, AND
        - skill has an owner_user_id that differs from user_id
        """
        ace_cfg = get_effective_ace_config()
        if not ace_cfg.ace_scope_enforcement_enabled:
            return
        if not user_id or not skill.owner_user_id:
            return
        if skill.owner_user_id != user_id:
            raise SkillAuthorizationError(
                f"User '{user_id}' cannot modify skill '{skill.id}' "
                f"owned by '{skill.owner_user_id}'"
            )

    def _resolve_prefix(self, tenant_id: str = "", user_id: str = "") -> str:
        """Resolve the URI prefix for shared skills storage."""
        if tenant_id:
            return f"opencortex://{tenant_id}/shared/skills"
        return self._prefix or "opencortex://default/shared/skills"

    def _build_scope_filter(
        self,
        tenant_id: str = "",
        user_id: str = "",
        section: Optional[str] = None,
        exclude_status: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Build a scope-aware filter for dual-read (private + shared + legacy).

        When tenant_id/user_id are provided, applies tenant isolation:
        - tenant_id must match
        - scope=shared OR scope=legacy OR (scope=private AND owner_user_id matches)

        Legacy skills (pre-migration, scope="legacy") are visible to all users
        in the tenant, same as shared skills.

        When not provided, falls back to basic context_type filter.

        Args:
            exclude_status: List of status values to exclude (default: ["deprecated"]).
        """
        if exclude_status is None:
            exclude_status = ["deprecated"]

        base_conds: List[Dict[str, Any]] = [
            {"op": "must", "field": "context_type", "conds": ["ace_skill"]},
        ]

        if exclude_status:
            base_conds.append(
                {"op": "must_not", "field": "status", "conds": exclude_status},
            )

        if tenant_id:
            base_conds.append(
                {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
            )
            if user_id:
                # Dual-read: private for this user OR shared/legacy for tenant
                base_conds.append({
                    "op": "or",
                    "conds": [
                        {"op": "must", "field": "scope", "conds": ["shared"]},
                        {"op": "must", "field": "scope", "conds": ["legacy"]},
                        {
                            "op": "and",
                            "conds": [
                                {"op": "must", "field": "scope", "conds": ["private"]},
                                {"op": "must", "field": "owner_user_id", "conds": [user_id]},
                            ],
                        },
                    ],
                })

        if section:
            base_conds.append(
                {"op": "must", "field": "type", "conds": [section]},
            )

        if len(base_conds) == 1:
            return base_conds[0]
        return {"op": "and", "conds": base_conds}

    # =========================================================================
    # Sharing Engine
    # =========================================================================

    # Hard-block regex patterns for sensitive content
    _SECRET_RE = re.compile(
        r'(?i)(api[_-]?key|secret|token|password|credential)\s*[:=]'
    )
    _PII_EMAIL_RE = re.compile(r'\S+@\S+\.\S+')
    _PII_PHONE_RE = re.compile(r'1[3-9]\d{9}')
    _PII_IDCARD_RE = re.compile(r'\d{17}[\dXx]')
    _ENV_PATH_RE = re.compile(r'/Users/|/home/|C:\\')
    _ENV_INTERNAL_RE = re.compile(r'\.\w+\.internal')

    @staticmethod
    def _hard_block_check(content: str) -> Tuple[bool, str]:
        """Check content for hard-block patterns that prevent sharing.

        Returns:
            (blocked, reason) — blocked=True if any pattern matches.
        """
        if Skillbook._SECRET_RE.search(content):
            return True, "contains_secret"
        if Skillbook._PII_EMAIL_RE.search(content):
            return True, "contains_pii_email"
        # Check ID card before phone (18-digit ID card contains phone-like substrings)
        if Skillbook._PII_IDCARD_RE.search(content):
            return True, "contains_pii_idcard"
        if Skillbook._PII_PHONE_RE.search(content):
            return True, "contains_pii_phone"
        if Skillbook._ENV_PATH_RE.search(content):
            return True, "contains_env_path"
        if Skillbook._ENV_INTERNAL_RE.search(content):
            return True, "contains_internal_host"
        return False, ""

    @staticmethod
    def _compute_share_score(skill: Skill) -> float:
        """Compute a deterministic share score (0.0-1.0).

        Three dimensions:
        - Generalizability (0.4): no user/env-specific references
        - Reusability (0.3): positive feedback signals
        - Executability (0.3): has action verbs and conditions
        """
        score = 0.0
        content = skill.content or ""

        # Generalizability (0.4)
        env_refs = len(re.findall(
            r'(?i)(localhost|127\.0\.0\.1|/Users/|~/)', content
        ))
        score += 0.4 * max(0, 1 - env_refs * 0.2)

        # Reusability (0.3)
        helpful = skill.helpful or 0
        score += 0.3 * min(1.0, helpful / 3.0)

        # Executability (0.3)
        has_actions = bool(re.search(
            r'(?i)(run|execute|create|update|delete|check|verify)', content
        ))
        has_conditions = bool(re.search(
            r'(?i)(if|when|before|after|unless)', content
        ))
        score += 0.3 * (0.5 * has_actions + 0.5 * has_conditions)

        return round(score, 3)

    @staticmethod
    def _should_promote_to_shared(
        skill: Skill,
        share_skills_to_team: bool,
        skill_share_mode: str,
        threshold: float,
    ) -> Tuple[str, float, str]:
        """Decide share_status based on config and content analysis.

        Args:
            skill: The skill to evaluate
            share_skills_to_team: Whether sharing is enabled
            skill_share_mode: "manual" | "auto_safe" | "auto_aggressive"
            threshold: Minimum share_score for auto modes

        Returns:
            (share_status, share_score, share_reason)
        """
        if not share_skills_to_team:
            return "private_only", 0.0, ""

        # Hard block check
        blocked, reason = Skillbook._hard_block_check(skill.content or "")
        if blocked:
            return "blocked", 0.0, reason

        # Soft score
        share_score = Skillbook._compute_share_score(skill)

        if skill_share_mode == "manual":
            return "candidate", share_score, "manual_candidate"

        if skill_share_mode == "auto_safe":
            if share_score >= threshold and (skill.helpful or 0) >= 2:
                return "promoted", share_score, "auto_safe_promoted"
            return "candidate", share_score, "auto_safe_below_threshold"

        if skill_share_mode == "auto_aggressive":
            if share_score >= threshold:
                return "promoted", share_score, "auto_aggressive_promoted"
            return "candidate", share_score, "auto_aggressive_below_threshold"

        return "private_only", share_score, f"unknown_mode_{skill_share_mode}"

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
