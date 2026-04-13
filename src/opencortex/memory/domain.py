# SPDX-License-Identifier: Apache-2.0
"""Shared memory-domain contracts for planner, runtime, and store."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class MemoryDomainModel(BaseModel):
    """Base model with stable JSON-friendly serialization."""

    model_config = ConfigDict(extra="forbid")

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable dictionary."""
        return self.model_dump(mode="json")


class MemoryKind(str, Enum):
    """Primary normalized memory kinds."""

    EVENT = "event"
    PROFILE = "profile"
    PREFERENCE = "preference"
    CONSTRAINT = "constraint"
    RELATION = "relation"
    DOCUMENT_CHUNK = "document_chunk"
    SUMMARY = "summary"


class StructuredSlots(MemoryDomainModel):
    """Bounded shared and kind-specific structured slots."""

    entities: List[str] = Field(default_factory=list)
    time_refs: List[str] = Field(default_factory=list)
    topics: List[str] = Field(default_factory=list)
    preferences: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    relations: List[str] = Field(default_factory=list)
    document_refs: List[str] = Field(default_factory=list)
    summary_refs: List[str] = Field(default_factory=list)


class AnchorEntry(MemoryDomainModel):
    """Derived anchor entry stored in `.abstract.json`."""

    anchor_type: str
    value: str
    text: str = ""


class MemoryLineage(MemoryDomainModel):
    """Bounded lineage fields shared across ingest modes."""

    parent_uri: str = ""
    session_id: str = ""
    source_doc_id: str = ""
    source_doc_title: str = ""
    section_path: List[str] = Field(default_factory=list)
    chunk_index: Optional[int] = None


class MemorySource(MemoryDomainModel):
    """Cheap source metadata for retrieval and traceability."""

    context_type: str = ""
    category: str = ""
    source_path: str = ""


class MemoryQuality(MemoryDomainModel):
    """Cheap quality counters for the machine-readable L0 surface."""

    anchor_count: int = 0
    entity_count: int = 0
    keyword_count: int = 0


class MemoryAbstract(MemoryDomainModel):
    """Fixed shared `.abstract.json` schema for all memory kinds."""

    uri: str
    memory_kind: MemoryKind
    context_type: str = ""
    category: str = ""
    summary: str = ""
    anchors: List[AnchorEntry] = Field(default_factory=list)
    slots: StructuredSlots = Field(default_factory=StructuredSlots)
    lineage: MemoryLineage = Field(default_factory=MemoryLineage)
    source: MemorySource = Field(default_factory=MemorySource)
    quality: MemoryQuality = Field(default_factory=MemoryQuality)


class MemoryKindPolicy(MemoryDomainModel):
    """Kind-level retrieval and execution policy metadata."""

    mergeable: bool
    association_friendly: bool
    hydration_ceiling: str
    retrieval_context_types: List[str] = Field(default_factory=list)
    retrieval_categories: List[str] = Field(default_factory=list)


class MemoryEntry(MemoryDomainModel):
    """Normalized memory entry projected from current unified-store records."""

    uri: str
    memory_kind: MemoryKind
    structured_slots: StructuredSlots = Field(default_factory=StructuredSlots)
    policy: MemoryKindPolicy
    context_type: str = ""
    category: str = ""
    abstract: str = ""
    overview: Optional[str] = None
    content: Optional[str] = None
    anchor_entries: List[AnchorEntry] = Field(default_factory=list)
    lineage: MemoryLineage = Field(default_factory=MemoryLineage)
    source: MemorySource = Field(default_factory=MemorySource)
    quality: MemoryQuality = Field(default_factory=MemoryQuality)
    metadata: Dict[str, Any] = Field(default_factory=dict)


_KIND_POLICIES: Dict[MemoryKind, MemoryKindPolicy] = {
    MemoryKind.EVENT: MemoryKindPolicy(
        mergeable=False,
        association_friendly=True,
        hydration_ceiling="l1",
        retrieval_context_types=["memory"],
        retrieval_categories=["event", "events"],
    ),
    MemoryKind.PROFILE: MemoryKindPolicy(
        mergeable=True,
        association_friendly=False,
        hydration_ceiling="l1",
        retrieval_context_types=["memory"],
        retrieval_categories=["profile"],
    ),
    MemoryKind.PREFERENCE: MemoryKindPolicy(
        mergeable=True,
        association_friendly=False,
        hydration_ceiling="l1",
        retrieval_context_types=["memory"],
        retrieval_categories=["preference", "preferences"],
    ),
    MemoryKind.CONSTRAINT: MemoryKindPolicy(
        mergeable=False,
        association_friendly=False,
        hydration_ceiling="l1",
        retrieval_context_types=["memory"],
        retrieval_categories=["constraint", "constraints"],
    ),
    MemoryKind.RELATION: MemoryKindPolicy(
        mergeable=False,
        association_friendly=True,
        hydration_ceiling="l1",
        retrieval_context_types=["memory"],
        retrieval_categories=["relation", "relations", "entity", "entities"],
    ),
    MemoryKind.DOCUMENT_CHUNK: MemoryKindPolicy(
        mergeable=False,
        association_friendly=True,
        hydration_ceiling="l2",
        retrieval_context_types=["resource", "any"],
        retrieval_categories=["document", "document_chunk", "resource", "case", "pattern"],
    ),
    MemoryKind.SUMMARY: MemoryKindPolicy(
        mergeable=False,
        association_friendly=True,
        hydration_ceiling="l1",
        retrieval_context_types=["memory", "resource", "any"],
        retrieval_categories=["summary", "summaries"],
    ),
}


def memory_kind_policy(kind: MemoryKind) -> MemoryKindPolicy:
    """Return policy metadata for a memory kind."""
    return _KIND_POLICIES[kind]
