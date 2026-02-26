from __future__ import annotations

import hashlib


def deterministic_embedding(text: str, dims: int = 16) -> list[float]:
    """Small deterministic embedding placeholder for scaffold-only flows."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values = []
    for i in range(dims):
        b = digest[i]
        values.append((b / 255.0) * 2 - 1)
    return values
