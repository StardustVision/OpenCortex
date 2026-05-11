# SPDX-License-Identifier: Apache-2.0
"""Primary object and directory vector payload contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from opencortex.vector.payloads.base import TextVectorPayload, VectorPayload


class PrimaryPayload(TextVectorPayload):
    """Payload for retrieval-ready primary object records."""

    abstract_json: dict[str, Any] = Field(default_factory=dict)
    derive_status: Literal["pending", "ready"] = "ready"
    source_doc_id: str = ""
    source_doc_title: str = ""
    source_section_path: str = ""
    chunk_role: str = ""
    speaker: str = ""
    event_date: Any = None
    memory_kind: str = ""
    anchor_hits: list[str] = Field(default_factory=list)
    merge_signature: str = ""
    mergeable: bool = False
    anchor_surface: bool = False


class DirectoryPayload(VectorPayload):
    """Payload-only URI directory record."""

    content: str = ""
    abstract: str = ""
    overview: str = ""
    entities: list[str] = Field(default_factory=list)
    keywords: str = ""
    abstract_json: dict[str, Any] = Field(default_factory=dict)
    derive_status: Literal["ready"] = "ready"
    is_leaf: bool = False


__all__ = ["DirectoryPayload", "PrimaryPayload"]
