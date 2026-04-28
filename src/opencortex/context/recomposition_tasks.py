# SPDX-License-Identifier: Apache-2.0
"""Background task lifecycle service for context recomposition."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Set

from opencortex.http.request_context import get_collection_name

if TYPE_CHECKING:
    from opencortex.context.manager import ContextManager, SessionKey

logger = logging.getLogger(__name__)

TaskFactory = Callable[[Optional[str]], Awaitable[Any]]


class ContextRecompositionTaskService:
    """Owns recomposition task registries, failures, and close cleanup."""

    def __init__(self, manager: "ContextManager") -> None:
        """Create empty task registries for one context manager."""
        self._manager = manager
        self._session_merge_locks: Dict[SessionKey, asyncio.Lock] = {}
        self._session_merge_tasks: Dict[SessionKey, asyncio.Task] = {}
        self._session_merge_task_failures: Dict[SessionKey, List[BaseException]] = {}
        self._session_merge_followup_tasks: Dict[SessionKey, Set[asyncio.Task]] = {}
        self._session_merge_followup_failures: Dict[
            SessionKey, List[BaseException]
        ] = {}
        self._session_full_recompose_tasks: Dict[SessionKey, asyncio.Task] = {}
        self._pending_tasks: Set[asyncio.Task] = set()

    @property
    def session_merge_tasks(self) -> Dict["SessionKey", asyncio.Task]:
        """Return active merge tasks for diagnostics and compatibility tests."""
        return self._session_merge_tasks

    @property
    def session_merge_followup_tasks(self) -> Dict["SessionKey", Set[asyncio.Task]]:
        """Return active merge follow-up tasks for diagnostics."""
        return self._session_merge_followup_tasks

    def merge_lock(self, sk: "SessionKey") -> asyncio.Lock:
        """Return the session merge lock used around buffer snapshots."""
        return self._session_merge_locks.setdefault(sk, asyncio.Lock())

    def track_pending_task(self, task: asyncio.Task) -> asyncio.Task:
        """Track a fire-and-forget task so manager close can drain it."""
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return task

    async def close(self) -> None:
        """Await and clear all tracked background tasks."""
        if not self._pending_tasks:
            return
        await asyncio.gather(*tuple(self._pending_tasks), return_exceptions=True)
        self._pending_tasks.clear()

    def cleanup_session(self, sk: "SessionKey") -> None:
        """Remove all recomposition task state for one session."""
        self._session_merge_locks.pop(sk, None)
        self._session_merge_tasks.pop(sk, None)
        self._session_merge_task_failures.pop(sk, None)
        self._session_merge_followup_tasks.pop(sk, None)
        self._session_merge_followup_failures.pop(sk, None)
        self._session_full_recompose_tasks.pop(sk, None)

    def spawn_merge_task(
        self,
        sk: "SessionKey",
        task_factory: TaskFactory,
    ) -> Optional[asyncio.Task]:
        """Start one background merge worker for the session if needed."""
        existing_task = self._session_merge_tasks.get(sk)
        if existing_task and not existing_task.done():
            return existing_task

        task = asyncio.create_task(task_factory(get_collection_name()))
        self._session_merge_tasks[sk] = task
        self.track_pending_task(task)

        def _cleanup(done_task: asyncio.Task) -> None:
            if self._session_merge_tasks.get(sk) is not done_task:
                return
            with contextlib.suppress(asyncio.CancelledError):
                exc = done_task.exception()
                if exc is not None:
                    failures = self._session_merge_task_failures.setdefault(sk, [])
                    failures.append(exc)
            self._session_merge_tasks.pop(sk, None)

        task.add_done_callback(_cleanup)
        return task

    def spawn_full_recompose_task(
        self,
        sk: "SessionKey",
        task_factory: TaskFactory,
    ) -> Optional[asyncio.Task]:
        """Start one async full-session recomposition worker per session."""
        existing_task = self._session_full_recompose_tasks.get(sk)
        if existing_task and not existing_task.done():
            return existing_task

        task = asyncio.create_task(task_factory(get_collection_name()))
        self._session_full_recompose_tasks[sk] = task
        self.track_pending_task(task)

        def _cleanup(done_task: asyncio.Task) -> None:
            if self._session_full_recompose_tasks.get(sk) is not done_task:
                return
            self._session_full_recompose_tasks.pop(sk, None)

        task.add_done_callback(_cleanup)
        return task

    async def wait_for_full_recompose_task(
        self,
        sk: "SessionKey",
        task: Optional[asyncio.Task],
        *,
        timeout: float,
    ) -> None:
        """Wait for a full recomposition task, cancelling it on timeout."""
        active_task = task or self._session_full_recompose_tasks.get(sk)
        if active_task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(active_task), timeout=timeout)
        except asyncio.TimeoutError:
            active_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active_task
            raise

    async def wait_for_merge_task(self, sk: "SessionKey") -> List[BaseException]:
        """Wait until any in-flight background merge for the session finishes."""
        failures = list(self._session_merge_task_failures.pop(sk, []))
        task = self._session_merge_tasks.get(sk)
        if not task:
            return self._dedupe_failures(failures)
        results = await asyncio.gather(task, return_exceptions=True)
        failures.extend(self._task_failures(results))
        return self._dedupe_failures(failures)

    def track_session_merge_followup_task(
        self,
        sk: "SessionKey",
        task: asyncio.Task,
    ) -> None:
        """Track deferred tasks spawned from a session merge worker."""
        session_tasks = self._session_merge_followup_tasks.setdefault(sk, set())
        session_tasks.add(task)
        self.track_pending_task(task)

        def _cleanup(done_task: asyncio.Task) -> None:
            active_tasks = self._session_merge_followup_tasks.get(sk)
            if active_tasks is None or done_task not in active_tasks:
                return
            with contextlib.suppress(asyncio.CancelledError):
                exc = done_task.exception()
                if exc is not None:
                    failures = self._session_merge_followup_failures.setdefault(sk, [])
                    failures.append(exc)
            active_tasks.discard(done_task)
            if not active_tasks:
                self._session_merge_followup_tasks.pop(sk, None)

        task.add_done_callback(_cleanup)

    async def wait_for_merge_followup_tasks(
        self, sk: "SessionKey"
    ) -> List[BaseException]:
        """Wait until deferred follow-up tasks for the session merge finish."""
        failures: List[BaseException] = list(
            self._session_merge_followup_failures.pop(sk, []),
        )
        while True:
            tasks = tuple(self._session_merge_followup_tasks.get(sk, set()))
            if not tasks:
                return self._dedupe_failures(failures)
            logger.info(
                "[ContextManager] Waiting for merge follow-up tasks sk=%s pending=%d",
                sk,
                len(tasks),
            )
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failures.extend(self._task_failures(results))

    @staticmethod
    def _task_failures(results: List[Any]) -> List[BaseException]:
        """Extract task failures from gather results."""
        return [result for result in results if isinstance(result, BaseException)]

    @staticmethod
    def _dedupe_failures(failures: List[BaseException]) -> List[BaseException]:
        """Deduplicate failures by object identity while preserving order."""
        deduped: List[BaseException] = []
        seen_ids: Set[int] = set()
        for failure in failures:
            if id(failure) in seen_ids:
                continue
            seen_ids.add(id(failure))
            deduped.append(failure)
        return deduped
