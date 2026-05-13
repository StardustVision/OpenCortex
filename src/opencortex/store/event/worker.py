# SPDX-License-Identifier: Apache-2.0
"""Persistent store event worker."""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import Hashable
from typing import Any

import structlog

from opencortex.storage.cfs_queue import CFSQueue, QueueMessage
from opencortex.store.event.actions import EventAction
from opencortex.store.event.events import (
    CheckUpdateEvent,
    MemoryEvent,
    MemoryEventManager,
    MemoryStoredEvent,
    SessionEndedEvent,
    SessionMergedEvent,
    SessionTurnStoredEvent,
)
from opencortex.store.event.failure import classify_event_failure
from opencortex.store.event.wait import StoreWaitTracker
from opencortex.store.types import EventName

logger = structlog.get_logger(__name__)

StoreWriteEvent = (
    MemoryStoredEvent
    | CheckUpdateEvent
    | SessionTurnStoredEvent
    | SessionMergedEvent
    | SessionEndedEvent
)

EVENT_TYPES: dict[str, type[StoreWriteEvent]] = {
    str(EventName.MEMORY_STORED): MemoryStoredEvent,
    str(EventName.CHECK_UPDATE): CheckUpdateEvent,
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
        concurrency: int = 1,
        locked_key_delay_seconds: float = 2.0,
        wait_tracker: StoreWaitTracker | None = None,
    ) -> None:
        self.memory_events = memory_events
        self.event_queue = event_queue
        self.actions = list(actions or [])
        self.queue_name = queue_name
        self.max_attempts = max(1, int(max_attempts))
        self.idle_sleep_seconds = max(0.01, float(idle_sleep_seconds))
        self.concurrency = max(1, int(concurrency))
        self.locked_key_delay_seconds = max(0.05, float(locked_key_delay_seconds))
        self.wait_tracker = wait_tracker
        self.tasks: set[asyncio.Task[None]] = set()
        self._key_locks: defaultdict[Hashable, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._stop = asyncio.Event()

    def subscribe(self) -> None:
        """Subscribe this worker to store write events."""
        self.memory_events.subscribe(str(EventName.MEMORY_STORED), self.enqueue)
        self.memory_events.subscribe(str(EventName.CHECK_UPDATE), self.enqueue)
        self.memory_events.subscribe(str(EventName.SESSION_TURN_STORED), self.enqueue)
        self.memory_events.subscribe(str(EventName.SESSION_MERGED), self.enqueue)
        self.memory_events.subscribe(str(EventName.SESSION_ENDED), self.enqueue)

    def enqueue(self, event: Any) -> None:
        """Enqueue supported store events without blocking the publisher."""
        if self.supports(event):
            request_id = (
                self.wait_tracker.current_request_id() if self.wait_tracker else ""
            )
            message_id = self.event_queue.enqueue(
                self.queue_name,
                self.event_payload(event, request_id=request_id),
                max_attempts=self.max_attempts,
            )
            if self.wait_tracker is not None and request_id:
                self.wait_tracker.register_message_nowait(request_id, message_id)

    async def aenqueue(self, event: StoreWriteEvent) -> None:
        """Enqueue supported store events through the async queue adapter."""
        request_id = self.wait_tracker.current_request_id() if self.wait_tracker else ""
        message_id = await self.event_queue.aenqueue(
            self.queue_name,
            self.event_payload(event, request_id=request_id),
            max_attempts=self.max_attempts,
        )
        if self.wait_tracker is not None and request_id:
            await self.wait_tracker.register_message(request_id, message_id)

    async def start(self) -> None:
        """Start the worker loop."""
        if self.tasks and any(not task.done() for task in self.tasks):
            return
        self._stop.clear()
        self.tasks = {
            asyncio.create_task(
                self.run(worker_index=index),
                name=f"opencortex.store.event.worker.{index}",
            )
            for index in range(self.concurrency)
        }

    async def close(self) -> None:
        """Stop the worker loop and await shutdown."""
        tasks = list(self.tasks)
        if not tasks:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=30.0)
        except asyncio.TimeoutError:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.tasks.clear()

    async def wait_idle(self, *, timeout_seconds: float = 30.0) -> None:
        """Wait until this worker's persistent queue has no active messages."""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            await self.wait_publish_tasks(deadline=deadline)
            status = await self.event_queue.astatus(self.queue_name)
            if (
                status.pending == 0
                and status.processing == 0
                and not self.memory_events.tasks
            ):
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Timed out waiting for queue {self.queue_name}")
            await asyncio.sleep(self.idle_sleep_seconds)

    async def wait_publish_tasks(self, *, deadline: float) -> None:
        """Wait for in-process event publisher tasks to enqueue their messages."""
        tasks = [
            task
            for task in self.memory_events.tasks
            if task is not asyncio.current_task()
        ]
        if not tasks:
            return
        timeout = max(0.0, deadline - asyncio.get_running_loop().time())
        if timeout <= 0:
            raise TimeoutError(f"Timed out waiting for queue {self.queue_name}")
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            raise TimeoutError(f"Timed out waiting for queue {self.queue_name}")
        for task in done:
            with contextlib.suppress(Exception):
                task.result()

    async def run(self, *, worker_index: int = 0) -> None:
        """Run the queue consumer loop."""
        _ = worker_index
        while not self._stop.is_set():
            message = await self.event_queue.adequeue(self.queue_name)
            if message is None:
                await asyncio.sleep(self.idle_sleep_seconds)
                continue
            await self.process(message)

    async def process(self, message: QueueMessage) -> None:
        """Run one persisted queue message and update queue state."""
        try:
            event = self.event_from_payload(message.payload)
            lock = self._key_locks[self.ordering_key(event)]
            if lock.locked():
                await self.event_queue.arelease(
                    message.id,
                    delay_seconds=self.locked_key_delay_seconds,
                )
                await self.mark_wait_requeued(message)
                return
            async with lock:
                failures = await self.handle(event)
            if failures:
                raise failures[0]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = classify_event_failure(exc)
            await self.event_queue.afail(
                message.id,
                failure.message,
                retry=failure.retry,
                delay_seconds=failure.delay_seconds,
            )
            if failure.retry and message.attempts < message.max_attempts:
                await self.mark_wait_requeued(message)
            else:
                await self.mark_wait_failed(message, failure.message)
            logger.warning(
                "store_event_failed",
                queue_name=self.queue_name,
                message_id=message.id,
                retry=failure.retry,
                error=failure.message,
            )
            return
        await self.event_queue.aack(message.id)
        await self.mark_wait_done(message)

    async def mark_wait_done(self, message: QueueMessage) -> None:
        """Update request-scoped wait state for a completed message."""
        if self.wait_tracker is None:
            return
        request_id = str(message.payload.get("request_id", "") or "")
        if request_id:
            await self.wait_tracker.mark_done(request_id, message.id)

    async def mark_wait_requeued(self, message: QueueMessage) -> None:
        """Update request-scoped wait state for a requeued message."""
        if self.wait_tracker is None:
            return
        request_id = str(message.payload.get("request_id", "") or "")
        if request_id:
            await self.wait_tracker.mark_requeued(request_id, message.id)

    async def mark_wait_failed(self, message: QueueMessage, error: str) -> None:
        """Update request-scoped wait state for a terminally failed message."""
        if self.wait_tracker is None:
            return
        request_id = str(message.payload.get("request_id", "") or "")
        if request_id:
            await self.wait_tracker.mark_failed(request_id, message.id, error)

    @staticmethod
    def ordering_key(event: StoreWriteEvent) -> Hashable:
        """Return the serialization key for ordered side effects."""
        profile = getattr(event, "profile", None)
        tenant_id = getattr(profile, "tenant_id", "") or getattr(event, "tenant_id", "")
        user_id = getattr(profile, "user_id", "") or getattr(event, "user_id", "")
        project_id = getattr(profile, "project_id", "") or getattr(
            event, "project_id", ""
        )
        session_id = getattr(profile, "session_id", "") or getattr(
            event, "session_id", ""
        )
        if isinstance(
            event,
            (SessionTurnStoredEvent, SessionMergedEvent, SessionEndedEvent),
        ):
            return ("session", tenant_id, user_id, project_id, session_id)
        uri = getattr(event, "uri", "") or getattr(event, "record_id", "")
        return ("record", tenant_id, user_id, project_id, uri)

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
        if isinstance(event, CheckUpdateEvent):
            return [
                action
                for action in self.actions
                if action.event_type is CheckUpdateEvent
            ]
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
                CheckUpdateEvent,
                SessionTurnStoredEvent,
                SessionMergedEvent,
                SessionEndedEvent,
            ),
        )

    @staticmethod
    def event_payload(
        event: StoreWriteEvent,
        *,
        request_id: str = "",
    ) -> dict[str, Any]:
        """Serialize one supported event for persistent queue storage."""
        return {
            "event_name": event.name,
            "event": event.model_dump(mode="json"),
            "request_id": request_id,
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
