# SPDX-License-Identifier: Apache-2.0
"""ACE data types for Skillbook and Engine."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


@dataclass
class Skill:
    """A single learned skill entry in the Skillbook."""

    id: str  # uuid4
    section: str  # "strategies" | "error_fixes" | "patterns" | "general"
    content: str  # L0: imperative sentence
    justification: Optional[str] = None
    evidence: Optional[str] = None
    helpful: int = 0
    harmful: int = 0
    neutral: int = 0
    status: str = "active"  # "active" | "invalid" | "protected"
    created_at: str = ""
    updated_at: str = ""
    # Multi-tenant scope fields
    tenant_id: str = ""
    owner_user_id: str = ""
    scope: str = "private"  # "private" | "shared" | "legacy"
    share_status: str = "private_only"  # "private_only"|"candidate"|"promoted"|"demoted"|"blocked"
    share_score: float = 0.0
    share_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "section": self.section,
            "content": self.content,
            "justification": self.justification,
            "evidence": self.evidence,
            "helpful": self.helpful,
            "harmful": self.harmful,
            "neutral": self.neutral,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tenant_id": self.tenant_id,
            "owner_user_id": self.owner_user_id,
            "scope": self.scope,
            "share_status": self.share_status,
            "share_score": self.share_score,
            "share_reason": self.share_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Skill":
        return cls(
            id=data.get("id", ""),
            section=data.get("section", data.get("type", "")),
            content=data.get("content", data.get("abstract", "")),
            justification=data.get("justification"),
            evidence=data.get("evidence"),
            helpful=data.get("helpful", 0),
            harmful=data.get("harmful", 0),
            neutral=data.get("neutral", 0),
            status=data.get("status", "active"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            tenant_id=data.get("tenant_id", ""),
            owner_user_id=data.get("owner_user_id", ""),
            scope=data.get("scope", "private"),
            share_status=data.get("share_status", "private_only"),
            share_score=data.get("share_score", 0.0),
            share_reason=data.get("share_reason", ""),
        )


@dataclass
class Learning:
    """A single learning extracted by the Reflector."""

    learning: str  # Imperative sentence, <20 words
    evidence: str  # Concrete evidence from the execution
    justification: str  # Why this is a general pattern


@dataclass
class SkillTag:
    """A tag decision for an existing skill."""

    skill_id: str
    tag: Literal["helpful", "harmful", "neutral"]


@dataclass
class ReflectorOutput:
    """Structured output from the Reflector's analysis."""

    reasoning: str  # Full analysis
    error_identification: str  # "none" or specific error
    root_cause_analysis: str  # Root cause
    key_insight: str  # Most important insight
    extracted_learnings: List[Learning] = field(default_factory=list)
    skill_tags: List[SkillTag] = field(default_factory=list)


@dataclass
class UpdateOperation:
    """Describes a Skillbook mutation."""

    type: Literal["ADD", "UPDATE", "TAG", "REMOVE"]
    section: str
    content: Optional[str] = None
    skill_id: Optional[str] = None
    metadata: Dict[str, int] = field(default_factory=dict)
    justification: Optional[str] = None
    evidence: Optional[str] = None


@dataclass
class LearnResult:
    """Return type for learn(), satisfying orchestrator's result.success/.best_action/.message access."""

    success: bool = True
    best_action: str = ""
    message: str = ""
    operations_applied: int = 0
    reflection_key_insight: str = ""


@dataclass
class HooksStats:
    """Return type for stats(), satisfying orchestrator's attribute access."""

    q_learning_patterns: int = 0
    vector_memories: int = 0
    learning_trajectories: int = 0
    error_patterns: int = 0
