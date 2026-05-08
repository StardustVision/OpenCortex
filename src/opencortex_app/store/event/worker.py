# SPDX-License-Identifier: Apache-2.0
"""Persistent store event worker."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import structlog

from opencortex_app.storage.cfs_queue import CFSQueue, QueueMessage
from opencortex_app.store.event.actions import EventAction
from opencortex_app.store.event.events import (
    MemoryEvent,
    MemoryEventManager,
    MemoryStoredEvent,
    SessionEndedEvent,
    SessionMergedEvent,
    SessionTurnStoredEvent,
)
from opencortex_app.store.event.failure import classify_event_failure
from opencortex_app.store.types import EventName

logger = structlog.get_logger(__name__)

StoreWriteEvent = (
    MemoryStoredEvent | SessionTurnStoredEvent | SessionMergedEvent | SessionEndedEvent
)

EVENT_TYPES: dict[str, type[StoreWriteEvent]] = {
    str(EventName.MEMORY_STORED): MemoryStoredEvent,
    str(EventName.SESSION_TURN_STORED): SessionTurnStoredEvent,
    str(EventName.SESSION_MERGED): SessionMergedEvent,
    str(EventName.SESSION_ENDED): SessionEndedEvent,
}


class EventWorker:
    """Receive store events through a persistent CFS queue."""

    def __init__(
        self,
        *,
        memory_events: MemoryEventManager,
        event_queue: CFSQueue,
        actions: list[EventAction[Any]] | None = None,
        queue_name: str = "store_events",
        max_attempts: int = 3,
        idle_sleep_seconds: float = 0.1,
    ) -> None:
        self.memory_events = memory_events
        self.event_queue = event_queue
        self.actions = list(actions or [])
        self.queue_name = queue_name
        self.max_attempts = max(1, int(max_attempts))
        self.idle_sleep_seconds = max(0.01, float(idle_sleep_seconds))
        self.task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def subscribe(self) -> None:
        """Subscribe this worker to the four store write events."""
        self.memory_events.subscribe(str(EventName.MEMORY_STORED), self.enqueue)
        self.memory_events.subscribe(str(EventName.SESSION_TURN_STORED), self.enqueue)
        self.memory_events.subscribe(str(EventName.SESSION_MERGED), self.enqueue)
        self.memory_events.subscribe(str(EventName.SESSION_ENDED), self.enqueue)

    def enqueue(self, event: Any) -> None:
        """Enqueue supported store events without blocking the publisher."""
        if self.supports(event):
            self.event_queue.enqueue(
                self.queue_name,
                self.event_payload(event),
                max_attempts=self.max_attempts,
            )

    async def start(self) -> None:
        """Start the worker loop."""
        if self.task is not None and not self.task.done():
            return
        self._stop.clear()
        self.task = asyncio.create_task(
            self.run(),
            name="opencortex_app.store.event.worker",
        )

    async def close(self) -> None:
        """Stop the worker loop and await shutdown."""
        task = self.task
        if task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(task, timeout=30.0)
        except asyncio.TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.task = None

    async def wait_idle(self, *, timeout_seconds: float = 30.0) -> None:
        """Wait until this worker's persistent queue has no active messages."""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            status = self.event_queue.status(self.queue_name)
            if status.pending == 0 and status.processing == 0:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Timed out waiting for queue {self.queue_name}")
            await asyncio.sleep(self.idle_sleep_seconds)

    async def run(self) -> None:
        """Run the queue consumer loop."""
        while not self._stop.is_set():
            message = self.event_queue.dequeue(self.queue_name)
            if message is None:
                await asyncio.sleep(self.idle_sleep_seconds)
                continue
            await self.process(message)

    async def process(self, message: QueueMessage) -> None:
        """Run one persisted queue message and update queue state."""
        try:
            event = self.event_from_payload(message.payload)
            failures = await self.handle(event)
            if failures:
                raise failures[0]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = classify_event_failure(exc)
            self.event_queue.fail(
                message.id,
                failure.message,
                retry=failure.retry,
                delay_seconds=failure.delay_seconds,
            )
            logger.warning(
                "store_event_failed",
                queue_name=self.queue_name,
                message_id=message.id,
                retry=failure.retry,
                error=failure.message,
            )
            return
        self.event_queue.ack(message.id)

    async def handle(self, event: StoreWriteEvent) -> list[Exception]:
        """Run all matching actions for one event and return contained failures."""
        failures: list[Exception] = []
        for action in self.actions_for(event):
            failure = await self.run_action(action, event)
            if failure is not None:
                failures.append(failure)
        return failures

    def actions_for(self, event: MemoryEvent) -> list[EventAction[Any]]:
        """Return actions that accept this event type."""
        return [
            action for action in self.actions if isinstance(event, action.event_type)
        ]

    async def run_action(
        self,
        action: EventAction[Any],
        event: MemoryEvent,
    ) -> Exception | None:
        """Run one action and isolate side-effect failures."""
        try:
            await action.run(event)
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "store_event_action_failed",
                action_name=action.name,
                event_name=event.name,
            )
            return exc

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

    @staticmethod
    def event_payload(event: StoreWriteEvent) -> dict[str, Any]:
        """Serialize one supported event for persistent queue storage."""
        return {
            "event_name": event.name,
            "event": event.model_dump(mode="json"),
        }

    @staticmethod
    def event_from_payload(payload: dict[str, Any]) -> StoreWriteEvent:
        """Restore one supported event from a persistent queue payload."""
        event_name = str(payload.get("event_name", "") or "")
        event_type = EVENT_TYPES.get(event_name)
        if event_type is None:
            raise ValueError(f"Unsupported store event payload: {event_name}")
        event_data = payload.get("event")
        if not isinstance(event_data, dict):
            raise ValueError(f"Invalid store event payload: {event_name}")
        return event_type.model_validate(event_data)
