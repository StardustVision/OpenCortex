# SPDX-License-Identifier: Apache-2.0
"""Text helpers for opencortex."""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Return a cheap token-count estimate."""
    value = str(text or "")
    return max(1, len(value) // 4)
