"""
Embedding LRU Cache with TTL.

Wraps any EmbedderBase to cache results, reducing redundant embedding calls.
"""

import hashlib
import logging
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from opencortex.models.embedder.base import EmbedderBase, EmbedResult

logger = logging.getLogger(__name__)


class CachedEmbedder(EmbedderBase):
    """LRU cache wrapper around any EmbedderBase."""

    def __init__(
        self,
        inner: EmbedderBase,
        max_size: int = 10000,
        ttl_seconds: int = 3600,
    ):
        super().__init__(model_name=f"cached({inner.model_name})")
        self._inner = inner
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._cache: OrderedDict[str, Tuple[EmbedResult, float]] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def _key(self, text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    def _expired(self, key: str) -> bool:
        if key not in self._cache:
            return True
        _, ts = self._cache[key]
        return (time.time() - ts) > self._ttl

    def _evict(self) -> None:
        while len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)

    def embed(self, text: str) -> EmbedResult:
        key = self._key(text)
        if key in self._cache and not self._expired(key):
            self._hits += 1
            self._cache.move_to_end(key)
            return self._cache[key][0]

        self._misses += 1
        result = self._inner.embed(text)
        self._evict()
        self._cache[key] = (result, time.time())
        return result

    def embed_query(self, text: str) -> EmbedResult:
        key = self._key("q:" + text)
        if key in self._cache and not self._expired(key):
            self._hits += 1
            self._cache.move_to_end(key)
            return self._cache[key][0]

        self._misses += 1
        result = self._inner.embed_query(text)
        self._evict()
        self._cache[key] = (result, time.time())
        return result

    def embed_batch(self, texts: List[str]) -> List[EmbedResult]:
        return [self.embed(t) for t in texts]

    def get_dimension(self) -> int:
        return self._inner.get_dimension()

    def close(self):
        self._cache.clear()
        self._inner.close()

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "cache_size": len(self._cache),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self._hits / (self._hits + self._misses) if (self._hits + self._misses) > 0 else 0.0,
        }
