# SPDX-License-Identifier: Apache-2.0
"""Shared test helpers — single sources of truth for fixture utilities.

Anything that previously appeared verbatim in two or more test modules
(``InMemoryStorage._resolve_field``, ``_FakeStorage._resolve_field``,
etc.) belongs here so that future Qdrant filter changes only need to be
mirrored once. Fixtures call into this module instead of carrying their
own copy.
"""

from __future__ import annotations

from typing import Any, Dict


def resolve_field(record: Dict[str, Any], field_name: str) -> Any:
    """Resolve a (possibly dot-path) field name into a record value.

    Mirrors Qdrant's nested-field filter semantics so in-memory test
    fixtures stay consistent with the real adapter. Bare names like
    ``session_id`` use direct ``.get``; dot-paths like
    ``meta.source_uri`` walk the nested dict step by step. Missing
    intermediate keys collapse to ``None`` so the caller's
    ``in conds`` check handles them the same way Qdrant would.
    """
    if "." not in field_name:
        return record.get(field_name)
    cursor: Any = record
    for part in field_name.split("."):
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        else:
            return None
        if cursor is None:
            return None
    return cursor
