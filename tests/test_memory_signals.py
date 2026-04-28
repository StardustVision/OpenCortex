# SPDX-License-Identifier: Apache-2.0
"""Tests for memory lifecycle signal dispatch."""

from __future__ import annotations

import asyncio
import unittest

from opencortex.services.memory_signals import (
    MemorySignalBus,
    MemoryStoredSignal,
)


class TestMemorySignalBus(unittest.IsolatedAsyncioTestCase):
    """Signal bus dispatch and failure containment."""

    async def test_publish_without_subscribers_is_noop(self) -> None:
        """Publishing with no subscribers should not create tasks."""
        bus = MemorySignalBus()

        bus.publish_nowait(
            MemoryStoredSignal(
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

    async def test_async_subscriber_receives_signal(self) -> None:
        """Async subscribers receive the published signal payload."""
        bus = MemorySignalBus()
        received: list[MemoryStoredSignal] = []
        delivered = asyncio.Event()

        async def handler(signal: MemoryStoredSignal) -> None:
            received.append(signal)
            delivered.set()

        bus.subscribe("memory_stored", handler)
        signal = MemoryStoredSignal(
            uri="opencortex://tenant/user/memories/test",
            record_id="record-1",
            tenant_id="tenant",
            user_id="user",
            project_id="public",
            context_type="memory",
            category="general",
        )

        bus.publish_nowait(signal)
        await asyncio.wait_for(delivered.wait(), timeout=1)

        self.assertEqual(received, [signal])
        await bus.close()

    async def test_handler_exception_is_contained(self) -> None:
        """A failing plugin handler should not leak to the publisher."""
        bus = MemorySignalBus()
        delivered = asyncio.Event()

        async def failing_handler(_signal: MemoryStoredSignal) -> None:
            delivered.set()
            raise RuntimeError("plugin failed")

        bus.subscribe("memory_stored", failing_handler)

        bus.publish_nowait(
            MemoryStoredSignal(
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
