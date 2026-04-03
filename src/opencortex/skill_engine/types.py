"""
Skill Engine data types — mirrors OpenSpace skill_engine/types.py.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class SkillOrigin(str, Enum):
    IMPORTED = "imported"
    CAPTURED = "captured"
    DERIVED = "derived"
    FIXED = "fixed"


class SkillCategory(str, Enum):
    WORKFLOW = "workflow"
    TOOL_GUIDE = "tool_guide"
    PATTERN = "pattern"


class SkillVisibility(str, Enum):
    PRIVATE = "private"
    SHARED = "shared"


class SkillStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


@dataclass
class SkillLineage:
    origin: SkillOrigin = SkillOrigin.CAPTURED
    generation: int = 0
    parent_skill_ids: List[str] = field(default_factory=list)
    source_memory_ids: List[str] = field(default_factory=list)
    change_summary: str = ""
    content_diff: str = ""
    content_snapshot: Dict[str, str] = field(default_factory=dict)
    created_by: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "origin": self.origin.value,
            "generation": self.generation,
            "parent_skill_ids": self.parent_skill_ids,
            "source_memory_ids": self.source_memory_ids,
            "change_summary": self.change_summary,
            "content_diff": self.content_diff,
            "content_snapshot": self.content_snapshot,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }


@dataclass
class SkillRecord:
    skill_id: str
    name: str
    description: str
    content: str
    category: SkillCategory
    status: SkillStatus = SkillStatus.CANDIDATE
    visibility: SkillVisibility = SkillVisibility.PRIVATE
    lineage: SkillLineage = field(default_factory=SkillLineage)
    tags: List[str] = field(default_factory=list)
    tenant_id: str = ""
    user_id: str = ""
    project_id: str = "public"
    uri: str = ""
    total_selections: int = 0
    total_applied: int = 0
    total_completions: int = 0
    total_fallbacks: int = 0
    abstract: str = ""
    overview: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    source_fingerprint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "content": self.content,
            "category": self.category.value,
            "status": self.status.value,
            "visibility": self.visibility.value,
            "lineage": self.lineage.to_dict(),
            "tags": self.tags,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "uri": self.uri,
            "total_selections": self.total_selections,
            "total_applied": self.total_applied,
            "total_completions": self.total_completions,
            "total_fallbacks": self.total_fallbacks,
            "abstract": self.abstract,
            "overview": self.overview,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_fingerprint": self.source_fingerprint,
        }


@dataclass
class EvolutionSuggestion:
    evolution_type: SkillOrigin
    target_skill_ids: List[str] = field(default_factory=list)
    category: SkillCategory = SkillCategory.WORKFLOW
    direction: str = ""
    confidence: float = 0.0
    source_memory_ids: List[str] = field(default_factory=list)


def make_skill_uri(
    tenant_id: str, user_id: str, skill_id: str,
    visibility: str = "private", category: str = "general",
) -> str:
    """Generate a stable skill URI compatible with CortexURI routing.

    Aligns with the existing URI scheme:
      Shared: opencortex://{tid}/shared/skills/{category}/{skill_id}
      Private: opencortex://{tid}/{uid}/skills/{category}/{skill_id}
    """
    if visibility == "shared":
        return f"opencortex://{tenant_id}/shared/skills/{category}/{skill_id}"
    return f"opencortex://{tenant_id}/{user_id}/skills/{category}/{skill_id}"


def make_source_fingerprint(memory_ids: List[str]) -> str:
    """Deterministic fingerprint from source memory IDs for extraction idempotency."""
    key = "|".join(sorted(memory_ids))
    return hashlib.sha256(key.encode()).hexdigest()[:16]
