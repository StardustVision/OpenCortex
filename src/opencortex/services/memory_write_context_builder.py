# SPDX-License-Identifier: Apache-2.0
"""Context and payload assembly for memory writes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opencortex.core.context import Context, Vectorize
from opencortex.core.user_id import UserIdentifier
from opencortex.http.request_context import get_effective_identity

if TYPE_CHECKING:
    from opencortex.services.memory_write_service import MemoryWriteService


@dataclass(frozen=True)
class ResolvedWriteTarget:
    """Pre-derive write target and explicit metadata inputs."""

    uri: str
    parent_uri: Optional[str]
    existing_record: Optional[Dict[str, Any]]
    meta: Dict[str, Any]
    explicit_entities: List[str] = field(default_factory=list)
    explicit_topics: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class AssembledWriteContext:
    """Post-derive Context and memory object payloads."""

    ctx: Context
    abstract: str
    overview: str
    keywords: str
    keywords_list: List[str]
    entities: List[str]
    meta: Dict[str, Any]
    effective_category: str
    abstract_json: Dict[str, Any]
    object_payload: Dict[str, Any]
    merge_signature: str
    mergeable: bool


class MemoryWriteContextBuilder:
    """Builds transient write Context objects and related payloads."""

    def __init__(self, write_service: "MemoryWriteService") -> None:
        """Bind the builder to a write service facade."""
        self._write_service = write_service

    @property
    def _orch(self) -> Any:
        return self._write_service._orch

    async def resolve_target(
        self,
        *,
        abstract: str,
        category: str,
        context_type: Optional[str],
        meta: Optional[Dict[str, Any]],
        parent_uri: Optional[str],
        uri: Optional[str],
    ) -> ResolvedWriteTarget:
        """Resolve URI, parent URI, existing record, and explicit metadata."""
        from opencortex.orchestrator import _merge_unique_strings

        orch = self._orch
        resolved_meta = dict(meta or {})
        explicit_entities = _merge_unique_strings(resolved_meta.get("entities"))
        explicit_topics = _merge_unique_strings(resolved_meta.get("topics"))

        if not uri:
            resolved_uri = orch._auto_uri(
                context_type or "memory",
                category,
                abstract=abstract,
            )
            resolved_uri = await orch._resolve_unique_uri(resolved_uri)
            existing_record = None
        else:
            resolved_uri = uri
            existing_record = await orch._get_record_by_uri(resolved_uri)

        resolved_parent_uri = parent_uri or orch._derive_parent_uri(resolved_uri)
        return ResolvedWriteTarget(
            uri=resolved_uri,
            parent_uri=resolved_parent_uri,
            existing_record=existing_record,
            meta=resolved_meta,
            explicit_entities=explicit_entities,
            explicit_topics=explicit_topics,
        )

    def assemble_context(
        self,
        *,
        target: ResolvedWriteTarget,
        abstract: str,
        overview: str,
        content: str,
        category: str,
        context_type: Optional[str],
        is_leaf: bool,
        related_uri: Optional[List[str]],
        session_id: Optional[str],
        embed_text: str,
        layers: Dict[str, Any],
    ) -> AssembledWriteContext:
        """Assemble the post-derive Context, metadata, and object payload."""
        from opencortex.orchestrator import (
            _merge_unique_strings,
            _split_keyword_string,
        )

        orch = self._orch
        meta = target.meta
        derived_entities = layers.get("entities", []) if content and is_leaf else []
        entities = _merge_unique_strings(derived_entities, target.explicit_entities)
        keywords_list = _merge_unique_strings(
            _split_keyword_string(str(layers.get("keywords", "") or "")),
            target.explicit_topics,
        )
        if keywords_list:
            meta["topics"] = _merge_unique_strings(meta.get("topics"), keywords_list)

        anchor_handles = _merge_unique_strings(
            meta.get("anchor_handles"),
            (layers.get("anchor_handles", []) if content and is_leaf else []),
        )
        if anchor_handles:
            meta["anchor_handles"] = anchor_handles
        keywords = ", ".join(keywords_list)

        tenant_id, user_id = get_effective_identity()
        effective_user = UserIdentifier(tenant_id, user_id)
        ctx = Context(
            uri=target.uri,
            parent_uri=target.parent_uri,
            is_leaf=is_leaf,
            abstract=abstract,
            overview=overview,
            context_type=context_type,
            category=category,
            related_uri=related_uri or [],
            meta=meta,
            session_id=session_id,
            user=effective_user,
            id=(
                str(target.existing_record.get("id", "") or "")
                if target.existing_record is not None
                else None
            ),
        )

        base_text = embed_text or abstract
        if keywords:
            ctx.vectorize = Vectorize(f"{base_text} {keywords}")
        elif embed_text:
            ctx.vectorize = Vectorize(embed_text)

        effective_category = category or orch._extract_category_from_uri(target.uri)
        abstract_json = orch._build_abstract_json(
            uri=target.uri,
            context_type=context_type or "",
            category=effective_category,
            abstract=abstract,
            overview=overview,
            content=content,
            entities=entities,
            meta=meta,
            keywords=keywords_list,
            parent_uri=target.parent_uri,
            session_id=session_id,
        )
        if content and is_leaf:
            abstract_json["fact_points"] = layers.get("fact_points", [])
        object_payload = orch._memory_object_payload(abstract_json, is_leaf=is_leaf)
        return AssembledWriteContext(
            ctx=ctx,
            abstract=abstract,
            overview=overview,
            keywords=keywords,
            keywords_list=keywords_list,
            entities=entities,
            meta=meta,
            effective_category=effective_category,
            abstract_json=abstract_json,
            object_payload=object_payload,
            merge_signature=str(object_payload["merge_signature"]),
            mergeable=bool(object_payload["mergeable"]),
        )
