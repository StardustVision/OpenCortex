# SPDX-License-Identifier: Apache-2.0
"""Lightweight structured timing helpers for the memory intent pipeline."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, TypeVar


T = TypeVar("T")


@dataclass
class StageTimingCollector:
    """Collects per-stage wall-clock timings in milliseconds."""

    _timings: Dict[str, int] = field(default_factory=dict)

    def record_ms(self, stage: str, elapsed_ms: int) -> None:
        """Record a stage duration in milliseconds."""
        self._timings[stage] = max(0, int(elapsed_ms))

    def record_elapsed(self, stage: str, started: float) -> None:
        """Record elapsed time since ``started`` using ``time.monotonic()``."""
        self.record_ms(stage, int((time.monotonic() - started) * 1000))

    def snapshot(self) -> Dict[str, int]:
        """Return a stable phase-timing payload."""
        return {
            "probe": self._timings.get("probe", self._timings.get("route", 0)),
            "plan": self._timings.get("plan", 0),
            "bind": self._timings.get("bind", 0),
            "retrieve": self._timings.get("retrieve", 0),
            "aggregate": self._timings.get("aggregate", 0),
            "total": self._timings.get("total", 0),
        }


async def measure_async(
    collector: StageTimingCollector,
    stage: str,
    func: Callable[..., Awaitable[T]],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Measure an async callable and record its wall-clock duration."""
    started = time.monotonic()
    try:
        return await func(*args, **kwargs)
    finally:
        collector.record_elapsed(stage, started)


def measure_sync(
    collector: StageTimingCollector,
    stage: str,
    func: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Measure a sync callable and record its wall-clock duration."""
    started = time.monotonic()
    try:
        return func(*args, **kwargs)
    finally:
        collector.record_elapsed(stage, started)
