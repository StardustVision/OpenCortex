# SPDX-License-Identifier: Apache-2.0
"""Store events and in-process event publishing."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from typing import Any, Awaitable, Callable, Dict, List, Set

from pydantic import BaseModel, Field

from opencortex.store.schemas import PrimaryRecordInput, StoredRecord
from opencortex.store.types import EventName

logger = logging.getLogger(__name__)

EventHandler = Callable[[Any], Awaitable[None] | None]


class MemoryEvent(BaseModel):
    """Base event with the event-manager routing name."""

    model_config = {"frozen": True}

    @property
    def name(self) -> str:
        """Event name used by the manager."""
        raise NotImplementedError


class TenantEvent(MemoryEvent):
    """Base event scoped to one tenant/user/project."""

    tenant_id: str
    user_id: str
    project_id: str


class SessionEvent(TenantEvent):
    """Base event scoped to one conversation session."""

    session_id: str


class MemoryStoredEvent(TenantEvent):
    """Emitted after `/memory/store` writes a primary record."""

    uri: str
    record_id: str
    context_type: str
    category: str
    session_id: str = ""
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
    collection: str = ""
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

    @property
    def name(self) -> str:
        """Event name used by the manager."""
        return str(EventName.SESSION_MERGED)


class SessionEndedEvent(SessionEvent):
    """Emitted after `/session/end` writes the final primary record."""

    final_uri: str
    merged_uris: List[str] = Field(default_factory=list)

    @property
    def name(self) -> str:
        """Event name used by the manager."""
        return str(EventName.SESSION_ENDED)


class MemoryEventManager:
    """Small in-process event manager for memory lifecycle plugins."""

    def __init__(self) -> None:
        """Create an empty event manager."""
        self._subscribers: Dict[str, List[EventHandler]] = {}
        self._tasks: Set[asyncio.Task[None]] = set()

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        """Register a handler for a named event."""
        self._subscribers.setdefault(event_name, []).append(handler)

    def publish_nowait(self, event: Any) -> None:
        """Schedule event handlers without blocking the caller."""
        handlers = list(self._subscribers.get(str(event.name), []))
        if not handlers:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "[MemoryEventManager] Dropping %s event without running loop",
                event.name,
            )
            return

        for handler in handlers:
            task = loop.create_task(
                self._dispatch(handler, event),
                name=f"opencortex.memory_event.{event.name}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        """Cancel and await pending handler tasks."""
        if not self._tasks:
            return
        tasks = list(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def _dispatch(self, handler: EventHandler, event: Any) -> None:
        """Run one handler and contain plugin failures."""
        try:
            result = handler(event)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[MemoryEventManager] Handler failed for %s: %s",
                event.name,
                exc,
            )


class StoreEvents:
    """Publish the four write events consumed by EventWorker."""

    def __init__(self, memory_events: Any) -> None:
        self._memory_events = memory_events

    def memory_stored(
        self,
        record_input: PrimaryRecordInput,
        stored: StoredRecord,
    ) -> None:
        """Publish `/memory/store` completion for a memory record."""
        self._publish_primary_record(record_input, stored)

    def resource_stored(
        self,
        record_input: PrimaryRecordInput,
        stored: StoredRecord,
    ) -> None:
        """Publish `/memory/store` completion for a resource record."""
        self._publish_primary_record(record_input, stored)

    def session_turn_stored(
        self,
        *,
        session_id: str,
        turn_id: str,
        tenant_id: str,
        user_id: str,
        project_id: str,
        record_uris: list[str],
        tool_calls: list[dict[str, Any]],
        collection: str = "",
    ) -> None:
        """Publish `/session/message` completion."""
        if self._memory_events is None:
            return
        self._memory_events.publish_nowait(
            SessionTurnStoredEvent(
                session_id=session_id,
                turn_id=turn_id,
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=project_id,
                record_uris=record_uris,
                tool_calls=tool_calls,
                collection=collection,
            )
        )

    def session_merged(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        project_id: str,
        merged_uri: str,
        source_uris: list[str],
    ) -> None:
        """Publish merge primary-record completion."""
        if self._memory_events is None:
            return
        self._memory_events.publish_nowait(
            SessionMergedEvent(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=project_id,
                merged_uri=merged_uri,
                source_uris=source_uris,
            )
        )

    def session_ended(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        project_id: str,
        final_uri: str,
        merged_uris: list[str],
    ) -> None:
        """Publish `/session/end` completion."""
        if self._memory_events is None:
            return
        self._memory_events.publish_nowait(
            SessionEndedEvent(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=project_id,
                final_uri=final_uri,
                merged_uris=merged_uris,
            )
        )

    def _publish_primary_record(
        self,
        record_input: PrimaryRecordInput,
        stored: StoredRecord,
    ) -> None:
        """Publish the `/memory/store` write event."""
        if self._memory_events is None:
            return
        self._memory_events.publish_nowait(
            MemoryStoredEvent(
                uri=stored.uri,
                record_id=str(stored.record["id"]),
                tenant_id=record_input.tenant_id,
                user_id=record_input.user_id,
                project_id=str(stored.record.get("project_id", "")),
                context_type=stored.context_type,
                category=stored.category,
                session_id=record_input.session_id,
                layer=str(record_input.meta.get("layer", "") or ""),
                content=record_input.content,
                record=dict(stored.record),
            )
        )
