# SPDX-License-Identifier: Apache-2.0
"""Phase-native contracts for the memory intent pipeline."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

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
    """Planner rewrite mode (single-mode contract)."""

    NONE = "none"


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


class ProbeScopeSource(str, Enum):
    """Source that selected the active probe bucket."""

    TARGET_URI = "target_uri"
    SESSION_ID = "session_id"
    SOURCE_DOC_ID = "source_doc_id"
    CONTEXT_TYPE = "context_type"
    GLOBAL_ROOT = "global_root"


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
    matched_anchors: List[str] = Field(default_factory=list)


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
    selected_bucket_source: Optional[ProbeScopeSource] = None
    scope_authoritative: bool = False
    selected_root_uris: List[str] = Field(default_factory=list)
    scoped_miss: bool = False
    degraded: bool = False
    degrade_reason: Optional[str] = None


class ProbeScopeInput(MemoryDomainModel):
    """Structured caller scope that preserves bucket precedence semantics."""

    source: ProbeScopeSource = ProbeScopeSource.GLOBAL_ROOT
    authoritative: bool = False
    target_uri: Optional[str] = None
    session_id: Optional[str] = None
    source_doc_id: Optional[str] = None
    context_type: Optional[str] = None


class ExecutionTrace(MemoryDomainModel):
    """Machine-readable execution trace."""

    probe: Dict[str, Any] = Field(default_factory=dict)
    planner: Dict[str, Any] = Field(default_factory=dict)
    effective: Dict[str, Any] = Field(default_factory=dict)
    hydration: List[Dict[str, Any]] = Field(default_factory=list)
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
    scope_source: ProbeScopeSource = ProbeScopeSource.GLOBAL_ROOT
    scope_authoritative: bool = False
    selected_root_uris: List[str] = Field(default_factory=list)
    scoped_miss: bool = False
    evidence: SearchEvidence = Field(default_factory=SearchEvidence)
    trace: MemoryProbeTrace = Field(default_factory=MemoryProbeTrace)


def probe_confidence(probe_result: Optional[SearchResult]) -> float:
    """Project bounded probe evidence into a cheap normalized confidence."""
    if probe_result is None:
        return 0.0
    evidence = probe_result.evidence
    top_score = evidence.top_score or 0.0
    score_gap = evidence.score_gap or 0.0
    object_top = evidence.object_top_score or 0.0
    anchor_top = evidence.anchor_top_score or 0.0

    confidence = top_score + min(score_gap, 0.18)
    if object_top > 0.0 and anchor_top > 0.0:
        confidence += 0.05 if abs(object_top - anchor_top) <= 0.15 else 0.02
    if evidence.anchor_hit_count > 0:
        confidence += min(0.08, evidence.anchor_hit_count * 0.02)
    if evidence.candidate_count == 0:
        confidence *= 0.45 if anchor_top <= 0.0 else 0.7
    return round(max(0.0, min(1.0, confidence)), 4)


class RetrievalPlan(MemoryDomainModel):
    """Phase 2 object-aware retrieval plan — planner-owned decision object."""

    target_memory_kinds: List[MemoryKind] = Field(default_factory=list)
    query_plan: MemoryQueryPlan = Field(default_factory=MemoryQueryPlan)
    search_profile: MemorySearchProfile
    retrieval_depth: RetrievalDepth
    scope_level: ScopeLevel = ScopeLevel.GLOBAL
    session_scope: Optional[str] = None
    confidence: Optional[float] = None
    decision: str = ""
    drill_uris: List[str] = Field(default_factory=list)
    expand_anchors: List[str] = Field(default_factory=list)
    scope_filter: Optional[Dict[str, Any]] = None


class ExecutionResult(MemoryDomainModel):
    """Phase 3 execution output envelope."""

    items: List[Dict[str, Any]] = Field(default_factory=list)
    trace: ExecutionTrace = Field(default_factory=ExecutionTrace)
    degrade: MemoryRuntimeDegrade = Field(default_factory=MemoryRuntimeDegrade)
