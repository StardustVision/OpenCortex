# SPDX-License-Identifier: Apache-2.0
"""In-process store event worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from opencortex.store.event.actions import EventAction
from opencortex.store.event.events import (
    MemoryEvent,
    MemoryEventManager,
    MemoryStoredEvent,
    SessionEndedEvent,
    SessionMergedEvent,
    SessionTurnStoredEvent,
)
from opencortex.store.types import EventName

logger = logging.getLogger(__name__)

StoreWriteEvent = (
    MemoryStoredEvent | SessionTurnStoredEvent | SessionMergedEvent | SessionEndedEvent
)


class EventWorker:
    """Receive store events and run matching actions."""

    def __init__(
        self,
        *,
        memory_events: MemoryEventManager,
        actions: list[EventAction[Any]] | None = None,
    ) -> None:
        self.memory_events = memory_events
        self.actions = list(actions or [])
        self.queue: asyncio.Queue[StoreWriteEvent | None] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    def subscribe(self) -> None:
        """Subscribe this worker to the four store write events."""
        self.memory_events.subscribe(str(EventName.MEMORY_STORED), self.enqueue)
        self.memory_events.subscribe(str(EventName.SESSION_TURN_STORED), self.enqueue)
        self.memory_events.subscribe(str(EventName.SESSION_MERGED), self.enqueue)
        self.memory_events.subscribe(str(EventName.SESSION_ENDED), self.enqueue)

    def enqueue(self, event: Any) -> None:
        """Enqueue supported store events without blocking the publisher."""
        if self.supports(event):
            self.queue.put_nowait(event)

    async def start(self) -> None:
        """Start the worker loop."""
        if self.task is not None and not self.task.done():
            return
        self.task = asyncio.create_task(
            self.run(),
            name="opencortex.store.event.worker",
        )

    async def close(self) -> None:
        """Stop the worker loop and await shutdown."""
        task = self.task
        if task is None:
            return
        await self.queue.put(None)
        try:
            await asyncio.wait_for(task, timeout=30.0)
        except asyncio.TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.task = None

    async def run(self) -> None:
        """Run the queue consumer loop."""
        while True:
            event = await self.queue.get()
            try:
                if event is None:
                    return
                await self.handle(event)
            finally:
                self.queue.task_done()

    async def handle(self, event: StoreWriteEvent) -> None:
        """Run all matching actions for one event."""
        for action in self.actions_for(event):
            await self.run_action(action, event)

    def actions_for(self, event: MemoryEvent) -> list[EventAction[Any]]:
        """Return actions that accept this event type."""
        return [
            action
            for action in self.actions
            if isinstance(event, action.event_type)
        ]

    async def run_action(
        self,
        action: EventAction[Any],
        event: MemoryEvent,
    ) -> None:
        """Run one action and isolate side-effect failures."""
        try:
            await action.run(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "[EventWorker] action failed action=%s event=%s",
                action.name,
                event.name,
            )

    @staticmethod
    def supports(event: Any) -> bool:
        """Return whether the worker receives this event."""
        return isinstance(
            event,
            (
                MemoryStoredEvent,
                SessionTurnStoredEvent,
                SessionMergedEvent,
                SessionEndedEvent,
            ),
        )
