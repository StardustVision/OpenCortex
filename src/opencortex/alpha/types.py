"""
Cortex Alpha data types.

Trace schema (Section 7.1) and Knowledge types (Section 7.2)
from docs/cortex-alpha-design.md.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TurnStatus(str, Enum):
    COMPLETE = "complete"
    INTERRUPTED = "interrupted"
    TIMEOUT = "timeout"


class TraceOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class KnowledgeType(str, Enum):
    BELIEF = "belief"
    SOP = "sop"
    NEGATIVE_RULE = "negative_rule"
    ROOT_CAUSE = "root_cause"


class KnowledgeStatus(str, Enum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class KnowledgeScope(str, Enum):
    USER = "user"
    TENANT = "tenant"
    GLOBAL = "global"


# ---------------------------------------------------------------------------
# Trace types (Design doc §7.1)
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    turn_id: str
    prompt_text: Optional[str] = None
    thought_text: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    final_text: Optional[str] = None
    turn_status: TurnStatus = TurnStatus.COMPLETE
    latency_ms: Optional[int] = None
    token_count: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "turn_id": self.turn_id,
            "turn_status": self.turn_status.value,
            "tool_calls": self.tool_calls,
        }
        for k in ("prompt_text", "thought_text", "final_text",
                   "latency_ms", "token_count"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d


@dataclass
class Trace:
    # Required
    trace_id: str
    session_id: str
    tenant_id: str
    user_id: str
    source: str  # "claude_code" / "codex" / "agno" / ...
    turns: List[Turn] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # Optional
    source_version: Optional[str] = None
    task_type: Optional[str] = None
    outcome: Optional[TraceOutcome] = None
    error_code: Optional[str] = None
    cost_meta: Optional[Dict[str, Any]] = None
    training_ready: bool = False
    # CortexFS layers (populated by Trace Splitter)
    abstract: Optional[str] = None   # L0
    overview: Optional[str] = None   # L1

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "source": self.source,
            "turns": [t.to_dict() for t in self.turns],
            "created_at": self.created_at,
            "training_ready": self.training_ready,
        }
        for k in ("source_version", "task_type", "error_code",
                   "cost_meta", "abstract", "overview"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.outcome is not None:
            d["outcome"] = self.outcome.value
        return d


# Searchable statuses (Design doc §8.4)
SEARCHABLE_STATUSES = {KnowledgeStatus.ACTIVE}


# ---------------------------------------------------------------------------
# Knowledge types (Design doc §7.2) — unified dataclass for all 4 types
# ---------------------------------------------------------------------------

@dataclass
class Knowledge:
    # Required for all types
    knowledge_id: str
    knowledge_type: KnowledgeType
    tenant_id: str
    user_id: str
    scope: KnowledgeScope
    status: KnowledgeStatus = KnowledgeStatus.CANDIDATE
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # CortexFS layers
    abstract: Optional[str] = None  # L0
    overview: Optional[str] = None  # L1
    # Shared optional fields
    statement: Optional[str] = None        # Belief / Negative Rule
    objective: Optional[str] = None        # Belief / SOP
    preconditions: Optional[str] = None    # Belief / SOP
    confidence: Optional[float] = None
    evidence_trace_ids: List[str] = field(default_factory=list)
    source_trace_ids: List[str] = field(default_factory=list)
    counter_examples: List[str] = field(default_factory=list)
    # SOP-specific
    action_steps: Optional[List[str]] = None
    trigger_keywords: Optional[List[str]] = None
    anti_patterns: Optional[List[str]] = None
    success_criteria: Optional[str] = None
    failure_signals: Optional[str] = None
    # Negative Rule-specific
    context: Optional[str] = None
    severity: Optional[str] = None
    # Root Cause-specific
    error_pattern: Optional[str] = None
    cause: Optional[str] = None
    fix_suggestion: Optional[str] = None
    frequency: Optional[int] = None
    # Future
    training_ready: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "knowledge_id": self.knowledge_id,
            "knowledge_type": self.knowledge_type.value,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "scope": self.scope.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "training_ready": self.training_ready,
        }
        # Include non-None optional fields
        for k in ("statement", "objective", "preconditions", "confidence",
                   "action_steps", "trigger_keywords", "anti_patterns",
                   "success_criteria", "failure_signals", "context",
                   "severity", "error_pattern", "cause", "fix_suggestion",
                   "frequency", "abstract", "overview"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        # Include non-empty lists
        for k in ("evidence_trace_ids", "source_trace_ids", "counter_examples"):
            v = getattr(self, k)
            if v:
                d[k] = v
        return d
