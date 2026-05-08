# SPDX-License-Identifier: Apache-2.0
"""Pydantic schemas for store flows."""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opencortex_app.store.schemas.records import Context
from opencortex_app.store.types import (
    ContextType,
    StoreMemoryCategory,
    StoreMetadataKey,
    StoreRecordType,
    StoreSourceKind,
)


class StoreSource(BaseModel):
    """Structured source information for a store request."""

    kind: StoreSourceKind
    id: str = ""
    uri: str = ""
    path: str = ""
    title: str = ""
    section: str = ""

    model_config = ConfigDict(extra="forbid")


class StoreRequest(BaseModel):
    """Request to store one memory or resource content record."""

    type: StoreRecordType
    content: str = Field(..., min_length=1)
    category: StoreMemoryCategory
    metadata: dict[str, Any]
    source: StoreSource

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        """Reject content that is only whitespace."""
        if not self.content.strip():
            raise ValueError("content is required")
        return self


class MemoryStoreInput(BaseModel):
    """Validated content input for the memory store flow."""

    content: str = ""
    category: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_memory(self) -> Self:
        """Validate memory-specific business input."""
        if not self.content.strip():
            raise ValueError("memory content is required")
        return self


class ResourceStoreInput(BaseModel):
    """Validated content input for the resource store flow."""

    content: str = ""
    category: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)

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


class ToolCallRecord(BaseModel):
    """Structured tool usage record captured from an agent turn."""

    name: str
    summary: str = ""


class SessionTurnRequest(BaseModel):
    """Store one conversation turn."""

    session_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    turn_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    messages: list[SessionMessage] = Field(..., min_length=1)
    tool_calls: list[ToolCallRecord] | None = None
    cited_uris: list[str] | None = None


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
    payload: dict[str, Any] = Field(default_factory=dict)
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


class SessionEndRequest(BaseModel):
    """Close one conversation session."""

    session_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,128}$")


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
        content=req.content,
        category=str(req.category),
        meta=store_meta_from_request(req),
    )


def resource_store_input_from_request(req: Any) -> ResourceStoreInput:
    """Validate a HTTP store request as a resource input."""
    return ResourceStoreInput(
        content=req.content,
        category=str(req.category),
        meta=store_meta_from_request(req),
    )


def store_meta_from_request(req: StoreRequest) -> dict[str, Any]:
    """Map public metadata and source fields to internal store metadata."""
    source = req.source.model_dump()
    meta = dict(req.metadata)
    meta[str(StoreMetadataKey.SOURCE)] = source

    if req.source.path:
        meta.setdefault(str(StoreMetadataKey.SOURCE_PATH), req.source.path)
        meta.setdefault(str(StoreMetadataKey.FILE_PATH), req.source.path)
    if req.source.title:
        meta.setdefault(str(StoreMetadataKey.SOURCE_DOC_TITLE), req.source.title)
        meta.setdefault(str(StoreMetadataKey.TITLE), req.source.title)
    if req.source.section:
        meta.setdefault(str(StoreMetadataKey.SOURCE_SECTION_PATH), req.source.section)
    return meta


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
