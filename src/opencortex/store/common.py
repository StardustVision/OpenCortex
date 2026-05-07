# SPDX-License-Identifier: Apache-2.0
"""Shared store helpers that are not part of the old write path."""

from __future__ import annotations

from typing import Any

from opencortex.memory import (
    MemoryKind,
    memory_abstract_from_record,
    memory_anchor_hits_from_abstract,
    memory_kind_policy,
    memory_merge_signature_from_abstract,
)


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
    result = memory_abstract_from_record(record).to_dict()
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
    memory_kind = MemoryKind(str(abstract_json["memory_kind"]))
    policy = memory_kind_policy(memory_kind)
    anchor_hits = memory_anchor_hits_from_abstract(abstract_json)
    return {
        "memory_kind": memory_kind.value,
        "anchor_hits": anchor_hits,
        "merge_signature": memory_merge_signature_from_abstract(abstract_json),
        "mergeable": policy.mergeable,
        "retrieval_surface": "l0_object" if is_leaf else "",
        "anchor_surface": bool(is_leaf and anchor_hits),
    }


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

