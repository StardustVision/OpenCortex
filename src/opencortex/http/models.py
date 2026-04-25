# SPDX-License-Identifier: Apache-2.0
"""Pydantic request/response models for the OpenCortex HTTP Server.

Each model mirrors the parameters of the corresponding MCP tool in
``mcp_server.py``.
"""

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

# =========================================================================
# Benchmark ingest payload limits — comfortably above LoCoMo / LongMemEval
# real distributions. Hard caps the per-request fan-out to keep one bad
# admin payload from saturating the LLM and embedder pools.
# =========================================================================

_BENCHMARK_MAX_SEGMENTS = 200
_BENCHMARK_MAX_MESSAGES_PER_SEGMENT = 2_000
_BENCHMARK_MAX_CONTENT_LENGTH = 64_000
_BENCHMARK_MAX_META_BYTES = 16_384

# =========================================================================
# Core Memory
# =========================================================================


class MemoryStoreRequest(BaseModel):
    """Store a new memory, resource, or skill.

    context_type: memory | resource | skill | case | pattern
    category: profile | preferences | entities | events | cases | patterns |
              error_fixes | workflows | strategies | documents | plans
    """

    abstract: str
    content: str = ""
    overview: str = ""
    category: str = Field(
        default="",
        description=(
            "Category: profile, preferences, entities, events, cases, "
            "patterns, error_fixes, workflows, strategies, documents, plans"
        ),
    )
    context_type: str = Field(
        default="memory",
        description="Type: memory, resource, skill, case, pattern",
    )
    meta: Optional[Dict[str, Any]] = None
    dedup: bool = Field(
        default=True,
        description="Check for semantic duplicates before storing. "
        "Set False for bulk import.",
    )
    embed_text: str = Field(
        default="",
        description="Optional text used for embedding instead of abstract. "
        "Useful when the display text differs from the optimal "
        "search text (e.g., omitting date prefixes).",
    )


class MemorySearchRequest(BaseModel):
    """Semantic search across stored memories, resources, and skills."""

    query: str
    limit: int = 5
    context_type: Optional[str] = Field(
        default=None,
        description="Filter by type (memory, resource, skill, case, pattern)",
    )
    category: Optional[str] = Field(
        default=None,
        description=(
            "Filter by category (profile, preferences, entities, events, "
            "cases, patterns, etc.)"
        ),
    )
    detail_level: str = "l1"
    metadata_filter: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional structured metadata filter for benchmark-scoped search.",
    )


class MemorySearchResultItem(BaseModel):
    """One result item returned by `/api/v1/memory/search`."""

    uri: str
    abstract: str
    context_type: str
    score: Optional[float] = None
    overview: Optional[str] = None
    content: Optional[str] = None
    keywords: Optional[str] = None
    source_doc_id: Optional[str] = None
    source_doc_title: Optional[str] = None
    source_section_path: Optional[str] = None
    source_uri: Optional[str] = None
    msg_range: Optional[List[int]] = None
    recomposition_stage: Optional[str] = None
    matched_anchors: Optional[List[str]] = None
    cone_used: Optional[bool] = None


class MemorySearchPipeline(BaseModel):
    """Phase-native pipeline payload for `memory/search`."""

    probe: Optional[Dict[str, Any]] = None
    planner: Optional[Dict[str, Any]] = None
    runtime: Optional[Dict[str, Any]] = None


class MemorySearchResponse(BaseModel):
    """Typed response payload for `/api/v1/memory/search`."""

    results: List[MemorySearchResultItem]
    total: int
    memory_pipeline: Optional[MemorySearchPipeline] = None
    explain_summary: Optional[Dict[str, Any]] = None
    explain_detail: Optional[List[Dict[str, Any]]] = None


class MemoryForgetRequest(BaseModel):
    """Delete a memory by URI or semantic query."""

    uri: str = Field(default="", description="URI to delete (exact)")
    query: str = Field(
        default="",
        description="Semantic search query — finds and deletes top match",
    )


class MemoryFeedbackRequest(BaseModel):
    """Submit reward feedback for a stored memory."""

    uri: str
    reward: float


# =========================================================================
# Session
# =========================================================================


class SessionBeginRequest(BaseModel):
    """Start a session transcript."""

    session_id: str


class SessionMessageRequest(BaseModel):
    """Append one message to an active session transcript."""

    session_id: str
    role: str
    content: str


class SessionEndRequest(BaseModel):
    """Close a session transcript and trigger post-processing."""

    session_id: str
    quality_score: float = 0.5


# =========================================================================
# Batch Import
# =========================================================================


class MemoryBatchItem(BaseModel):
    """One batch-ingested memory or resource payload."""

    content: str
    category: str = "documents"
    context_type: str = "resource"
    meta: Optional[Dict[str, Any]] = None


class MemoryBatchStoreRequest(BaseModel):
    """Batch store request payload."""

    items: List[MemoryBatchItem]
    source_path: str = ""
    scan_meta: Optional[Dict[str, Any]] = None


class PromoteToSharedRequest(BaseModel):
    """Promote private memory URIs into a shared project scope."""

    uris: List[str]
    project_id: str


# =========================================================================
# Intent
# =========================================================================


class IntentShouldRecallRequest(BaseModel):
    """Probe whether a query should trigger memory recall."""

    query: str


# =========================================================================
# Cortex Alpha
# =========================================================================


class SessionMessagesRequest(BaseModel):
    """Batch message recording (Observer debounce buffer)."""

    session_id: str
    messages: List[Dict[str, Any]]  # [{role, content, timestamp?}]


class KnowledgeSearchRequest(BaseModel):
    """Search approved knowledge artifacts."""

    query: str
    types: Optional[List[str]] = None
    limit: int = 10


class KnowledgeApproveRequest(BaseModel):
    """Approve a candidate knowledge item."""

    knowledge_id: str


class KnowledgeRejectRequest(BaseModel):
    """Reject a candidate knowledge item."""

    knowledge_id: str


class KnowledgePromoteRequest(BaseModel):
    """Promote knowledge to a broader visibility scope."""

    knowledge_id: str
    new_scope: str


class TraceSplitRequest(BaseModel):
    """Request trace splitting for one session."""

    session_id: str


class TraceListRequest(BaseModel):
    """List traces for one session."""

    session_id: str


# =========================================================================
# Context Protocol
# =========================================================================


class ToolCallRecord(BaseModel):
    """Structured tool usage record from MCP add_message."""

    name: str
    summary: str = ""


class ContextMessage(BaseModel):
    """One context lifecycle message payload."""

    role: str
    content: str
    meta: Optional[Dict[str, Any]] = None


class ContextConfig(BaseModel):
    """Runtime knobs for the context lifecycle endpoint."""

    max_items: int = Field(default=5, ge=1, le=20)
    detail_level: str = "l1"  # l0 | l1 | l2
    recall_mode: str = "auto"  # auto | always | never
    fail_fast_end: bool = False


class ContextRequest(BaseModel):
    """Unified `/api/v1/context` request payload."""

    session_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    turn_id: Optional[str] = Field(
        default=None,
        pattern=r"^[a-zA-Z0-9_-]{1,128}$",
    )
    phase: str  # prepare | commit | end
    messages: Optional[List[ContextMessage]] = None
    tool_calls: Optional[List[ToolCallRecord]] = None
    cited_uris: Optional[List[str]] = None
    config: Optional[ContextConfig] = None


class BenchmarkConversationMessage(BaseModel):
    """One benchmark-ingest message with hard size caps.

    Mirrors :class:`ContextMessage` shape but enforces ``content`` and
    ``meta`` limits at the request boundary so a single admin payload
    cannot fan out to unbounded embed / LLM work.
    """

    role: str = Field(..., min_length=1, max_length=64)
    content: str = Field(..., max_length=_BENCHMARK_MAX_CONTENT_LENGTH)
    meta: Optional[Dict[str, Any]] = None

    @field_validator("meta")
    @classmethod
    def _meta_within_byte_budget(
        cls, value: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if value is None:
            return value
        try:
            serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("meta must be JSON-serializable") from exc
        if len(serialized.encode("utf-8")) > _BENCHMARK_MAX_META_BYTES:
            raise ValueError(
                f"meta exceeds {_BENCHMARK_MAX_META_BYTES}-byte limit"
            )
        return value


class BenchmarkConversationSegment(BaseModel):
    """One offline conversation segment used for benchmark ingest."""

    messages: List[BenchmarkConversationMessage] = Field(
        ..., max_length=_BENCHMARK_MAX_MESSAGES_PER_SEGMENT
    )


class BenchmarkConversationIngestRequest(BaseModel):
    """Benchmark-only offline conversation ingest request."""

    session_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    segments: List[BenchmarkConversationSegment] = Field(
        ..., max_length=_BENCHMARK_MAX_SEGMENTS
    )
    include_session_summary: bool = True
    ingest_shape: str = Field(
        default="merged_recompose",
        description=(
            "Benchmark ingest shape. 'merged_recompose' stores merged offline "
            "conversation leaves and runs full recomposition; 'direct_evidence' "
            "stores each supplied segment as a searchable evidence unit without "
            "full session recomposition."
        ),
    )


class ContextPrepareIntent(BaseModel):
    """Intent envelope returned by `/api/v1/context` prepare."""

    should_recall: bool
    probe_candidate_count: int
    probe_top_score: Optional[float] = None
    depth: str
    memory_pipeline: Optional[MemorySearchPipeline] = None


class ContextPrepareMemoryItem(BaseModel):
    """One recalled memory item in the context prepare response."""

    uri: str
    abstract: str
    score: float
    context_type: str
    category: str
    session_id: Optional[str] = None
    source_uri: Optional[str] = None
    msg_range: Optional[List[int]] = None
    recomposition_stage: Optional[str] = None
    matched_anchors: Optional[List[str]] = None
    cone_used: Optional[bool] = None
    overview: Optional[str] = None
    content: Optional[str] = None


class ContextPrepareKnowledgeItem(BaseModel):
    """One knowledge item in the context prepare response."""

    knowledge_id: str
    type: str
    abstract: str
    confidence: float


class ContextPrepareInstructions(BaseModel):
    """Agent-facing guidance emitted by prepare."""

    should_cite_memory: bool
    memory_confidence: float
    recall_count: int
    guidance: str


class ContextPrepareResponse(BaseModel):
    """Typed response payload for `/api/v1/context` prepare."""

    session_id: str
    turn_id: str
    intent: ContextPrepareIntent
    memory: List[ContextPrepareMemoryItem]
    knowledge: List[ContextPrepareKnowledgeItem]
    instructions: ContextPrepareInstructions


# =========================================================================
# Admin — Token Management
# =========================================================================


class CreateTokenRequest(BaseModel):
    """Create a new JWT token for a tenant/user pair."""

    tenant_id: str
    user_id: str


class RevokeTokenRequest(BaseModel):
    """Revoke a token by prefix."""

    token_prefix: str
