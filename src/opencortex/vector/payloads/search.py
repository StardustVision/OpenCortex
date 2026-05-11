# SPDX-License-Identifier: Apache-2.0
"""Search-side vector payload contracts."""

from __future__ import annotations

from pydantic import Field

from opencortex.vector.payloads.base import SourceLinkedPayload


class AnchorIndexPayload(SourceLinkedPayload):
    """Payload for entity, keyword, and handle anchor indexes."""

    anchor_type: str = "term"
    index_score: float = 1.0
    anchor_hits: list[str] = Field(default_factory=list)
    memory_kind: str = ""
    cone_seed: bool = True


class FactIndexPayload(SourceLinkedPayload):
    """Payload for fact sentence indexes."""

    index_score: float = 1.0
    anchor_hits: list[str] = Field(default_factory=list)
    memory_kind: str = ""
    cone_seed: bool = True


class EntityIndexPayload(SourceLinkedPayload):
    """Payload for entity-only indexes."""

    entity_text: str
    anchor_hits: list[str] = Field(default_factory=list)
    memory_kind: str = ""


__all__ = ["AnchorIndexPayload", "EntityIndexPayload", "FactIndexPayload"]
