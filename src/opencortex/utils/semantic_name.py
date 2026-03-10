# SPDX-License-Identifier: Apache-2.0
"""
Semantic node naming for OpenCortex URIs.

Generates filesystem-safe semantic names from text.
Produces deterministic, human-readable URI segments from arbitrary text.
"""
import hashlib
import re


def semantic_node_name(text: str, max_length: int = 50) -> str:
    """Sanitize text for use as a URI node name.

    Preserves letters, digits, CJK characters, underscores, and hyphens.
    Replaces all other characters with underscores. Merges consecutive
    underscores. If the result exceeds *max_length*, truncates and appends
    a SHA-256 hash suffix for uniqueness.

    Args:
        text: Input text (e.g., abstract, filename).
        max_length: Maximum output length (default 50).

    Returns:
        URI-safe, deterministic node name. Returns ``"unnamed"`` for empty input.
    """
    safe = re.sub(
        r"[^\w\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af\u3400-\u4dbf-]",
        "_",
        text,
    )
    safe = re.sub(r"_+", "_", safe).strip("_")

    if not safe:
        return "unnamed"

    if len(safe) > max_length:
        hash_suffix = hashlib.sha256(text.encode()).hexdigest()[:8]
        safe = f"{safe[:max_length - 9]}_{hash_suffix}"

    return safe
