# SPDX-License-Identifier: Apache-2.0
"""Text helpers for opencortex."""

from __future__ import annotations


def smart_truncate(text: str, max_chars: int) -> str:
    """Truncate text without raising on empty values."""
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 1)].rstrip() + "…"


def estimate_tokens(text: str) -> int:
    """Return a cheap token-count estimate."""
    value = str(text or "")
    return max(1, len(value) // 4)
