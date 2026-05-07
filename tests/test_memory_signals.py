# SPDX-License-Identifier: Apache-2.0
"""Tests for memory lifecycle event dispatch."""

from __future__ import annotations

import asyncio
import unittest

from opencortex.store.events import (
    MemoryEventManager,
    MemoryStoredEvent,
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
