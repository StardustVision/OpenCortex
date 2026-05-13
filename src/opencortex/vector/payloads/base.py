# SPDX-License-Identifier: Apache-2.0
"""Shared vector payload contracts for Qdrant points."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class VectorPayloadSurface(StrEnum):
    """Physical retrieval surfaces stored in the vector collection."""

    L0_OBJECT = "l0_object"
    DIRECTORY = "directory"
    ANCHOR_INDEX = "anchor_index"
    FACT_INDEX = "fact_index"
    ENTITY_INDEX = "entity_index"
    REASON_TREE_INDEX = "reason_tree_index"


class VectorPayload(BaseModel):
    """Base payload shared by all vector-store point types."""

    id: str
    uri: str
    parent_uri: str = ""
    context_type: str = ""
    category: str = ""
    scope: str = ""
    tenant_id: str = ""
    user_id: str = ""
    source_tenant_id: str = ""
    source_user_id: str = ""
    project_id: str = ""
    session_id: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    is_leaf: bool = True
    retrieval_surface: str = ""
    retrieval_ready: bool = True
    ttl_expires_at: str = ""
    event_ts: str = ""
    utterance_ts: str = ""
    date_range_start: str = ""
    date_range_end: str = ""
    time_refs: list[str] = Field(default_factory=list)
    section_index: int | None = None

    model_config = ConfigDict(extra="forbid")

    def to_record(self) -> dict[str, Any]:
        """Return a JSON-serializable Qdrant payload."""
        return self.model_dump(mode="json")


class TextVectorPayload(VectorPayload):
    """Vector payload with text and semantic summary fields."""

    content: str = ""
    abstract: str = ""
    overview: str = ""
    entities: list[str] = Field(default_factory=list)
    keywords: str = ""


class SourceLinkedPayload(TextVectorPayload):
    """Payload for secondary indexes that point back to a primary record."""

    source_uri: str
    source_record_id: str


__all__ = [
    "SourceLinkedPayload",
    "TextVectorPayload",
    "VectorPayload",
    "VectorPayloadSurface",
]
