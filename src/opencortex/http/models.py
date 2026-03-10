# SPDX-License-Identifier: Apache-2.0
"""
Pydantic request/response models for the OpenCortex HTTP Server.

Each model mirrors the parameters of the corresponding MCP tool in
``mcp_server.py``.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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
        description="Category: profile, preferences, entities, events, cases, patterns, "
                    "error_fixes, workflows, strategies, documents, plans",
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
        description="Filter by category (profile, preferences, entities, events, cases, patterns, etc.)",
    )
    detail_level: str = "l1"


class MemoryFeedbackRequest(BaseModel):
    uri: str
    reward: float


# =========================================================================
# Session
# =========================================================================

class SessionBeginRequest(BaseModel):
    session_id: str


class SessionMessageRequest(BaseModel):
    session_id: str
    role: str
    content: str


class SessionEndRequest(BaseModel):
    session_id: str
    quality_score: float = 0.5


class SessionExtractTurnRequest(BaseModel):
    session_id: str
    quality_score: float = 0.5


# =========================================================================
# Batch Import
# =========================================================================

class MemoryBatchItem(BaseModel):
    content: str
    category: str = "documents"
    context_type: str = "resource"
    meta: Optional[Dict[str, Any]] = None


class MemoryBatchStoreRequest(BaseModel):
    items: List[MemoryBatchItem]
    source_path: str = ""
    scan_meta: Optional[Dict[str, Any]] = None


class PromoteToSharedRequest(BaseModel):
    uris: List[str]
    project_id: str


# =========================================================================
# Intent
# =========================================================================

class IntentShouldRecallRequest(BaseModel):
    query: str


# =========================================================================
# Skill Evolution
# =========================================================================

class SkillLookupRequest(BaseModel):
    objective: str
    section: str = ""
    limit: int = 5


class SkillFeedbackRequest(BaseModel):
    uri: str
    session_id: str = ""
    turn_uuid: str = ""
    success: bool = True
    score: float = 1.0


class SkillMineRequest(BaseModel):
    section: str = ""
    min_cases: int = 5
    max_cases: int = 200
    max_clusters: int = 10
    llm_budget: int = 5


class SkillEvolveRequest(BaseModel):
    uri: str
    confidence_threshold: float = 0.3
    observation_turns: int = 10


# =========================================================================
# Cortex Alpha
# =========================================================================

class SessionMessagesRequest(BaseModel):
    """Batch message recording (Observer debounce buffer)."""
    session_id: str
    messages: List[Dict[str, Any]]  # [{role, content, timestamp?}]


class KnowledgeSearchRequest(BaseModel):
    query: str
    types: Optional[List[str]] = None
    limit: int = 10


class KnowledgeApproveRequest(BaseModel):
    knowledge_id: str


class KnowledgeRejectRequest(BaseModel):
    knowledge_id: str


class KnowledgePromoteRequest(BaseModel):
    knowledge_id: str
    new_scope: str


class TraceSplitRequest(BaseModel):
    session_id: str


class TraceListRequest(BaseModel):
    session_id: str
