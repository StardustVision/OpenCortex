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
    abstract: str
    content: str = ""
    overview: str = ""
    category: str = ""
    context_type: str = "memory"
    uri: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None


class MemorySearchRequest(BaseModel):
    query: str
    limit: int = 5
    context_type: Optional[str] = None
    category: Optional[str] = None
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
