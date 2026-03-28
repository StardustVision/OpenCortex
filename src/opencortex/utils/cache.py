"""Async-compatible TTL cache for LLM results.

Do NOT use functools.lru_cache with async functions — it caches coroutines, not results.
"""
import hashlib
import time
from typing import Any, Dict, Optional


class AsyncTTLCache:
    """Simple TTL cache safe for use with async code.

    Single-thread asyncio event loop only (not thread-safe).
    """

    def __init__(self, ttl_seconds: float = 60.0, max_size: int = 128):
        self._cache: Dict[str, tuple] = {}
        self._ttl = ttl_seconds
        self._max_size = max_size

    def get(self, key: str) -> Optional[Any]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        value, ts = entry
        if time.monotonic() - ts > self._ttl:
            del self._cache[key]
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        if len(self._cache) >= self._max_size:
            oldest = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest]
        self._cache[key] = (value, time.monotonic())

    @staticmethod
    def make_key(*parts) -> str:
        raw = "|".join(str(p) for p in parts)
        return hashlib.md5(raw.encode()).hexdigest()
