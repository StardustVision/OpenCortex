# SPDX-License-Identifier: Apache-2.0
"""Tests for memory lifecycle event dispatch."""

from __future__ import annotations

import asyncio
import unittest

from opencortex.core.identity import IdentityProfile
from opencortex.store.event_actions import SearchIndexAction
from opencortex.store.event_worker import EventWorker
from opencortex.store.events import (
    MemoryEvent,
    MemoryEventManager,
    MemoryStoredEvent,
    SessionTurnStoredEvent,
)


class TestMemoryEventManager(unittest.IsolatedAsyncioTestCase):
    """Event bus dispatch and failure containment."""

    async def test_publish_without_subscribers_is_noop(self) -> None:
        """Publishing with no subscribers should not create tasks."""
        bus = MemoryEventManager()

        bus.publish_nowait(
            MemoryStoredEvent(
                uri="opencortex://tenant/user/memories/test",
                record_id="record-1",
                tenant_id="tenant",
                user_id="user",
                project_id="public",
                context_type="memory",
                category="general",
            )
        )

        await bus.close()


class _CaptureAction:
    """Test action that records accepted events."""

    name = "capture"

    def __init__(
        self,
        event_type: type[MemoryEvent],
        received: list[MemoryEvent],
    ) -> None:
        self.event_type = event_type
        self.received = received

    async def run(self, event: MemoryEvent) -> None:
        """Capture one event."""
        self.received.append(event)


class _FailingAction:
    """Test action that fails without stopping the worker."""

    name = "failing"
    event_type = MemoryStoredEvent

    async def run(self, event: MemoryStoredEvent) -> None:
        """Raise to verify action failure isolation."""
        _ = event
        raise RuntimeError("action failed")


class TestEventWorker(unittest.IsolatedAsyncioTestCase):
    """Event worker routes supported events to matching actions."""

    async def test_worker_runs_matching_actions(self) -> None:
        """Only actions matching the event type are run."""
        events = MemoryEventManager()
        received: list[MemoryEvent] = []
        worker = EventWorker(
            memory_events=events,
            actions=[_CaptureAction(MemoryStoredEvent, received)],
        )
        event = MemoryStoredEvent(
            uri="opencortex://tenant/user/memories/test",
            record_id="record-1",
            tenant_id="tenant",
            user_id="user",
            project_id="public",
            context_type="memory",
            category="general",
        )

        await worker.handle(event)

        self.assertEqual(received, [event])

    async def test_action_failure_does_not_stop_next_action(self) -> None:
        """One failing action does not block following actions."""
        events = MemoryEventManager()
        received: list[MemoryEvent] = []
        worker = EventWorker(
            memory_events=events,
            actions=[
                _FailingAction(),
                _CaptureAction(MemoryStoredEvent, received),
            ],
        )
        event = MemoryStoredEvent(
            uri="opencortex://tenant/user/memories/test",
            record_id="record-1",
            tenant_id="tenant",
            user_id="user",
            project_id="public",
            context_type="memory",
            category="general",
        )

        await worker.handle(event)

        self.assertEqual(received, [event])

    async def test_worker_subscribes_to_four_store_events(self) -> None:
        """The worker receives the four store/session event types."""
        events = MemoryEventManager()
        received: list[MemoryEvent] = []
        worker = EventWorker(
            memory_events=events,
            actions=[_CaptureAction(MemoryEvent, received)],
        )
        worker.subscribe()
        await worker.start()
        memory_event = MemoryStoredEvent(
            uri="opencortex://tenant/user/memories/test",
            record_id="record-1",
            tenant_id="tenant",
            user_id="user",
            project_id="public",
            context_type="memory",
            category="general",
        )
        turn_event = SessionTurnStoredEvent(
            session_id="session-1",
            turn_id="turn-1",
            tenant_id="tenant",
            user_id="user",
            project_id="public",
            record_uris=["uri-1"],
        )

        events.publish_nowait(memory_event)
        events.publish_nowait(turn_event)
        await asyncio.sleep(0)
        await worker.close()
        await events.close()

        self.assertEqual(received, [memory_event, turn_event])


class TestSearchIndexAction(unittest.IsolatedAsyncioTestCase):
    """SearchIndexAction names the secondary search indexes explicitly."""

    async def test_builds_anchor_and_fact_indexes(self) -> None:
        """Stored primary records can produce AnchorIndex and FactIndex entries."""
        action = SearchIndexAction(
            storage=None,
            collection_resolver=lambda: "context",
        )
        event = MemoryStoredEvent(
            profile=IdentityProfile(
                tenant_id="tenant",
                user_id="user",
                project_id="public",
            ),
            uri="opencortex://tenant/user/memories/test",
            record_id="record-1",
            context_type="memory",
            category="general",
            record={
                "entities": ["Alice", "Python"],
                "abstract_json": {
                    "fact_points": [
                        "Alice uses Python",
                        "OpenCortex stores primary records",
                    ]
                },
            },
        )

        anchor_indexes = action.anchor_indexes(event)
        fact_indexes = action.fact_indexes(event)

        self.assertEqual([item.text for item in anchor_indexes], ["Alice", "Python"])
        self.assertEqual(
            [item.text for item in fact_indexes],
            [
                "Alice uses Python",
                "OpenCortex stores primary records",
            ],
        )
        self.assertEqual(anchor_indexes[0].source_uri, event.uri)
        self.assertEqual(fact_indexes[0].source_record_id, event.record_id)

    async def test_async_subscriber_receives_event(self) -> None:
        """Async subscribers receive the published event payload."""
        bus = MemoryEventManager()
        received: list[MemoryStoredEvent] = []
        delivered = asyncio.Event()

        async def handler(event: MemoryStoredEvent) -> None:
            received.append(event)
            delivered.set()

        bus.subscribe("memory_stored", handler)
        event = MemoryStoredEvent(
            uri="opencortex://tenant/user/memories/test",
            record_id="record-1",
            tenant_id="tenant",
            user_id="user",
            project_id="public",
            context_type="memory",
            category="general",
        )

        bus.publish_nowait(event)
        await asyncio.wait_for(delivered.wait(), timeout=1)

        self.assertEqual(received, [event])
        await bus.close()

    async def test_handler_exception_is_contained(self) -> None:
        """A failing plugin handler should not leak to the publisher."""
        bus = MemoryEventManager()
        delivered = asyncio.Event()

        async def failing_handler(_event: MemoryStoredEvent) -> None:
            delivered.set()
            raise RuntimeError("plugin failed")

        bus.subscribe("memory_stored", failing_handler)

        bus.publish_nowait(
            MemoryStoredEvent(
                uri="opencortex://tenant/user/memories/test",
                record_id="record-1",
                tenant_id="tenant",
                user_id="user",
                project_id="public",
                context_type="memory",
                category="general",
            )
        )
        await asyncio.wait_for(delivered.wait(), timeout=1)
        await bus.close()


if __name__ == "__main__":
    unittest.main()
