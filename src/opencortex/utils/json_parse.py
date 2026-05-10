# SPDX-License-Identifier: Apache-2.0
"""JSON extraction helpers."""

from __future__ import annotations

import json
from typing import Any


def parse_json_from_response(response: str) -> dict[str, Any]:
    """Parse a JSON object from an LLM response."""
    text = str(response or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return {}
        value = json.loads(text[start : end + 1])
    return value if isinstance(value, dict) else {}
