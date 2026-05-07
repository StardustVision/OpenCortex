# SPDX-License-Identifier: Apache-2.0
"""Store-domain enums."""

from __future__ import annotations

from enum import Enum


class StoreTextEnum(str, Enum):
    """String enum that behaves like its stored text."""

    def __str__(self) -> str:
        """Return the stored string value."""
        return self.value


class EventName(StoreTextEnum):
    """Store and session write events accepted by EventWorker."""

    MEMORY_STORED = "memory_stored"
    SESSION_TURN_STORED = "session_turn_stored"
    SESSION_MERGED = "session_merged"
    SESSION_ENDED = "session_ended"


class MemoryCategory(StoreTextEnum):
    """Primary memory categories owned by store flows."""

    EVENTS = "events"


class SessionRecordLayer(StoreTextEnum):
    """Session primary-record layers."""

    IMMEDIATE = "immediate"
    MERGED = "merged"
    FINAL = "session_final"
