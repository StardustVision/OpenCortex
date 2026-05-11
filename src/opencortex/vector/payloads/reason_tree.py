# SPDX-License-Identifier: Apache-2.0
"""Reason-tree vector payload contracts."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from opencortex.vector.payloads.base import SourceLinkedPayload


class ReasonTreePayload(SourceLinkedPayload):
    """Payload for reason-tree selector and expansion indexes."""

    parent_source_uri: str = ""
    tree_uri: str = ""
    path: str = ""
    path_segments: list[str] = Field(default_factory=list)
    level: int
    reason_role: str = "leaf"
    context_window: str = "self"
    source_uris: list[str] = Field(default_factory=list)
    merged_uris: list[str] = Field(default_factory=list)
    title: str = ""
    summary: str = ""
    fact_points: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    anchor_hits: list[str] = Field(default_factory=list)
    memory_kind: str = ""
    cone_seed: bool = True
    cone_neighbors: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


__all__ = ["ReasonTreePayload"]
