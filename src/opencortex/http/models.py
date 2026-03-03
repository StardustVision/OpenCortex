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
# Hooks Learn
# =========================================================================

class HooksLearnRequest(BaseModel):
    state: str
    action: str
    reward: float
    available_actions: str = ""


class HooksRememberRequest(BaseModel):
    content: str
    memory_type: str = "general"


class HooksRecallRequest(BaseModel):
    query: str
    limit: int = 5


# =========================================================================
# Trajectory
# =========================================================================

class TrajectoryBeginRequest(BaseModel):
    trajectory_id: str
    initial_state: str


class TrajectoryStepRequest(BaseModel):
    trajectory_id: str
    action: str
    reward: float
    next_state: str = ""


class TrajectoryEndRequest(BaseModel):
    trajectory_id: str
    quality_score: float


# =========================================================================
# Error
# =========================================================================

class ErrorRecordRequest(BaseModel):
    error: str
    fix: str
    context: str = ""


class ErrorSuggestRequest(BaseModel):
    error: str


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
# Integration
# =========================================================================

class HooksRouteRequest(BaseModel):
    task: str
    agents: str = ""


class HooksInitRequest(BaseModel):
    project_path: str = "."


class HooksPretrainRequest(BaseModel):
    repo_path: str = "."


class HooksExportRequest(BaseModel):
    format: str = "json"


# =========================================================================
# Intent
# =========================================================================

class IntentShouldRecallRequest(BaseModel):
    query: str


# =========================================================================
# Skill Approval & Demotion
# =========================================================================

class SkillReviewRequest(BaseModel):
    skill_id: str
    decision: str  # "approve" | "reject"


class SkillDemoteRequest(BaseModel):
    skill_id: str
    reason: str = ""
