# SPDX-License-Identifier: Apache-2.0
"""Mappers from current unified-store records into shared memory-domain views."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any, Dict, Iterable, List

from pydantic import Field

from opencortex.memory.domain import (
    AnchorEntry,
    MemoryDomainModel,
    MemoryAbstract,
    MemoryEntry,
    MemoryKind,
    MemoryLineage,
    MemoryQuality,
    MemorySource,
    StructuredSlots,
    memory_kind_policy,
)

_TIME_TOKEN_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{4}|\d{1,2}:\d{2}|yesterday|today|tomorrow|last|next)\b",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")
_HANDLE_SPLIT_RE = re.compile(r"[,;\n|]+")
_QUOTED_HANDLE_RE = re.compile(r"[\"'`“”‘’]([^\"'`“”‘’]{2,40})[\"'`“”‘’]")
_PATHISH_HANDLE_RE = re.compile(r"\b[a-zA-Z0-9]+(?:[._/-][a-zA-Z0-9]+)+\b")
_CAMEL_CASE_HANDLE_RE = re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b")
_ALL_CAPS_HANDLE_RE = re.compile(r"\b[A-Z]{2,}(?:[._-][A-Z0-9]+)*\b")
_MULTIWORD_LATIN_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9]+(?:\s+[A-Za-z0-9][A-Za-z0-9._/-]+)+\b"
)
_GENERIC_CATEGORY_TOPICS = {
    "event",
    "events",
    "preference",
    "preferences",
    "constraint",
    "constraints",
    "profile",
    "summary",
    "summaries",
    "document",
    "document_chunk",
}
_GENERIC_SLOT_VALUES = _GENERIC_CATEGORY_TOPICS | {
    "memory",
    "resource",
}
_GENERIC_HANDLE_VALUES = _GENERIC_SLOT_VALUES | {
    "document",
    "documents",
    "chunk",
    "chunks",
    "section",
    "sections",
    "message",
    "messages",
    "note",
    "notes",
    "content",
    "details",
    "detail",
    "record",
    "records",
    "资料",
    "文档",
    "章节",
    "部分",
    "消息",
    "记录",
    "内容",
    "详情",
}
_MAX_DISTILLED_ANCHORS = 6
_MAX_HANDLE_LENGTH = 80


class MemoryRetrievalHints(MemoryDomainModel):
    """Execution hints derived from target memory kinds."""

    context_types: List[str] = Field(default_factory=list)
    categories: List[str] = Field(default_factory=list)


def infer_memory_kind(*, category: str = "", context_type: str = "", uri: str = "") -> MemoryKind:
    """Infer the primary memory kind from current record signals."""
    normalized_category = (category or "").strip().lower()
    normalized_context_type = (context_type or "").strip().lower()
    normalized_uri = (uri or "").strip().lower()

    if "summary" in normalized_category or "/summary" in normalized_uri:
        return MemoryKind.SUMMARY
    if normalized_category in {"profile"} or "/profile" in normalized_uri:
        return MemoryKind.PROFILE
    if normalized_category in {"preference", "preferences"} or "/preferences" in normalized_uri:
        return MemoryKind.PREFERENCE
    if normalized_category in {"constraint", "constraints"}:
        return MemoryKind.CONSTRAINT
    if normalized_category in {"relation", "relations", "entity", "entities"}:
        return MemoryKind.RELATION
    if normalized_category in {"event", "events"}:
        return MemoryKind.EVENT
    if normalized_category in {"document", "document_chunk"}:
        return MemoryKind.DOCUMENT_CHUNK
    if normalized_context_type in {"resource", "case", "pattern"}:
        return MemoryKind.DOCUMENT_CHUNK
    if normalized_context_type == "memory":
        return MemoryKind.EVENT
    return MemoryKind.SUMMARY


def retrieval_hints_for_kinds(memory_kinds: Iterable[MemoryKind]) -> MemoryRetrievalHints:
    """Project normalized memory kinds into current-store execution hints."""
    context_types: List[str] = []
    categories: List[str] = []
    for memory_kind in memory_kinds:
        policy = memory_kind_policy(memory_kind)
        for context_type in policy.retrieval_context_types:
            if context_type not in context_types:
                context_types.append(context_type)
        for category in policy.retrieval_categories:
            if category not in categories:
                categories.append(category)
    return MemoryRetrievalHints(context_types=context_types, categories=categories)


def memory_anchor_hits_from_abstract(abstract_payload: Mapping[str, Any]) -> List[str]:
    """Project stable flat anchor strings from an abstract payload."""
    hits: List[str] = []
    for anchor in abstract_payload.get("anchors") or []:
        if not isinstance(anchor, Mapping):
            continue
        value = str(anchor.get("text") or anchor.get("value") or "").strip()
        if value and value not in hits:
            hits.append(value)
    return hits


def memory_merge_signature_from_abstract(
    abstract_payload: Mapping[str, Any],
) -> str:
    """Build a stable merge signature for mergeable memory kinds."""
    entry = _memory_entry_from_abstract_payload(
        abstract_payload,
        {
            "uri": abstract_payload.get("uri", ""),
            "context_type": abstract_payload.get("context_type", ""),
            "category": abstract_payload.get("category", ""),
            "abstract": abstract_payload.get("summary", ""),
        },
    )
    if not entry.policy.mergeable:
        return ""

    parts = [entry.memory_kind.value]
    if entry.memory_kind == MemoryKind.PROFILE:
        parts.extend(_signature_values(entry.structured_slots.entities))
    elif entry.memory_kind == MemoryKind.PREFERENCE:
        parts.extend(_signature_values(entry.structured_slots.entities))
        parts.extend(_signature_values(entry.structured_slots.preferences))
    elif entry.memory_kind == MemoryKind.CONSTRAINT:
        parts.extend(_signature_values(entry.structured_slots.entities))
        parts.extend(_signature_values(entry.structured_slots.constraints))

    if len(parts) == 1:
        summary_value = _normalize_signature_value(
            str(abstract_payload.get("summary", ""))
        )
        if summary_value:
            parts.append(summary_value)

    unique_parts: List[str] = []
    for part in parts:
        if part and part not in unique_parts:
            unique_parts.append(part)
    return "|".join(unique_parts)


def memory_object_view_from_record(record: Mapping[str, Any]) -> MemoryEntry:
    """Build a normalized memory entry from a dict-like record."""
    abstract_payload = record.get("abstract_json")
    if isinstance(abstract_payload, Mapping):
        return _memory_entry_from_abstract_payload(abstract_payload, record)

    category = _string_value(record, "category")
    context_type = _string_value(record, "context_type")
    uri = _string_value(record, "uri")
    memory_kind = infer_memory_kind(
        category=category,
        context_type=context_type,
        uri=uri,
    )
    metadata = _metadata_map(record)
    slots = StructuredSlots(
        entities=_unique_strings(record.get("entities") or metadata.get("entities")),
        time_refs=_extract_time_refs(record, metadata),
        topics=_extract_topics(record, metadata),
        preferences=_extract_kind_values(memory_kind, record, metadata, "preferences"),
        constraints=_extract_kind_values(memory_kind, record, metadata, "constraints"),
        relations=_extract_relation_values(record, metadata),
        document_refs=_extract_document_refs(memory_kind, record, metadata),
        summary_refs=_extract_summary_refs(memory_kind, record, metadata),
    )
    anchor_entries = _anchor_entries_from_slots(slots)
    lineage = _lineage_from_record(record, metadata)
    source = _source_from_record(record, metadata)
    quality = MemoryQuality(
        anchor_count=len(anchor_entries),
        entity_count=len(slots.entities),
        keyword_count=len(slots.topics),
    )
    return MemoryEntry(
        uri=uri,
        memory_kind=memory_kind,
        structured_slots=slots,
        policy=memory_kind_policy(memory_kind),
        context_type=context_type,
        category=category,
        abstract=_string_value(record, "abstract"),
        overview=_optional_string_value(record, "overview"),
        content=_optional_string_value(record, "content"),
        anchor_entries=anchor_entries,
        lineage=lineage,
        source=source,
        quality=quality,
        metadata=metadata,
    )


def memory_object_view_from_match(match: Any) -> MemoryEntry:
    """Build a normalized memory entry from a matched-context-like object."""
    record = {
        "uri": getattr(match, "uri", ""),
        "category": getattr(match, "category", ""),
        "context_type": _context_type_value(getattr(match, "context_type", "")),
        "abstract": getattr(match, "abstract", ""),
        "overview": getattr(match, "overview", None),
        "content": getattr(match, "content", None),
        "metadata": getattr(match, "metadata", {}) if hasattr(match, "metadata") else {},
    }
    return memory_object_view_from_record(record)


def memory_abstract_from_record(record: Mapping[str, Any]) -> MemoryAbstract:
    """Project a record into the fixed shared `.abstract.json` schema."""
    entry = memory_object_view_from_record(record)
    return MemoryAbstract(
        uri=entry.uri,
        memory_kind=entry.memory_kind,
        context_type=entry.context_type,
        category=entry.category,
        summary=entry.abstract,
        anchors=entry.anchor_entries,
        slots=entry.structured_slots,
        lineage=entry.lineage,
        source=entry.source,
        quality=entry.quality,
    )


def _memory_entry_from_abstract_payload(
    abstract_payload: Mapping[str, Any],
    record: Mapping[str, Any],
) -> MemoryEntry:
    memory_kind = MemoryKind(str(abstract_payload.get("memory_kind", MemoryKind.SUMMARY.value)))
    slots_payload = abstract_payload.get("slots") or {}
    lineage_payload = abstract_payload.get("lineage") or {}
    source_payload = abstract_payload.get("source") or {}
    quality_payload = abstract_payload.get("quality") or {}
    anchors_payload = abstract_payload.get("anchors") or []

    anchor_entries = [
        AnchorEntry(
            anchor_type=str(anchor.get("anchor_type", "")),
            value=str(anchor.get("value", "")),
            text=str(anchor.get("text", anchor.get("value", ""))),
        )
        for anchor in anchors_payload
        if isinstance(anchor, Mapping)
    ]

    return MemoryEntry(
        uri=_string_value(record, "uri") or str(abstract_payload.get("uri", "")),
        memory_kind=memory_kind,
        structured_slots=StructuredSlots(**dict(slots_payload)),
        policy=memory_kind_policy(memory_kind),
        context_type=_string_value(record, "context_type") or str(abstract_payload.get("context_type", "")),
        category=_string_value(record, "category") or str(abstract_payload.get("category", "")),
        abstract=_string_value(record, "abstract") or str(abstract_payload.get("summary", "")),
        overview=_optional_string_value(record, "overview"),
        content=_optional_string_value(record, "content"),
        anchor_entries=anchor_entries,
        lineage=MemoryLineage(**dict(lineage_payload)),
        source=MemorySource(**dict(source_payload)),
        quality=MemoryQuality(**dict(quality_payload)),
        metadata=_metadata_map(record),
    )


def _context_type_value(raw_value: Any) -> str:
    if hasattr(raw_value, "value"):
        return str(raw_value.value)
    return str(raw_value or "")


def _string_value(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    if value is None:
        return ""
    return str(value)


def _optional_string_value(record: Mapping[str, Any], key: str) -> str | None:
    value = record.get(key)
    if value in (None, ""):
        return None
    return str(value)


def _metadata_map(record: Mapping[str, Any]) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    raw_metadata = record.get("metadata")
    if isinstance(raw_metadata, Mapping):
        metadata.update(raw_metadata)
    raw_meta = record.get("meta")
    if isinstance(raw_meta, Mapping):
        metadata.update(raw_meta)
    return metadata


def _unique_strings(raw_values: Any) -> List[str]:
    if not raw_values:
        return []
    if isinstance(raw_values, str):
        raw_iterable = [raw_values]
    else:
        raw_iterable = list(raw_values)

    values: List[str] = []
    for raw_value in raw_iterable:
        if raw_value is None:
            continue
        normalized = str(raw_value).strip()
        if normalized and normalized not in values:
            values.append(normalized)
    return values


def _extract_time_refs(record: Mapping[str, Any], metadata: Mapping[str, Any]) -> List[str]:
    raw_time_refs = metadata.get("time_refs")
    explicit_values: List[Any] = []
    if isinstance(raw_time_refs, str):
        explicit_values.append(raw_time_refs)
    elif raw_time_refs:
        explicit_values.extend(list(raw_time_refs))
    explicit_values.extend(
        [
            metadata.get("event_date"),
            metadata.get("timestamp"),
            record.get("event_date"),
        ]
    )
    explicit_values = [v for v in explicit_values if v is not None]
    explicit = _unique_strings(explicit_values)
    if explicit:
        return explicit
    content = " ".join(
        part
        for part in (
            _string_value(record, "abstract"),
            _string_value(record, "overview"),
            _string_value(record, "content"),
        )
        if part
    )
    found = _TIME_TOKEN_RE.findall(content)
    return _unique_strings(found)


def _extract_topics(record: Mapping[str, Any], metadata: Mapping[str, Any]) -> List[str]:
    topics: List[str] = []
    trusted_candidates: List[str] = []
    trusted_candidates.extend(_unique_strings(metadata.get("topics")))
    trusted_candidates.extend(_keyword_values(metadata.get("keywords")))
    trusted_candidates.extend(_keyword_values(record.get("keywords")))
    trusted_candidates.extend(_unique_strings(metadata.get("anchor_handles")))

    for candidate in trusted_candidates:
        _append_distilled_handle(topics, candidate, trusted=True)
        if len(topics) >= _MAX_DISTILLED_ANCHORS:
            return topics
    if topics:
        return topics

    raw_text = " ".join(
        part
        for part in (
            _string_value(record, "abstract"),
            _string_value(record, "overview"),
            _string_value(record, "content"),
        )
        if part
    )
    for candidate in _text_anchor_candidates(raw_text):
        _append_distilled_handle(topics, candidate, trusted=False)
        if len(topics) >= _MAX_DISTILLED_ANCHORS:
            return topics
    if topics:
        return topics
    category = _string_value(record, "category")
    if category and category.lower() not in _GENERIC_CATEGORY_TOPICS:
        return [category]
    return []


def _extract_kind_values(
    memory_kind: MemoryKind,
    record: Mapping[str, Any],
    metadata: Mapping[str, Any],
    key: str,
) -> List[str]:
    values = _unique_strings(metadata.get(key))
    if values:
        return values

    if memory_kind == MemoryKind.PREFERENCE and key == "preferences":
        summary = _string_value(record, "abstract") or _string_value(record, "overview")
        return [summary] if summary else []
    if memory_kind == MemoryKind.CONSTRAINT and key == "constraints":
        summary = _string_value(record, "abstract") or _string_value(record, "overview")
        return [summary] if summary else []
    return []


def _extract_relation_values(record: Mapping[str, Any], metadata: Mapping[str, Any]) -> List[str]:
    values = _unique_strings(metadata.get("relations"))
    if values:
        return values
    raw_relations = record.get("relations")
    if not raw_relations:
        return []

    relation_values: List[str] = []
    for raw_relation in raw_relations:
        if isinstance(raw_relation, Mapping):
            candidate = raw_relation.get("uri") or raw_relation.get("abstract")
        else:
            candidate = raw_relation
        normalized = str(candidate).strip()
        if normalized and normalized not in relation_values:
            relation_values.append(normalized)
    return relation_values


def _extract_document_refs(
    memory_kind: MemoryKind,
    record: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> List[str]:
    values = _unique_strings(metadata.get("document_refs"))
    if values:
        return values
    if memory_kind == MemoryKind.DOCUMENT_CHUNK:
        uri = _string_value(record, "uri")
        return [uri] if uri else []
    return []


def _extract_summary_refs(
    memory_kind: MemoryKind,
    record: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> List[str]:
    values = _unique_strings(metadata.get("summary_refs"))
    if values:
        return values
    if memory_kind == MemoryKind.SUMMARY:
        uri = _string_value(record, "uri")
        return [uri] if uri else []
    return []


def _anchor_entries_from_slots(slots: StructuredSlots) -> List[AnchorEntry]:
    anchors: List[AnchorEntry] = []
    seen: set[tuple[str, str]] = set()
    for anchor_type, values in (
        ("entity", slots.entities),
        ("time", slots.time_refs),
        ("topic", slots.topics),
        ("preference", slots.preferences),
        ("constraint", slots.constraints),
        ("relation", slots.relations),
    ):
        for value in values:
            normalized = _normalize_anchor_handle(value)
            if not normalized:
                continue
            if anchor_type not in {"entity", "time"}:
                if _is_paragraph_style_handle(normalized):
                    continue
                if normalized.lower() in _GENERIC_HANDLE_VALUES:
                    continue
            dedupe_key = (anchor_type, normalized.lower())
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            anchors.append(
                AnchorEntry(anchor_type=anchor_type, value=normalized, text=normalized)
            )
            if len(anchors) >= _MAX_DISTILLED_ANCHORS:
                return anchors
    return anchors


def _lineage_from_record(
    record: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> MemoryLineage:
    raw_section_path = metadata.get("section_path")
    if isinstance(raw_section_path, str):
        section_path = [raw_section_path] if raw_section_path else []
    else:
        section_path = _unique_strings(raw_section_path)

    raw_chunk_index = metadata.get("chunk_index")
    chunk_index = raw_chunk_index if isinstance(raw_chunk_index, int) else None

    return MemoryLineage(
        parent_uri=_string_value(record, "parent_uri") or _string_value(metadata, "parent_uri"),
        session_id=_string_value(record, "session_id") or _string_value(metadata, "session_id"),
        source_doc_id=_string_value(metadata, "source_doc_id"),
        source_doc_title=_string_value(metadata, "source_doc_title"),
        section_path=section_path,
        chunk_index=chunk_index,
    )


def _source_from_record(
    record: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> MemorySource:
    return MemorySource(
        context_type=_string_value(record, "context_type"),
        category=_string_value(record, "category"),
        source_path=_string_value(metadata, "source_path"),
    )


def _signature_values(values: List[str]) -> List[str]:
    normalized: List[str] = []
    for value in values:
        candidate = _normalize_signature_value(value)
        if candidate and candidate not in normalized:
            normalized.append(candidate)
    return normalized[:3]


def _normalize_signature_value(value: str) -> str:
    normalized = _WHITESPACE_RE.sub(" ", str(value or "").strip().lower())
    if not normalized or normalized in _GENERIC_SLOT_VALUES:
        return ""
    return normalized[:120]


def _keyword_values(raw_value: Any) -> List[str]:
    if not raw_value:
        return []
    if isinstance(raw_value, str):
        return _unique_strings(_HANDLE_SPLIT_RE.split(raw_value))
    return _unique_strings(raw_value)


def _normalize_anchor_handle(value: Any) -> str:
    normalized = _WHITESPACE_RE.sub(" ", str(value or "").strip())
    if not normalized:
        return ""
    normalized = normalized.strip(" \t\r\n,;:!?[](){}<>\"'`")
    if not normalized:
        return ""
    return normalized[:_MAX_HANDLE_LENGTH]


def _is_paragraph_style_handle(value: str) -> bool:
    if "\n" in value:
        return True
    if len(value) > _MAX_HANDLE_LENGTH:
        return True
    if value.count("。") > 1 or value.count(". ") > 1:
        return True
    return False


def _has_concrete_handle_signal(value: str) -> bool:
    if _TIME_TOKEN_RE.search(value):
        return True
    if any(char.isdigit() for char in value):
        return True
    if _PATHISH_HANDLE_RE.search(value):
        return True
    if _CAMEL_CASE_HANDLE_RE.search(value):
        return True
    if _ALL_CAPS_HANDLE_RE.search(value):
        return True
    if _MULTIWORD_LATIN_RE.search(value):
        return True
    return False


def _append_distilled_handle(
    handles: List[str],
    candidate: Any,
    *,
    trusted: bool,
) -> None:
    normalized = _normalize_anchor_handle(candidate)
    if not normalized:
        return
    lowered = normalized.lower()
    if lowered in _GENERIC_HANDLE_VALUES:
        return
    if _is_paragraph_style_handle(normalized):
        return
    if not trusted and not _has_concrete_handle_signal(normalized):
        return
    if lowered in {handle.lower() for handle in handles}:
        return
    handles.append(normalized)


def _text_anchor_candidates(text: str) -> List[str]:
    if not text:
        return []
    candidates: List[str] = []
    for match in _QUOTED_HANDLE_RE.findall(text):
        _append_distilled_handle(candidates, match, trusted=False)
    for regex in (
        _PATHISH_HANDLE_RE,
        _CAMEL_CASE_HANDLE_RE,
        _ALL_CAPS_HANDLE_RE,
        _MULTIWORD_LATIN_RE,
    ):
        for match in regex.findall(text):
            _append_distilled_handle(candidates, match, trusted=False)
            if len(candidates) >= _MAX_DISTILLED_ANCHORS:
                return candidates
    return candidates
