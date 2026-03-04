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
    status: str = "active"  # "active" | "invalid" | "protected" | "observation" | "deprecated"
    created_at: str = ""
    updated_at: str = ""
    # Evolution fields
    confidence_score: float = 0.5
    version: int = 1
    trigger_conditions: List[str] = field(default_factory=list)
    action_template: List[str] = field(default_factory=list)
    success_metric: str = ""
    source_case_uris: List[str] = field(default_factory=list)
    supersedes_uri: Optional[str] = None
    superseded_by_uri: Optional[str] = None
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
            "confidence_score": self.confidence_score,
            "version": self.version,
            "trigger_conditions": self.trigger_conditions,
            "action_template": self.action_template,
            "success_metric": self.success_metric,
            "source_case_uris": self.source_case_uris,
            "supersedes_uri": self.supersedes_uri or "",
            "superseded_by_uri": self.superseded_by_uri or "",
            "tenant_id": self.tenant_id,
            "owner_user_id": self.owner_user_id,
            "scope": self.scope,
            "share_status": self.share_status,
            "share_score": self.share_score,
            "share_reason": self.share_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Skill":
        # Read-time compat: fill missing evolution fields
        data.setdefault("confidence_score", 0.5)
        data.setdefault("version", 1)
        data.setdefault("trigger_conditions", [])
        data.setdefault("action_template", [])
        data.setdefault("success_metric", "")
        data.setdefault("source_case_uris", [])
        data.setdefault("supersedes_uri", "")
        data.setdefault("superseded_by_uri", "")
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
            confidence_score=data.get("confidence_score", 0.5),
            version=data.get("version", 1),
            trigger_conditions=data.get("trigger_conditions", []),
            action_template=data.get("action_template", []),
            success_metric=data.get("success_metric", ""),
            source_case_uris=data.get("source_case_uris", []),
            supersedes_uri=data.get("supersedes_uri") or None,
            superseded_by_uri=data.get("superseded_by_uri") or None,
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
