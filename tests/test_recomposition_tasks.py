# SPDX-License-Identifier: Apache-2.0
"""Tests for recomposition task lifecycle ownership."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock

from opencortex.context.recomposition_tasks import ContextRecompositionTaskService


class TestContextRecompositionTaskService(unittest.TestCase):
    """Task lifecycle behavior for recomposition background work."""

    def test_close_drains_pending_tasks(self) -> None:
        """Tracked pending tasks are awaited and cleared on close."""

        async def _case() -> None:
            service = ContextRecompositionTaskService(MagicMock())
            completed = asyncio.Event()

            async def work() -> None:
                await asyncio.sleep(0)
                completed.set()

            service.track_pending_task(asyncio.create_task(work()))
            await service.close()

            self.assertTrue(completed.is_set())
            self.assertEqual(service._pending_tasks, set())

        asyncio.run(_case())

    def test_cleanup_session_removes_task_state(self) -> None:
        """Session cleanup removes all service-owned task registries."""

        async def _case() -> None:
            service = ContextRecompositionTaskService(MagicMock())
            sk = ("context", "tenant", "user", "session")
            task = asyncio.create_task(asyncio.sleep(0))
            followup = asyncio.create_task(asyncio.sleep(0))

            service.merge_lock(sk)
            service._session_merge_tasks[sk] = task
            service._session_merge_task_failures[sk] = [RuntimeError("merge")]
            service._session_merge_followup_tasks[sk] = {followup}
            service._session_merge_followup_failures[sk] = [RuntimeError("followup")]
            service._session_full_recompose_tasks[sk] = task

            service.cleanup_session(sk)
            await asyncio.gather(task, followup)

            self.assertNotIn(sk, service._session_merge_locks)
            self.assertNotIn(sk, service._session_merge_tasks)
            self.assertNotIn(sk, service._session_merge_task_failures)
            self.assertNotIn(sk, service._session_merge_followup_tasks)
            self.assertNotIn(sk, service._session_merge_followup_failures)
            self.assertNotIn(sk, service._session_full_recompose_tasks)

        asyncio.run(_case())

    def test_cleanup_session_prevents_late_failure_reinsert(self) -> None:
        """Late task callbacks do not recreate state after cleanup."""

        async def _case() -> None:
            service = ContextRecompositionTaskService(MagicMock())
            sk = ("context", "tenant", "user", "session")
            release = asyncio.Event()

            async def fail_late(_collection_name: str | None) -> None:
                await release.wait()
                raise RuntimeError("late failure")

            service.spawn_merge_task(sk, fail_late)
            service.cleanup_session(sk)
            release.set()
            await service.close()

            self.assertNotIn(sk, service._session_merge_task_failures)
            self.assertNotIn(sk, service._session_merge_tasks)

        asyncio.run(_case())

    def test_merge_wait_dedupes_callback_and_gather_failure(self) -> None:
        """Merge failure is returned once even if callback and gather see it."""

        async def _case() -> None:
            service = ContextRecompositionTaskService(MagicMock())
            sk = ("context", "tenant", "user", "session")
            expected = RuntimeError("merge boom")

            async def fail(_collection_name: str | None) -> None:
                raise expected

            service.spawn_merge_task(sk, fail)
            await asyncio.sleep(0)

            failures = await service.wait_for_merge_task(sk)

            self.assertEqual(failures, [expected])

        asyncio.run(_case())

    def test_full_recompose_timeout_cancels_task(self) -> None:
        """Full recomposition wait cancels the task on timeout."""

        async def _case() -> None:
            service = ContextRecompositionTaskService(MagicMock())
            sk = ("context", "tenant", "user", "session")
            started = asyncio.Event()

            async def wait_forever(_collection_name: str | None) -> None:
                started.set()
                await asyncio.Event().wait()

            task = service.spawn_full_recompose_task(sk, wait_forever)
            await asyncio.wait_for(started.wait(), timeout=1.0)

            with self.assertRaises(asyncio.TimeoutError):
                await service.wait_for_full_recompose_task(sk, task, timeout=0.01)

            self.assertTrue(task.cancelled())

        asyncio.run(_case())


if __name__ == "__main__":
    unittest.main()
