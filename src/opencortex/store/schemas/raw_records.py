# SPDX-License-Identifier: Apache-2.0
"""Raw primary record payloads produced by the synchronous store path."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from opencortex.core.identity import get_effective_project_id
from opencortex.store.schemas.records import Context
from opencortex.store.types import ContextType, MemoryCategory, SessionRecordLayer
from opencortex.utils.uri import CortexURI


class RawPrimaryRecord(BaseModel):
    """Primary Qdrant record before worker semantic derivation."""

    id: str
    uri: str
    parent_uri: str = ""
    is_leaf: bool = True
    context_type: str
    category: str
    scope: str = "shared"
    tenant_id: str
    user_id: str
    source_tenant_id: str
    source_user_id: str
    project_id: str
    session_id: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    content: str = ""
    abstract: str = ""
    overview: str = ""
    entities: list[str] = Field(default_factory=list)
    keywords: str = ""
    abstract_json: dict[str, Any] = Field(default_factory=dict)
    retrieval_surface: str = ""
    retrieval_ready: bool = False
    derive_status: Literal["pending", "ready"] = "pending"
    ttl_expires_at: str = ""
    source_doc_id: str = ""
    source_doc_title: str = ""
    source_section_path: str = ""
    chunk_role: str = ""
    speaker: str = ""
    event_date: Any = None
    event_ts: str = ""
    utterance_ts: str = ""
    date_range_start: str = ""
    date_range_end: str = ""
    time_refs: list[str] = Field(default_factory=list)
    section_index: int | None = None
    vector: list[float] | None = None
    sparse_vector: Any = None
    memory_kind: str = ""
    anchor_hits: list[str] = Field(default_factory=list)
    merge_signature: str = ""
    mergeable: bool = False
    anchor_surface: bool = False

    @classmethod
    def from_context(
        cls,
        *,
        ctx: Context,
        content: str,
        effective_category: str,
        tenant_id: str,
        user_id: str,
        project_id: str,
        session_id: str = "",
        meta: dict[str, Any] | None = None,
        ttl_expires_at: str = "",
    ) -> "RawPrimaryRecord":
        """Build a raw primary record before semantic derivation."""
        context_data = ctx.model_dump(mode="json")
        metadata = dict(meta or {})
        metadata.setdefault("project_id", project_id)
        resolved_project_id = project_id or get_effective_project_id()
        return cls(
            id=context_data["uri"],
            uri=context_data["uri"],
            parent_uri=context_data.get("parent_uri", ""),
            is_leaf=bool(context_data.get("is_leaf", True)),
            context_type=str(context_data.get("context_type", "")),
            category=effective_category,
            scope="private" if CortexURI(context_data["uri"]).is_private else "shared",
            tenant_id=tenant_id,
            user_id=user_id,
            source_tenant_id=tenant_id,
            source_user_id=user_id,
            project_id=resolved_project_id,
            session_id=session_id,
            meta=metadata,
            content=content,
            ttl_expires_at=ttl_expires_at,
            source_doc_id=str(metadata.get("source_doc_id", "") or ""),
            source_doc_title=str(metadata.get("source_doc_title", "") or ""),
            source_section_path=str(metadata.get("source_section_path", "") or ""),
            chunk_role=str(metadata.get("chunk_role", "") or ""),
            speaker=str(metadata.get("speaker", "") or ""),
            event_date=metadata.get("event_date"),
            event_ts=str(metadata.get("event_ts", "") or ""),
            utterance_ts=str(metadata.get("utterance_ts", "") or ""),
            date_range_start=str(metadata.get("date_range_start", "") or ""),
            date_range_end=str(metadata.get("date_range_end", "") or ""),
            time_refs=[str(item) for item in metadata.get("time_refs", []) or []],
            section_index=metadata.get("section_index"),
        )


def primary_ttl(
    *,
    config: Any,
    ttl_from_hours: Any,
    context_type: ContextType,
    category: str,
    layer: str = "",
) -> str:
    """Return TTL for short-lived primary record kinds."""
    if context_type == ContextType.STAGING:
        return ttl_from_hours(config.immediate_event_ttl_hours)
    if (
        context_type == ContextType.MEMORY
        and category == str(MemoryCategory.EVENTS)
        and layer == str(SessionRecordLayer.IMMEDIATE)
    ):
        return ttl_from_hours(config.immediate_event_ttl_hours)
    if (
        context_type == ContextType.MEMORY
        and category == str(MemoryCategory.EVENTS)
        and layer == str(SessionRecordLayer.MERGED)
    ):
        return ttl_from_hours(config.merged_event_ttl_hours)
    return ""
