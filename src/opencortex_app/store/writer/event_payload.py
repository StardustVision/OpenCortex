# SPDX-License-Identifier: Apache-2.0
"""Helpers for projecting store events into writer inputs."""

from __future__ import annotations

from hashlib import sha1
from typing import Any

from opencortex_app.store.event.events import (
    MemoryEvent,
    MemoryStoredEvent,
    SessionEndedEvent,
    SessionMergedEvent,
)


def primary_record(event: MemoryEvent) -> dict[str, Any]:
    """Return the primary record carried by a write event."""
    if isinstance(event, (MemoryStoredEvent, SessionMergedEvent, SessionEndedEvent)):
        return dict(event.record or {})
    return {}


def event_uri(event: MemoryEvent) -> str:
    """Return the primary record URI carried by an event."""
    if isinstance(event, MemoryStoredEvent):
        return event.uri
    if isinstance(event, SessionMergedEvent):
        return event.merged_uri
    if isinstance(event, SessionEndedEvent):
        return event.final_uri
    return ""


def event_record_id(event: MemoryEvent) -> str:
    """Return the primary record ID carried by an event."""
    record = primary_record(event)
    if record.get("id"):
        return str(record["id"])
    if isinstance(event, MemoryStoredEvent):
        return event.record_id
    return ""


def event_content(event: MemoryEvent) -> str:
    """Return the primary text carried by an event."""
    if isinstance(event, (MemoryStoredEvent, SessionMergedEvent, SessionEndedEvent)):
        return str(event.content or "")
    return ""


def record_abstract_json(record: dict[str, Any]) -> dict[str, Any]:
    """Return the abstract-json payload for a primary record."""
    return dict(record.get("abstract_json") or {})


def digest(text: str) -> str:
    """Return a stable short digest for index URIs."""
    return sha1(text.strip().lower().encode("utf-8")).hexdigest()[:16]
