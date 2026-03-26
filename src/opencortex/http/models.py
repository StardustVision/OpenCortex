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
        description="Filter by category (profile, preferences, entities, events, cases, patterns, etc.)",
    )
    detail_level: str = "l1"


class MemoryForgetRequest(BaseModel):
    """Delete a memory by URI or semantic query."""
    uri: str = Field(default="", description="URI to delete (exact)")
    query: str = Field(
        default="",
        description="Semantic search query — finds and deletes top match",
    )


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


# =========================================================================
# Context Protocol
# =========================================================================

class ContextMessage(BaseModel):
    role: str
    content: str


class ContextConfig(BaseModel):
    max_items: int = Field(default=5, ge=1, le=20)
    detail_level: str = "l1"      # l0 | l1 | l2
    recall_mode: str = "auto"     # auto | always | never


class ContextRequest(BaseModel):
    session_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    turn_id: Optional[str] = Field(
        default=None, pattern=r"^[a-zA-Z0-9_-]{1,128}$",
    )
    phase: str                     # prepare | commit | end
    messages: Optional[List[ContextMessage]] = None
    cited_uris: Optional[List[str]] = None
    config: Optional[ContextConfig] = None


# =========================================================================
# Admin — Token Management
# =========================================================================

class CreateTokenRequest(BaseModel):
    tenant_id: str
    user_id: str


class RevokeTokenRequest(BaseModel):
    token_prefix: str
