# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for extracting JSON from LLM responses."""

import orjson as json
import re
from typing import Optional, Union

# Pre-compiled patterns
_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def parse_json_from_response(
    response: str,
    *,
    expect_array: bool = False,
) -> Optional[Union[dict, list]]:
    """Parse JSON (object or array) from an LLM response string.

    Handles common LLM output patterns:
    - Pure JSON
    - JSON wrapped in markdown code blocks (```json ... ```)
    - JSON embedded in surrounding text

    Args:
        response: Raw LLM response string.
        expect_array: If True, look for ``[...]`` instead of ``{...}``.

    Returns:
        Parsed dict/list, or None if parsing fails.
    """
    if not response:
        return None

    stripped = response.strip()

    # 1. Direct parse
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2. Markdown code block
    match = _CODE_BLOCK_RE.search(response)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3. First balanced brace/bracket block (non-greedy is insufficient;
    #    we count nesting so that inner braces don't cause mis-match).
    open_ch, close_ch = ("[", "]") if expect_array else ("{", "}")
    extracted = _extract_balanced(response, open_ch, close_ch)
    if extracted is not None:
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            pass

    return None


def _extract_balanced(text: str, open_ch: str, close_ch: str) -> Optional[str]:
    """Extract the first balanced ``open_ch…close_ch`` substring."""
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
