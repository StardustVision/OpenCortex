# SPDX-License-Identifier: Apache-2.0
"""Request-scoped wait tracking for store side effects."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

_current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "opencortex_store_wait_request_id",
    default="",
)


@dataclass
class StoreWaitState:
    """Queue progress for one store wait request."""

    pending: set[str] = field(default_factory=set)
    done: int = 0
    failed: int = 0
    requeue_count: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def status(self) -> dict[str, Any]:
        """Return public status counters."""
        return {
            "pending": len(self.pending),
            "done": self.done,
            "failed": self.failed,
            "requeue_count": self.requeue_count,
            "errors": list(self.errors),
        }


class StoreWaitTracker:
    """Track queue messages derived from one public store request."""

    def __init__(self, *, retention_seconds: float = 900.0) -> None:
        self.retention_seconds = max(60.0, float(retention_seconds))
        self._states: dict[str, StoreWaitState] = {}
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def scope(self, request_id: str) -> Iterator[None]:
        """Set the active wait request id for events published in this scope."""
        token = _current_request_id.set(request_id)
        try:
            yield
        finally:
            _current_request_id.reset(token)

    def current_request_id(self) -> str:
        """Return the request id inherited by the current task."""
        return _current_request_id.get()

    async def register_request(self, request_id: str) -> None:
        """Ensure a state exists before store work starts."""
        if not request_id:
            return
        with self._lock:
            self._cleanup_locked()
            self._states.setdefault(request_id, StoreWaitState())

    def register_message_nowait(self, request_id: str, message_id: str) -> None:
        """Track one queued side-effect message from synchronous event handlers."""
        if not request_id or not message_id:
            return
        with self._lock:
            state = self._states.setdefault(request_id, StoreWaitState())
            state.pending.add(message_id)
            state.updated_at = time.time()

    async def register_message(self, request_id: str, message_id: str) -> None:
        """Track one queued side-effect message."""
        self.register_message_nowait(request_id, message_id)

    async def mark_done(self, request_id: str, message_id: str) -> None:
        """Mark one queued message as fully acknowledged."""
        if not request_id or not message_id:
            return
        with self._lock:
            state = self._states.get(request_id)
            if state is None:
                return
            if message_id in state.pending:
                state.pending.remove(message_id)
                state.done += 1
            state.updated_at = time.time()

    async def mark_requeued(self, request_id: str, message_id: str) -> None:
        """Record a retry while keeping the message pending."""
        if not request_id or not message_id:
            return
        with self._lock:
            state = self._states.get(request_id)
            if state is None:
                return
            state.pending.add(message_id)
            state.requeue_count += 1
            state.updated_at = time.time()

    async def mark_failed(
        self,
        request_id: str,
        message_id: str,
        message: str,
    ) -> None:
        """Mark one queued message as terminally failed."""
        if not request_id or not message_id:
            return
        with self._lock:
            state = self._states.get(request_id)
            if state is None:
                return
            if message_id in state.pending:
                state.pending.remove(message_id)
            state.failed += 1
            state.errors.append({"message": message})
            state.updated_at = time.time()

    async def wait_for_request(
        self,
        request_id: str,
        *,
        timeout_seconds: float,
        poll_interval: float = 0.05,
    ) -> None:
        """Wait until the request has no pending queue messages."""
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout_seconds)
        while True:
            if await self.is_complete(request_id):
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Timed out waiting for store request {request_id}")
            await asyncio.sleep(poll_interval)

    async def is_complete(self, request_id: str) -> bool:
        """Return whether all registered messages reached a terminal state."""
        if not request_id:
            return True
        with self._lock:
            state = self._states.get(request_id)
            if state is None:
                return True
            return not state.pending

    async def status(self, request_id: str) -> dict[str, Any]:
        """Return public status for one request id."""
        with self._lock:
            self._cleanup_locked()
            state = self._states.get(request_id)
            if state is None:
                return {
                    "request_id": request_id,
                    "index_status": "unknown",
                    "queue_status": {
                        "pending": 0,
                        "done": 0,
                        "failed": 0,
                        "requeue_count": 0,
                        "errors": [],
                    },
                }
            queue_status = state.status()
        index_status = "ready" if queue_status["pending"] == 0 else "processing"
        if queue_status["failed"]:
            index_status = "failed"
        return {
            "request_id": request_id,
            "index_status": index_status,
            "queue_status": queue_status,
        }

    def _cleanup_locked(self) -> None:
        cutoff = time.time() - self.retention_seconds
        expired = [
            request_id
            for request_id, state in self._states.items()
            if state.updated_at < cutoff and not state.pending
        ]
        for request_id in expired:
            self._states.pop(request_id, None)
