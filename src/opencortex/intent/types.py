# SPDX-License-Identifier: Apache-2.0
"""Phase-native contracts for the memory intent pipeline."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field, model_validator

from opencortex.memory import MemoryKind
from opencortex.memory.domain import MemoryDomainModel


class MemoryCoarseClass(str, Enum):
    """Planner-internal coarse priors."""

    LOOKUP = "lookup"
    PROFILE = "profile"
    EXPLORE = "explore"
    RELATIONAL = "relational"


class QueryAnchorKind(str, Enum):
    """Supported planner anchor kinds."""

    ENTITY = "entity"
    TIME = "time"
    PROFILE = "profile"
    TOPIC = "topic"


class QueryRewriteMode(str, Enum):
    """Bounded planner rewrite modes."""

    NONE = "none"
    LIGHT = "light"
    DECOMPOSE = "decompose"


class RetrievalDepth(str, Enum):
    """Planner-requested evidence depth."""

    L0 = "l0"
    L1 = "l1"
    L2 = "l2"


class ScopeLevel(str, Enum):
    """Probe-derived scope boundary for retrieval."""

    CONTAINER_SCOPED = "container_scoped"
    SESSION_ONLY = "session_only"
    DOCUMENT_ONLY = "document_only"
    GLOBAL = "global"


class QueryAnchor(MemoryDomainModel):
    """Structured anchor extracted by planner."""

    kind: QueryAnchorKind
    value: str
    confidence: Optional[float] = None


class MemoryQueryPlan(MemoryDomainModel):
    """Planner-owned query posture."""

    anchors: List[QueryAnchor] = Field(default_factory=list)
    rewrite_mode: QueryRewriteMode = QueryRewriteMode.NONE


class MemorySearchProfile(MemoryDomainModel):
    """Planner-owned retrieval budgets."""

    recall_budget: float
    association_budget: float
    rerank: bool


class StartingPoint(MemoryDomainModel):
    """Session or document root used as a retrieval starting point."""

    uri: str
    session_id: Optional[str] = None
    source_doc_id: Optional[str] = None
    parent_uri: Optional[str] = None
    entities: List[str] = Field(default_factory=list)
    time_refs: List[str] = Field(default_factory=list)
    score: float = 0.0


class SearchCandidate(MemoryDomainModel):
    """Cheap L0 candidate surface emitted by the bootstrap probe."""

    uri: str
    memory_kind: MemoryKind
    context_type: str = ""
    category: str = ""
    score: Optional[float] = None
    abstract: str = ""
    overview: Optional[str] = None
    anchors: List[str] = Field(default_factory=list)


class SearchEvidence(MemoryDomainModel):
    """Probe evidence signals consumed by the planner."""

    top_score: Optional[float] = None
    score_gap: Optional[float] = None
    object_top_score: Optional[float] = None
    anchor_top_score: Optional[float] = None
    candidate_count: int = 0
    object_candidate_count: int = 0
    anchor_candidate_count: int = 0
    anchor_hit_count: int = 0


class MemoryProbeTrace(MemoryDomainModel):
    """Machine-readable trace for Phase 1 bootstrap probing."""

    backend: str = "local_probe"
    model: Optional[str] = None
    top_k: int = 0
    latency_ms: Optional[float] = None
    object_latency_ms: Optional[float] = None
    anchor_latency_ms: Optional[float] = None
    object_candidates: int = 0
    anchor_candidates: int = 0
    starting_points: int = 0
    degraded: bool = False
    degrade_reason: Optional[str] = None


class ExecutionTrace(MemoryDomainModel):
    """Machine-readable execution trace."""

    probe: Dict[str, Any] = Field(default_factory=dict)
    planner: Dict[str, Any] = Field(default_factory=dict)
    effective: Dict[str, Any] = Field(default_factory=dict)
    hydration: List[Dict[str, Any]] = Field(default_factory=list)
    fallback: List[Dict[str, Any]] = Field(default_factory=list)
    latency_ms: Dict[str, Any] = Field(default_factory=dict)


class MemoryRuntimeDegrade(MemoryDomainModel):
    """Execution-level degrade report."""

    applied: bool = False
    reasons: List[str] = Field(default_factory=list)
    actions: List[str] = Field(default_factory=list)


class SearchResult(MemoryDomainModel):
    """Phase 1 bootstrap probe output."""

    should_recall: bool
    anchor_hits: List[str] = Field(default_factory=list)
    candidate_entries: List[SearchCandidate] = Field(default_factory=list)
    starting_points: List[StartingPoint] = Field(default_factory=list)
    query_entities: List[str] = Field(default_factory=list)
    starting_point_anchors: List[str] = Field(default_factory=list)
    scope_level: ScopeLevel = ScopeLevel.GLOBAL
    evidence: SearchEvidence = Field(default_factory=SearchEvidence)
    trace: MemoryProbeTrace = Field(default_factory=MemoryProbeTrace)

    @model_validator(mode="after")
    def _normalize_fields(self) -> "SearchResult":
        if not self.should_recall:
            self.anchor_hits = []
            self.candidate_entries = []
            self.starting_points = []
            self.query_entities = []
            self.starting_point_anchors = []
            self.scope_level = ScopeLevel.GLOBAL
            self.evidence = SearchEvidence()
        return self


class RetrievalPlan(MemoryDomainModel):
    """Phase 2 object-aware retrieval plan."""

    target_memory_kinds: List[MemoryKind] = Field(default_factory=list)
    query_plan: MemoryQueryPlan = Field(default_factory=MemoryQueryPlan)
    search_profile: MemorySearchProfile
    retrieval_depth: RetrievalDepth
    scope_level: ScopeLevel = ScopeLevel.GLOBAL
    session_scope: Optional[str] = None
    confidence: Optional[float] = None
    decision: str = ""


class ExecutionResult(MemoryDomainModel):
    """Phase 3 execution output envelope."""

    items: List[Dict[str, Any]] = Field(default_factory=list)
    trace: ExecutionTrace = Field(default_factory=ExecutionTrace)
    degrade: MemoryRuntimeDegrade = Field(default_factory=MemoryRuntimeDegrade)
