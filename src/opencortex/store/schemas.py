# SPDX-License-Identifier: Apache-2.0
"""Pydantic schemas for store flows."""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

from opencortex.core.context import Context
from opencortex.retrieve.types import ContextType


class MemoryStoreInput(BaseModel):
    """Validated input for the memory store flow."""

    abstract: str
    content: str = ""
    overview: str = ""
    category: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    embed_text: str = ""

    @model_validator(mode="after")
    def validate_memory(self) -> Self:
        """Validate memory-specific business input."""
        if not self.abstract.strip():
            raise ValueError("memory abstract is required")
        return self


class ResourceStoreInput(BaseModel):
    """Validated input for the resource store flow."""

    abstract: str = ""
    content: str
    overview: str = ""
    category: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    embed_text: str = ""

    @model_validator(mode="after")
    def validate_resource(self) -> Self:
        """Validate resource-specific business input."""
        if not self.content.strip():
            raise ValueError("resource content is required")
        return self

    @property
    def source_path(self) -> str:
        """Return the source path from resource metadata."""
        return str(self.meta.get("source_path", "") or "")


class SessionMessage(BaseModel):
    """One validated conversation message for immediate storage."""

    role: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    meta: dict[str, Any] = Field(default_factory=dict)


class SessionMessageInput(BaseModel):
    """Validated input for the session message flow."""

    session_id: str = Field(..., min_length=1)
    turn_id: str = Field(..., min_length=1)
    messages: list[SessionMessage] = Field(..., min_length=1)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    cited_uris: list[str] = Field(default_factory=list)


class StoreEmbedding(BaseModel):
    """Embedding result attached to a store draft."""

    embed_ms: int = 0
    sparse_vector: Any = None


class StoreTarget(BaseModel):
    """Resolved primary-record target."""

    uri: str
    parent_uri: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    explicit_entities: list[str] = Field(default_factory=list)
    explicit_topics: list[str] = Field(default_factory=list)


class StoreDerived(BaseModel):
    """Derived store fields."""

    abstract: str = ""
    overview: str = ""
    layers: dict[str, Any] = Field(default_factory=dict)
    derive_ms: int = 0


class StoreDraft(BaseModel):
    """Assembled draft before embedding and writing."""

    ctx: Context
    abstract: str = ""
    overview: str = ""
    keywords: str = ""
    keywords_list: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    effective_category: str = ""
    abstract_json: dict[str, Any] = Field(default_factory=dict)
    object_payload: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


class PrimaryRecordInput(BaseModel):
    """Fixed input accepted by the primary record writer."""

    ctx: Context
    abstract_json: dict[str, Any] = Field(default_factory=dict)
    object_payload: dict[str, Any] = Field(default_factory=dict)
    effective_category: str = ""
    keywords: str = ""
    entities: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    context_type: ContextType
    session_id: str = ""
    tenant_id: str
    user_id: str
    sparse_vector: Any = None
    content: str = ""

    model_config = {"arbitrary_types_allowed": True}


class StoredRecord(BaseModel):
    """Primary record write result."""

    uri: str
    context_type: str
    category: str
    abstract: str
    overview: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    record: dict[str, Any] = Field(default_factory=dict)
    upsert_ms: int = 0


class SessionMessageResult(BaseModel):
    """Result returned by the session message flow."""

    accepted: bool = True
    write_status: str = "ok"
    turn_id: str
    written_uris: list[str] = Field(default_factory=list)
    merge_requested: bool = False


class SessionEndInput(BaseModel):
    """Validated input for the session end flow."""

    session_id: str = Field(..., min_length=1)


class SessionEndResult(BaseModel):
    """Result returned by the session end flow."""

    accepted: bool = True
    write_status: str = "ok"
    session_id: str
    merged_uris: list[str] = Field(default_factory=list)
    final_uri: str = ""


def memory_store_input_from_request(req: Any) -> MemoryStoreInput:
    """Validate a HTTP store request as a memory input."""
    return MemoryStoreInput(
        abstract=req.abstract,
        content=req.content,
        overview=req.overview,
        category=req.category,
        meta=dict(req.meta or {}),
        embed_text=req.embed_text,
    )


def resource_store_input_from_request(req: Any) -> ResourceStoreInput:
    """Validate a HTTP store request as a resource input."""
    return ResourceStoreInput(
        abstract=req.abstract,
        content=req.content,
        overview=req.overview,
        category=req.category,
        meta=dict(req.meta or {}),
        embed_text=req.embed_text,
    )


def session_message_input_from_request(req: Any) -> SessionMessageInput:
    """Validate a HTTP session message request as store input."""
    return SessionMessageInput(
        session_id=req.session_id,
        turn_id=req.turn_id,
        messages=[
            SessionMessage(
                role=message.role,
                content=message.content,
                meta=dict(message.meta or {}),
            )
            for message in req.messages
        ],
        tool_calls=[tool_call.model_dump() for tool_call in req.tool_calls]
        if req.tool_calls
        else [],
        cited_uris=list(req.cited_uris or []),
    )


def session_end_input_from_request(req: Any) -> SessionEndInput:
    """Validate a HTTP session end request as store input."""
    return SessionEndInput(session_id=req.session_id)
