# SPDX-License-Identifier: Apache-2.0
"""Pydantic output schemas for opencortex prompt contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LayerDerivationOutput(BaseModel):
    """LLM-derived L0/L1 and locator fields for one stored record."""

    abstract: str
    overview: str
    keywords: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    anchor_handles: list[str] = Field(default_factory=list)
    fact_points: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")

    @field_validator(
        "keywords",
        "entities",
        "anchor_handles",
        "fact_points",
        mode="before",
    )
    @classmethod
    def normalize_string_list(cls, value: Any) -> list[str]:
        """Accept list-like LLM output and drop blank values."""
        if value is None:
            return []
        if isinstance(value, str):
            values = [part.strip() for part in value.split(",")]
        elif isinstance(value, list):
            values = [str(item).strip() for item in value]
        else:
            return []
        result: list[str] = []
        for item in values:
            if item and item not in result:
                result.append(item)
        return result

    @model_validator(mode="after")
    def require_retrieval_text(self) -> "LayerDerivationOutput":
        """Require the two retrieval text surfaces."""
        if not self.abstract.strip():
            raise ValueError("LLM layer derivation missing abstract")
        if not self.overview.strip():
            raise ValueError("LLM layer derivation missing overview")
        return self


class MemoryExtractionItem(BaseModel):
    """One long-term memory candidate extracted from session content."""

    category: Literal["profile", "preference", "entity", "event", "case", "pattern"]
    abstract: str
    overview: str
    content: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_refs: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class MemoryExtractionOutput(BaseModel):
    """Memory extraction payload from a session/message prompt."""

    memories: list[MemoryExtractionItem] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class ReasonTreeNode(BaseModel):
    """Shared reason-tree node produced from resource or session content."""

    title: str
    summary: str
    fact_points: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    children: list["ReasonTreeNode"] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")

    @field_validator("fact_points", "source_refs", mode="before")
    @classmethod
    def normalize_string_list(cls, value: Any) -> list[str]:
        """Accept list-like LLM output and drop blank values."""
        return LayerDerivationOutput.normalize_string_list(value)


class ReasonTreeOutput(BaseModel):
    """Reason-tree projection shared by resources and session/end."""

    abstract: str
    overview: str
    nodes: list[ReasonTreeNode] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class QueryDecompositionOutput(BaseModel):
    """Short vector-search queries generated from one large recall query."""

    retrieval_queries: list[str] = Field(default_factory=list)
    query_type: str = ""

    model_config = ConfigDict(extra="ignore")


class ReasonTreeSource(BaseModel):
    """Candidate node data shown to the reason-tree selector."""

    uri: str
    title: str = ""
    summary: str = ""
    fact_points: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    context_window: str = ""


class ReasonTreeSelectionOutput(BaseModel):
    """LLM-selected reason-tree entry URIs."""

    selected_uris: list[str] = Field(default_factory=list)
    reason: str = ""

    model_config = ConfigDict(extra="ignore")
