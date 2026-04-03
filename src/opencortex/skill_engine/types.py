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
    # ReAct feedback loop fields
    rating: "SkillRating" = field(default_factory=lambda: SkillRating())
    tdd_passed: bool = False
    quality_score: int = 0
    reward_score: float = 0.0

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
            "rating": self.rating.to_dict(),
            "tdd_passed": self.tdd_passed,
            "quality_score": self.quality_score,
            "reward_score": self.reward_score,
        }


@dataclass
class EvolutionSuggestion:
    evolution_type: SkillOrigin
    target_skill_ids: List[str] = field(default_factory=list)
    category: SkillCategory = SkillCategory.WORKFLOW
    direction: str = ""
    confidence: float = 0.0
    source_memory_ids: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ReAct Feedback Loop Types
# ---------------------------------------------------------------------------

@dataclass
class SkillEvent:
    """Durable skill usage event — stored in independent skill_events collection."""
    event_id: str
    session_id: str
    turn_id: str
    skill_id: str
    skill_uri: str
    tenant_id: str
    user_id: str
    event_type: str      # "selected" | "cited"
    outcome: str = ""    # "" | "success" | "failure"
    timestamp: str = ""
    evaluated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "skill_id": self.skill_id,
            "skill_uri": self.skill_uri,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "event_type": self.event_type,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
            "evaluated": self.evaluated,
        }


@dataclass
class QualityCheck:
    name: str
    severity: str    # "ERROR" | "WARNING" | "INFO"
    passed: bool
    message: str
    fix_suggestion: str = ""


@dataclass
class QualityReport:
    score: int       # 0-100
    checks: List[QualityCheck] = field(default_factory=list)
    errors: int = 0
    warnings: int = 0


@dataclass
class TDDResult:
    passed: bool
    scenarios_total: int = 0
    scenarios_improved: int = 0
    scenarios_same: int = 0
    scenarios_worse: int = 0
    sections_cited: List[str] = field(default_factory=list)
    rationalizations: List[str] = field(default_factory=list)
    quality_delta: float = 0.0
    llm_calls_used: int = 0


@dataclass
class SkillRating:
    practicality: float = 0.0
    clarity: float = 0.0
    automation: float = 0.0
    quality: float = 0.0
    impact: float = 0.0
    overall: float = 0.0
    rank: str = "C"

    def compute_overall(self) -> None:
        self.overall = (self.practicality + self.clarity + self.automation
                        + self.quality + self.impact) / 5
        if self.overall >= 9.0: self.rank = "S"
        elif self.overall >= 7.0: self.rank = "A"
        elif self.overall >= 5.0: self.rank = "B"
        else: self.rank = "C"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "practicality": self.practicality, "clarity": self.clarity,
            "automation": self.automation, "quality": self.quality,
            "impact": self.impact, "overall": self.overall, "rank": self.rank,
        }


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


def extract_skill_id_from_uri(uri: str) -> str:
    """Extract skill_id from a skill URI."""
    return uri.split("/")[-1] if uri else ""


def make_source_fingerprint(memory_ids: List[str]) -> str:
    """Deterministic fingerprint from source memory IDs for extraction idempotency."""
    key = "|".join(sorted(memory_ids))
    return hashlib.sha256(key.encode()).hexdigest()[:16]
