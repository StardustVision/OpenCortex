# SPDX-License-Identifier: Apache-2.0
"""In-process store event worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from opencortex.store.events import (
    MemoryEvent,
    MemoryEventManager,
    MemoryStoredEvent,
    SessionEndedEvent,
    SessionMergedEvent,
    SessionTurnStoredEvent,
)
from opencortex.store.types import EventName

logger = logging.getLogger(__name__)


class EventWorker:
    """Receive store events and route them to async worker queues."""

    def __init__(
        self,
        *,
        memory_events: MemoryEventManager,
    ) -> None:
        self._memory_events = memory_events
        self._queue: asyncio.Queue[MemoryEvent | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    def subscribe(self) -> None:
        """Subscribe this worker to the four store write events."""
        self._memory_events.subscribe(str(EventName.MEMORY_STORED), self.enqueue)
        self._memory_events.subscribe(str(EventName.SESSION_TURN_STORED), self.enqueue)
        self._memory_events.subscribe(str(EventName.SESSION_MERGED), self.enqueue)
        self._memory_events.subscribe(str(EventName.SESSION_ENDED), self.enqueue)

    def enqueue(self, event: Any) -> None:
        """Enqueue supported store events without blocking the publisher."""
        if not isinstance(
            event,
            (
                MemoryStoredEvent,
                SessionTurnStoredEvent,
                SessionMergedEvent,
                SessionEndedEvent,
            ),
        ):
            return
        self._queue.put_nowait(event)

    async def start(self) -> None:
        """Start the worker loop."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run(),
            name="opencortex.store.event_worker",
        )

    async def close(self) -> None:
        """Stop the worker loop and await shutdown."""
        task = self._task
        if task is None:
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(task, timeout=30.0)
        except asyncio.TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None

    async def _run(self) -> None:
        """Run the queue consumer loop."""
        while True:
            event = await self._queue.get()
            try:
                if event is None:
                    return
                await self._handle(event)
            finally:
                self._queue.task_done()

    async def _handle(self, event: MemoryEvent) -> None:
        """Route one event to its future async side-effect handler."""
        try:
            if isinstance(event, MemoryStoredEvent):
                await self._handle_memory_stored(event)
            elif isinstance(event, SessionTurnStoredEvent):
                await self._handle_session_turn_stored(event)
            elif isinstance(event, SessionMergedEvent):
                await self._handle_session_merged(event)
            elif isinstance(event, SessionEndedEvent):
                await self._handle_session_ended(event)
        except Exception as exc:
            logger.warning(
                "[EventWorker] event handling failed event=%s: %s",
                event.name,
                exc,
            )

    async def _handle_memory_stored(self, event: MemoryStoredEvent) -> None:
        """Handle `/memory/store` side effects."""
        _ = event

    async def _handle_session_turn_stored(
        self,
        event: SessionTurnStoredEvent,
    ) -> None:
        """Handle `/session/message` side effects."""
        _ = event

    async def _handle_session_merged(self, event: SessionMergedEvent) -> None:
        """Handle merge side effects."""
        _ = event

    async def _handle_session_ended(self, event: SessionEndedEvent) -> None:
        """Handle `/session/end` side effects."""
        _ = event
