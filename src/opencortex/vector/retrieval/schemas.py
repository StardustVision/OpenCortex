# SPDX-License-Identifier: Apache-2.0
"""Pydantic schemas for opencortex recall."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DetailLevel(StrEnum):
    """Hydration depth for returned recall results."""

    L0 = "l0"
    L1 = "l1"
    L2 = "l2"


class RetrievalDecision(StrEnum):
    """Planner-selected retrieval posture."""

    NO_RECALL = "no_recall"
    FOCUSED = "focused"
    EXPAND = "expand"


class RetrievalSurface(StrEnum):
    """Vector index surfaces available to recall."""

    L0_OBJECT = "l0_object"
    ANCHOR_INDEX = "anchor_index"
    FACT_INDEX = "fact_index"
    ENTITY_INDEX = "entity_index"
    REASON_TREE_INDEX = "reason_tree_index"


class QuerySize(StrEnum):
    """Recall query complexity buckets used by probe preparation."""

    QUICK = "quick"
    MEDIUM = "medium"
    LARGE = "large"


class RecallEvidenceKind(StrEnum):
    """Business-facing evidence categories returned by recall."""

    MEMORY = "memory"
    TOPIC = "topic"
    FACT = "fact"
    ENTITY = "entity"
    SUMMARY = "summary"
    MATCH = "match"


class RetrievalRequest(BaseModel):
    """Public memory recall request."""

    query: str = Field(..., min_length=1)
    limit: int = Field(default=5, ge=1, le=50)

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="after")
    def validate_query(self) -> "RetrievalRequest":
        """Reject blank recall queries."""
        if not self.query.strip():
            raise ValueError("query is required")
        return self


class ProbeEvidence(BaseModel):
    """Score and count signals produced by the bootstrap probe."""

    top_score: float | None = None
    score_gap: float | None = None
    object_top_score: float | None = None
    locator_top_score: float | None = None
    candidate_count: int = 0
    object_candidate_count: int = 0
    locator_candidate_count: int = 0


class RetrievalProbeResult(BaseModel):
    """Probe output consumed by the planner and executor."""

    should_recall: bool = True
    starting_uris: list[str] = Field(default_factory=list)
    evidence: ProbeEvidence = Field(default_factory=ProbeEvidence)
    search_vectors: list[list[float]] = Field(default_factory=list, exclude=True)


class RetrievalPlan(BaseModel):
    """Planner output consumed by the executor."""

    query: str
    limit: int
    candidate_limit: int
    surfaces: list[RetrievalSurface] = Field(default_factory=list)
    surface_limits: dict[RetrievalSurface, int] = Field(default_factory=dict)
    surface_weights: dict[RetrievalSurface, float] = Field(default_factory=dict)
    surface_bonus: dict[RetrievalSurface, float] = Field(default_factory=dict)
    diversity_bonus: float = 0.04
    max_diversity_bonus: float = 0.18
    starting_uri_bonus: float = 0.05
    starting_uris: list[str] = Field(default_factory=list)
    search_vectors: list[list[float]] = Field(default_factory=list, exclude=True)
    confidence: float = 0.0
    decision: RetrievalDecision = RetrievalDecision.EXPAND
    depth: DetailLevel = DetailLevel.L2
    reason_tree: "ReasonTreePlan" = Field(default_factory=lambda: ReasonTreePlan())
    cone_expansion: "ConeExpansionPlan" = Field(
        default_factory=lambda: ConeExpansionPlan()
    )
    rerank: "RerankPlan" = Field(default_factory=lambda: RerankPlan())
    probe: RetrievalProbeResult | None = None


class ReasonTreePlan(BaseModel):
    """Optional LLM-assisted reason-tree entry selection."""

    enabled: bool = False
    use_llm: bool = False
    max_nodes: int = 0


class ConeExpansionPlan(BaseModel):
    """Optional post-rank relation expansion."""

    enabled: bool = False
    max_seeds: int = 0
    max_neighbors_per_seed: int = 0
    bonus: float = 0.03


class RerankPlan(BaseModel):
    """Optional LLM rerank plan for fused recall candidates."""

    seed_enabled: bool = False
    final_enabled: bool = False
    seed_limit: int = 0
    final_limit: int = 0


class RetrievalHit(BaseModel):
    """Internal scored hit from one retrieval surface."""

    record: dict[str, Any]
    score: float = 0.0
    surface: RetrievalSurface
    source_uri: str = ""
    path_cost: float | None = None


class ProbeCandidateEvidence(BaseModel):
    """Internal probe evidence grouped by source URI."""

    uri: str
    score: float
    entities: list[str] = Field(default_factory=list)
    anchors: list[str] = Field(default_factory=list)


class RecallSource(BaseModel):
    """Business source for one recall result."""

    uri: str = ""
    primary_uri: str = ""
    session_id: str = ""
    document_id: str | None = None
    title: str = ""
    section: str = ""


class RecallEvidence(BaseModel):
    """Evidence that explains why a result was returned."""

    uri: str
    kind: RecallEvidenceKind = RecallEvidenceKind.MATCH
    score: float = 0.0
    snippet: str = ""


class MatchedMemory(BaseModel):
    """One user-facing recall result."""

    uri: str
    type: str = ""
    category: str = ""
    abstract: str = ""
    overview: str | None = None
    content: str | None = None
    score: float = 0.0
    session_id: str = ""
    source: RecallSource = Field(default_factory=RecallSource)
    evidence: list[RecallEvidence] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    keywords: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class RetrievalResponse(BaseModel):
    """Public memory recall response."""

    results: list[MatchedMemory] = Field(default_factory=list)
    total: int = 0
