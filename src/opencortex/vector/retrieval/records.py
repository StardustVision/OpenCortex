# SPDX-License-Identifier: Apache-2.0
"""Helpers for interpreting retrieval vector records."""

from __future__ import annotations

from typing import Any

from opencortex.vector.retrieval.schemas import RetrievalSurface


def source_uri(record: dict[str, Any]) -> str:
    """Return the primary URI represented by a retrieval record."""
    if record.get("retrieval_surface") == RetrievalSurface.L0_OBJECT.value:
        return str(record.get("uri") or "")
    if record.get("retrieval_surface") == RetrievalSurface.REASON_TREE_INDEX.value:
        meta = dict(record.get("meta") or {})
        return str(record.get("source_uri") or meta.get("source_uri") or "")
    meta = dict(record.get("meta") or {})
    return str(
        record.get("source_uri")
        or meta.get("source_uri")
        or record.get("parent_uri")
        or record.get("uri")
        or ""
    )


def record_score(record: dict[str, Any]) -> float:
    """Return the best available score on a vector record."""
    for key in ("_score", "score", "index_score"):
        value = record.get(key)
        if value is not None:
            return float(value)
    return 0.0
