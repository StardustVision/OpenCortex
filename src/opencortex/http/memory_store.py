# SPDX-License-Identifier: Apache-2.0
"""HTTP helpers for `/api/v1/memory/store`."""

from __future__ import annotations

import re
from typing import Dict

_CODE_PATTERN = re.compile(
    r"^\s*(def |class |import |from |if |for |while |return |"
    r"const |let |var |function |\{|\}|//|#!)"
)


def store_warnings(abstract: str) -> list[Dict[str, str]]:
    """Return advisory warnings for a store request. Never blocks storage."""
    warnings: list[Dict[str, str]] = []
    stripped = abstract.strip()
    if len(stripped) < 10:
        warnings.append(
            {
                "key": "abstract_too_short",
                "message": (
                    "Memory abstract should be at least 10 characters "
                    "for useful retrieval"
                ),
            }
        )
        return warnings

    lines = [line for line in stripped.splitlines() if line.strip()]
    if len(lines) >= 2:
        code_lines = sum(1 for line in lines if _CODE_PATTERN.match(line))
        if code_lines / len(lines) > 0.8:
            warnings.append(
                {
                    "key": "code_snippet_detected",
                    "message": (
                        "Consider storing a description of the code pattern "
                        "rather than raw code"
                    ),
                }
            )
    return warnings
