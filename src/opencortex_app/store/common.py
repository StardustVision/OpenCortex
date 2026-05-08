# SPDX-License-Identifier: Apache-2.0
"""Shared store helpers that are not part of the old write path."""

from __future__ import annotations

from typing import Any


def merge_unique_strings(*groups: Any) -> list[str]:
    """Return a stable ordered union of non-empty string values."""
    merged: list[str] = []
    for group in groups:
        if not group:
            continue
        values = [group] if isinstance(group, str) else list(group)
        for value in values:
            normalized = str(value).strip()
            if normalized and normalized not in merged:
                merged.append(normalized)
    return merged


def split_keyword_string(raw_keywords: str) -> list[str]:
    """Split a comma-separated keyword string into normalized tokens."""
    if not raw_keywords:
        return []
    return [
        token.strip()
        for token in str(raw_keywords).split(",")
        if token and token.strip()
    ]


def build_abstract_json(
    *,
    uri: str,
    context_type: str,
    category: str,
    abstract: str,
    overview: str,
    content: str,
    entities: list[str],
    meta: dict[str, Any],
    keywords: list[str],
    parent_uri: str,
    session_id: str,
) -> dict[str, Any]:
    """Build the canonical abstract payload for a primary record."""
    record = {
        "uri": uri,
        "context_type": context_type,
        "category": category,
        "abstract": abstract,
        "overview": overview,
        "content": content,
        "entities": entities,
        "keywords": keywords,
        "metadata": meta,
        "parent_uri": parent_uri,
        "session_id": session_id,
    }
    result = {
        **record,
        "memory_kind": memory_kind_for_record(record),
        "anchors": anchors_from_record(record),
    }
    anchor_handles = meta.get("anchor_handles")
    if not anchor_handles:
        return result

    existing_values = {
        anchor.get("value", "").lower()
        for anchor in result.get("anchors") or []
        if isinstance(anchor, dict)
    }
    for handle in anchor_handles:
        if not isinstance(handle, str):
            continue
        normalized = handle.strip()
        if not normalized or normalized.lower() in existing_values:
            continue
        result.setdefault("anchors", []).append(
            {
                "anchor_type": "handle",
                "value": normalized,
                "text": normalized,
            }
        )
        existing_values.add(normalized.lower())
    return result


def memory_object_payload(
    abstract_json: dict[str, Any],
    *,
    is_leaf: bool,
) -> dict[str, Any]:
    """Project canonical abstract payload into flat vector metadata."""
    memory_kind = str(abstract_json.get("memory_kind", "semantic") or "semantic")
    anchor_hits = [
        str(anchor.get("value", "") or "")
        for anchor in abstract_json.get("anchors", [])
        if isinstance(anchor, dict) and str(anchor.get("value", "") or "").strip()
    ]
    return {
        "memory_kind": memory_kind,
        "anchor_hits": anchor_hits,
        "merge_signature": merge_signature_from_abstract(abstract_json),
        "mergeable": memory_kind != "episodic",
        "retrieval_surface": "l0_object" if is_leaf else "",
        "anchor_surface": bool(is_leaf and anchor_hits),
    }


def memory_kind_for_record(record: dict[str, Any]) -> str:
    """Return the memory kind for a canonical record."""
    category = str(record.get("category", "") or "").strip()
    if category in {"semantic", "episodic", "procedural"}:
        return category
    return "semantic"


def anchors_from_record(record: dict[str, Any]) -> list[dict[str, str]]:
    """Build anchor payloads from entities and keywords."""
    anchors: list[dict[str, str]] = []
    for entity in record.get("entities", []) or []:
        value = str(entity).strip()
        if value:
            anchors.append({"anchor_type": "entity", "value": value, "text": value})
    for keyword in record.get("keywords", []) or []:
        value = str(keyword).strip()
        if value:
            anchors.append({"anchor_type": "topic", "value": value, "text": value})
    return anchors


def merge_signature_from_abstract(abstract_json: dict[str, Any]) -> str:
    """Return a deterministic merge signature for object-level memory."""
    parts = [
        str(abstract_json.get("memory_kind", "")),
        str(abstract_json.get("category", "")),
        str(abstract_json.get("abstract", "")).strip().lower(),
    ]
    return "|".join(parts)


def extract_category_from_uri(uri: str) -> str:
    """Extract category from a Cortex URI path."""
    parts = uri.split("/")
    for parent in (
        "memories",
        "cases",
        "patterns",
        "skills",
        "staging",
        "resources",
    ):
        if parent not in parts:
            continue
        idx = parts.index(parent)
        if parent in ("cases", "patterns"):
            return parent
        if parent == "resources":
            cat_idx = idx + 2
            if cat_idx < len(parts):
                candidate = parts[cat_idx]
                if len(candidate) != 12:
                    return candidate
            continue
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return ""
