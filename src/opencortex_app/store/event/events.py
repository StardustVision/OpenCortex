# SPDX-License-Identifier: Apache-2.0
"""Store events and in-process event publishing."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from typing import Any, Awaitable, Callable, Dict, List, Set

import structlog
from pydantic import BaseModel, Field, model_validator

from opencortex_app.core.identity import IdentityProfile
from opencortex_app.store.schemas import PrimaryRecordInput, StoredRecord
from opencortex_app.store.types import EventName

logger = structlog.get_logger(__name__)

EventHandler = Callable[[Any], Awaitable[None] | None]


class MemoryEvent(BaseModel):
    """Base event with the event-manager routing name."""

    @property
    def name(self) -> str:
        """Event name used by the manager."""
        raise NotImplementedError


class ProfileEvent(MemoryEvent):
    """Base event carrying identity and routing context."""

    profile: IdentityProfile | None = None

    @model_validator(mode="before")
    @classmethod
    def build_profile(cls, data: Any) -> Any:
        """Build a profile from legacy id fields when needed."""
        if not isinstance(data, dict) or data.get("profile") is not None:
            return data
        data = dict(data)
        data["profile"] = IdentityProfile(
            tenant_id=str(data.get("tenant_id", "") or "default"),
            user_id=str(data.get("user_id", "") or "default"),
            project_id=str(data.get("project_id", "") or "public"),
            session_id=str(data.get("session_id", "") or ""),
            collection=str(data.get("collection", "") or ""),
        )
        return data

    @property
    def tenant_id(self) -> str:
        """Return the tenant id for compatibility with existing handlers."""
        return self.profile.tenant_id if self.profile else "default"

    @property
    def user_id(self) -> str:
        """Return the user id for compatibility with existing handlers."""
        return self.profile.user_id if self.profile else "default"

    @property
    def project_id(self) -> str:
        """Return the project id for compatibility with existing handlers."""
        return self.profile.project_id if self.profile else "public"

    @property
    def session_id(self) -> str:
        """Return the session id for compatibility with existing handlers."""
        return self.profile.session_id if self.profile else ""

    @property
    def collection(self) -> str:
        """Return the collection for compatibility with existing handlers."""
        return self.profile.collection if self.profile else ""


class SessionEvent(ProfileEvent):
    """Base event scoped to one conversation session."""

    pass


class MemoryStoredEvent(ProfileEvent):
    """Emitted after `/memory/store` writes a primary record."""

    uri: str
    record_id: str
    context_type: str
    category: str
    layer: str = ""
    content: str = ""
    record: Dict[str, Any] = Field(default_factory=dict)

    @property
    def name(self) -> str:
        """Event name used by the manager."""
        return str(EventName.MEMORY_STORED)


class SessionTurnStoredEvent(SessionEvent):
    """Emitted after `/session/message` stores one turn."""

    turn_id: str
    record_uris: List[str] = Field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)

    @property
    def name(self) -> str:
        """Event name used by the manager."""
        return str(EventName.SESSION_TURN_STORED)


class SessionMergedEvent(SessionEvent):
    """Emitted after merge writes one merged primary record."""

    merged_uri: str
    source_uris: List[str] = Field(default_factory=list)
    content: str = ""
    record: Dict[str, Any] = Field(default_factory=dict)

    @property
    def name(self) -> str:
        """Event name used by the manager."""
        return str(EventName.SESSION_MERGED)


class SessionEndedEvent(SessionEvent):
    """Emitted after `/session/end` writes the final primary record."""

    final_uri: str
    merged_uris: List[str] = Field(default_factory=list)
    content: str = ""
    record: Dict[str, Any] = Field(default_factory=dict)

    @property
    def name(self) -> str:
        """Event name used by the manager."""
        return str(EventName.SESSION_ENDED)


class MemoryEventManager:
    """Small in-process event manager for memory lifecycle plugins."""

    def __init__(self) -> None:
        """Create an empty event manager."""
        self.subscribers: Dict[str, List[EventHandler]] = {}
        self.tasks: Set[asyncio.Task[None]] = set()

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        """Register a handler for a named event."""
        self.subscribers.setdefault(event_name, []).append(handler)

    def publish_nowait(self, event: Any) -> None:
        """Schedule event handlers without blocking the caller."""
        handlers = list(self.subscribers.get(str(event.name), []))
        if not handlers:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "memory_event_dropped_without_running_loop",
                event_name=event.name,
            )
            return

        for handler in handlers:
            task = loop.create_task(
                self.dispatch(handler, event),
                name=f"opencortex_app.memory_event.{event.name}",
            )
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def close(self) -> None:
        """Cancel and await pending handler tasks."""
        if not self.tasks:
            return
        tasks = list(self.tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.tasks.clear()

    async def dispatch(self, handler: EventHandler, event: Any) -> None:
        """Run one handler and contain plugin failures."""
        try:
            result = handler(event)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "memory_event_handler_failed",
                event_name=event.name,
                error=str(exc),
            )


class StoreEvents:
    """Publish the four write events consumed by EventWorker."""

    def __init__(self, memory_events: Any) -> None:
        self.memory_events = memory_events

    def memory_stored(
        self,
        record_input: PrimaryRecordInput,
        stored: StoredRecord,
    ) -> None:
        """Publish `/memory/store` completion for a memory record."""
        self.publish_primary_record(record_input, stored)

    def resource_stored(
        self,
        record_input: PrimaryRecordInput,
        stored: StoredRecord,
    ) -> None:
        """Publish `/memory/store` completion for a resource record."""
        self.publish_primary_record(record_input, stored)

    def session_turn_stored(
        self,
        *,
        profile: IdentityProfile,
        turn_id: str,
        record_uris: list[str],
        tool_calls: list[dict[str, Any]],
    ) -> None:
        """Publish `/session/message` completion."""
        if self.memory_events is None:
            return
        self.memory_events.publish_nowait(
            SessionTurnStoredEvent(
                profile=profile,
                turn_id=turn_id,
                record_uris=record_uris,
                tool_calls=tool_calls,
            )
        )

    def session_merged(
        self,
        *,
        profile: IdentityProfile,
        merged_uri: str,
        source_uris: list[str],
        content: str = "",
        record: dict[str, Any] | None = None,
    ) -> None:
        """Publish merge primary-record completion."""
        if self.memory_events is None:
            return
        self.memory_events.publish_nowait(
            SessionMergedEvent(
                profile=profile,
                merged_uri=merged_uri,
                source_uris=source_uris,
                content=content,
                record=dict(record or {}),
            )
        )

    def session_ended(
        self,
        *,
        profile: IdentityProfile,
        final_uri: str,
        merged_uris: list[str],
        content: str = "",
        record: dict[str, Any] | None = None,
    ) -> None:
        """Publish `/session/end` completion."""
        if self.memory_events is None:
            return
        self.memory_events.publish_nowait(
            SessionEndedEvent(
                profile=profile,
                final_uri=final_uri,
                merged_uris=merged_uris,
                content=content,
                record=dict(record or {}),
            )
        )

    def publish_primary_record(
        self,
        record_input: PrimaryRecordInput,
        stored: StoredRecord,
    ) -> None:
        """Publish the `/memory/store` write event."""
        if self.memory_events is None:
            return
        self.memory_events.publish_nowait(
            MemoryStoredEvent(
                profile=IdentityProfile(
                    tenant_id=record_input.tenant_id,
                    user_id=record_input.user_id,
                    project_id=str(stored.record.get("project_id", "") or "public"),
                    session_id=record_input.session_id,
                ),
                uri=stored.uri,
                record_id=str(stored.record["id"]),
                context_type=stored.context_type,
                category=stored.category,
                layer=str(record_input.meta.get("layer", "") or ""),
                content=record_input.content,
                record=dict(stored.record),
            )
        )
