# SPDX-License-Identifier: Apache-2.0
"""Failure classification for persistent store event processing."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel


class PermanentStoreEventError(RuntimeError):
    """Action error that should not be retried."""


class TransientStoreEventError(RuntimeError):
    """Action error that should be retried while attempts remain."""


class EventFailure(BaseModel):
    """Queue failure decision."""

    retry: bool
    delay_seconds: float
    message: str


def classify_event_failure(exc: Exception) -> EventFailure:
    """Classify a store event worker failure for sqlite queue handling."""
    message = str(exc) or exc.__class__.__name__
    if isinstance(exc, PermanentStoreEventError):
        return EventFailure(retry=False, delay_seconds=0.0, message=message)
    if isinstance(exc, TransientStoreEventError):
        return EventFailure(retry=True, delay_seconds=1.0, message=message)
    if isinstance(exc, (TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return EventFailure(retry=True, delay_seconds=1.0, message=message)
    if isinstance(exc, ValueError):
        return EventFailure(retry=False, delay_seconds=0.0, message=message)
    return EventFailure(retry=True, delay_seconds=1.0, message=message)
