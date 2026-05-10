# SPDX-License-Identifier: Apache-2.0
"""Store-domain enums."""

from __future__ import annotations

from enum import Enum


class StoreTextEnum(str, Enum):
    """String enum that behaves like its stored text."""

    def __str__(self) -> str:
        """Return the stored string value."""
        return self.value


class ContextType(StoreTextEnum):
    """Supported stored context types."""

    MEMORY = "memory"
    RESOURCE = "resource"
    STAGING = "staging"


class EventName(StoreTextEnum):
    """Store and session write events accepted by EventWorker."""

    MEMORY_STORED = "memory_stored"
    CHECK_UPDATE = "check_update"
    SESSION_TURN_STORED = "session_turn_stored"
    SESSION_MERGED = "session_merged"
    SESSION_ENDED = "session_ended"


class StoreRecordType(StoreTextEnum):
    """Public store record types."""

    MEMORY = "memory"
    RESOURCE = "resource"


class StoreMemoryCategory(StoreTextEnum):
    """Public memory categories aligned with mem0."""

    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


class StoreSourceKind(StoreTextEnum):
    """Public store source kinds."""

    MANUAL = "manual"
    SESSION = "session"
    DOCUMENT = "document"
    TOOL = "tool"
    API = "api"


class StoreMetadataKey(StoreTextEnum):
    """Metadata keys used by the store API compatibility mapping."""

    SOURCE = "source"
    SOURCE_PATH = "source_path"
    SOURCE_DOC_TITLE = "source_doc_title"
    SOURCE_SECTION_PATH = "source_section_path"
    SOURCE_FORMAT = "source_format"
    CONTENT_TYPE = "content_type"
    FILE_PATH = "file_path"
    TITLE = "title"


class MemoryCategory(StoreTextEnum):
    """Primary memory categories owned by store flows."""

    EVENTS = "events"


class SessionRecordLayer(StoreTextEnum):
    """Session primary-record layers."""

    IMMEDIATE = "immediate"
    MERGED = "merged"
    FINAL = "session_final"
