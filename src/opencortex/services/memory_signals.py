# SPDX-License-Identifier: Apache-2.0
"""Async lifecycle signals for optional memory plugins."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Set

logger = logging.getLogger(__name__)

SignalHandler = Callable[[Any], Awaitable[None] | None]


@dataclass(frozen=True)
class MemoryStoredSignal:
    """Emitted after a memory record has been durably stored or merged."""

    uri: str
    record_id: str
    tenant_id: str
    user_id: str
    project_id: str
    context_type: str
    category: str
    dedup_action: str = ""
    record: Dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """Signal name used by the bus."""
        return "memory_stored"


@dataclass(frozen=True)
class RecallCompletedSignal:
    """Emitted after core recall has assembled its result."""

    query: str
    tenant_id: str
    user_id: str
    memories: List[Any] = field(default_factory=list)
    resources: List[Any] = field(default_factory=list)
    skills: List[Any] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Signal name used by the bus."""
        return "recall_completed"


class MemorySignalBus:
    """Small in-process async signal bus for memory lifecycle plugins."""

    def __init__(self) -> None:
        """Create an empty signal bus."""
        self._subscribers: Dict[str, List[SignalHandler]] = {}
        self._tasks: Set[asyncio.Task[None]] = set()

    def subscribe(self, signal_name: str, handler: SignalHandler) -> None:
        """Register a handler for a named signal."""
        self._subscribers.setdefault(signal_name, []).append(handler)

    def publish_nowait(self, signal: Any) -> None:
        """Schedule signal handlers without blocking the caller."""
        handlers = list(self._subscribers.get(str(signal.name), []))
        if not handlers:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "[MemorySignalBus] Dropping %s signal without running loop",
                signal.name,
            )
            return

        for handler in handlers:
            task = loop.create_task(
                self._dispatch(handler, signal),
                name=f"opencortex.memory_signal.{signal.name}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        """Cancel and await pending handler tasks."""
        if not self._tasks:
            return
        tasks = list(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def _dispatch(self, handler: SignalHandler, signal: Any) -> None:
        """Run one handler and contain plugin failures."""
        try:
            result = handler(signal)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[MemorySignalBus] Handler failed for %s: %s",
                signal.name,
                exc,
            )
