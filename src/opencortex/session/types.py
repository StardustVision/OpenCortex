# SPDX-License-Identifier: Apache-2.0
"""Data types for OpenCortex session management."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Message:
    """A single message in a session conversation."""

    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionContext:
    """Active session state."""

    session_id: str
    tenant_id: str = ""
    user_id: str = ""
    messages: List[Message] = field(default_factory=list)
    started_at: float = 0.0
    summary: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedMemory:
    """A single memory extracted from session analysis."""

    abstract: str
    content: str = ""
    category: str = ""
    context_type: str = "memory"  # "memory" | "skill" | "resource"
    confidence: float = 0.0
    uri_hint: str = ""  # suggested target URI (user/agent)


@dataclass
class ExtractionResult:
    """Result of session memory extraction."""

    session_id: str
    memories: List[ExtractedMemory] = field(default_factory=list)
    stored_count: int = 0
    merged_count: int = 0
    skipped_count: int = 0
    quality_score: float = 0.0
    reasoning: str = ""
