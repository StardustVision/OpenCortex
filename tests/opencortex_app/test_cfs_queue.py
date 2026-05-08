# SPDX-License-Identifier: Apache-2.0
"""Tests for SQLite-backed CFS queues."""

from __future__ import annotations

import tempfile
import time
import unittest

from opencortex_app.storage.cfs import CFS
from opencortex_app.storage.cfs_queue import CFSQueue


class TestCFSQueue(unittest.TestCase):
    """Persistent queue behavior."""

    def test_enqueue_dequeue_ack_persists_messages(self) -> None:
        """A message can be claimed, acknowledged, and observed after reopen."""
        with tempfile.TemporaryDirectory() as root:
            queue = CFSQueue(cfs=CFS(root=root))
            message_id = queue.enqueue("events", {"kind": "memory"}, max_attempts=2)

            message = queue.dequeue("events")
            self.assertIsNotNone(message)
            assert message is not None
            self.assertEqual(message.id, message_id)
            self.assertEqual(message.payload, {"kind": "memory"})
            queue.ack(message.id)

            reopened = CFSQueue(cfs=CFS(root=root))
            status = reopened.status("events")
            self.assertEqual(status.pending, 0)
            self.assertEqual(status.processing, 0)
            self.assertEqual(status.done, 1)

    def test_fail_retries_until_max_attempts_then_marks_failed(self) -> None:
        """Transient failures requeue until attempts are exhausted."""
        with tempfile.TemporaryDirectory() as root:
            queue = CFSQueue(cfs=CFS(root=root))
            queue.enqueue("events", {"kind": "memory"}, max_attempts=2)

            first = queue.dequeue("events")
            self.assertIsNotNone(first)
            assert first is not None
            queue.fail(first.id, "temporary", retry=True, delay_seconds=0)

            status = queue.status("events")
            self.assertEqual(status.pending, 1)
            self.assertEqual(status.requeue_count, 1)

            second = queue.dequeue("events")
            self.assertIsNotNone(second)
            assert second is not None
            queue.fail(second.id, "temporary", retry=True, delay_seconds=0)

            status = queue.status("events")
            self.assertEqual(status.pending, 0)
            self.assertEqual(status.failed, 1)
            self.assertEqual(status.errors[0]["message"], "temporary")

    def test_recover_stale_processing_returns_message_to_pending(self) -> None:
        """Stale processing messages are made claimable again."""
        with tempfile.TemporaryDirectory() as root:
            queue = CFSQueue(cfs=CFS(root=root), stale_after_seconds=1)
            queue.enqueue("events", {"kind": "memory"})

            message = queue.dequeue("events")
            self.assertIsNotNone(message)
            assert message is not None
            recovered = queue.recover_stale("events", now=time.time() + 2)

            self.assertEqual(recovered, 1)
            status = queue.status("events")
            self.assertEqual(status.pending, 1)
            self.assertEqual(status.processing, 0)
