"""Tests for CachedEmbedder (LRU + TTL cache)."""

import time
import unittest
from unittest.mock import MagicMock, patch

from opencortex.models.embedder.base import EmbedderBase, EmbedResult
from opencortex.models.embedder.cache import CachedEmbedder


class FakeEmbedder(EmbedderBase):
    """Minimal embedder for testing the cache wrapper."""

    def __init__(self):
        super().__init__(model_name="fake")
        self.call_count = 0
        self._dim = 4

    def embed(self, text: str) -> EmbedResult:
        self.call_count += 1
        vec = [float(ord(c) % 10) / 10 for c in text[:self._dim]]
        vec += [0.0] * (self._dim - len(vec))
        return EmbedResult(dense_vector=vec)

    def get_dimension(self) -> int:
        return self._dim


class TestCachedEmbedderBasics(unittest.TestCase):

    def setUp(self):
        self.inner = FakeEmbedder()
        self.cached = CachedEmbedder(self.inner, max_size=100, ttl_seconds=3600)

    def test_cache_hit(self):
        """Second call with same text returns cached result."""
        r1 = self.cached.embed("hello")
        r2 = self.cached.embed("hello")
        self.assertEqual(r1.dense_vector, r2.dense_vector)
        self.assertEqual(self.inner.call_count, 1)

    def test_cache_miss_different_text(self):
        """Different texts produce separate cache entries."""
        self.cached.embed("hello")
        self.cached.embed("world")
        self.assertEqual(self.inner.call_count, 2)

    def test_stats(self):
        """Stats track hits and misses."""
        self.cached.embed("a")
        self.cached.embed("b")
        self.cached.embed("a")  # hit
        stats = self.cached.stats
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 2)
        self.assertAlmostEqual(stats["hit_rate"], 1 / 3)
        self.assertEqual(stats["cache_size"], 2)

    def test_get_dimension_delegates(self):
        self.assertEqual(self.cached.get_dimension(), 4)

    def test_model_name_wraps(self):
        self.assertEqual(self.cached.model_name, "cached(fake)")


class TestCachedEmbedderEviction(unittest.TestCase):

    def test_lru_eviction(self):
        """Cache evicts oldest entries when full."""
        inner = FakeEmbedder()
        cached = CachedEmbedder(inner, max_size=3, ttl_seconds=3600)

        cached.embed("a")
        cached.embed("b")
        cached.embed("c")
        self.assertEqual(inner.call_count, 3)

        # Adding a 4th should evict "a"
        cached.embed("d")
        self.assertEqual(inner.call_count, 4)
        self.assertEqual(cached.stats["cache_size"], 3)

        # "a" should be evicted — re-embedding triggers inner call
        cached.embed("a")
        self.assertEqual(inner.call_count, 5)

    def test_lru_access_refreshes(self):
        """Accessing an entry moves it to end, preventing eviction."""
        inner = FakeEmbedder()
        cached = CachedEmbedder(inner, max_size=3, ttl_seconds=3600)

        cached.embed("a")
        cached.embed("b")
        cached.embed("c")
        # Access "a" to refresh it
        cached.embed("a")  # hit
        self.assertEqual(inner.call_count, 3)

        # Add "d" — should evict "b" (oldest non-refreshed), not "a"
        cached.embed("d")
        # "a" should still be cached
        cached.embed("a")  # hit
        self.assertEqual(inner.call_count, 4)  # only "d" caused a new call

        # "b" should be evicted
        cached.embed("b")  # miss
        self.assertEqual(inner.call_count, 5)


class TestCachedEmbedderTTL(unittest.TestCase):

    def test_ttl_expiry(self):
        """Expired entries are re-fetched."""
        inner = FakeEmbedder()
        cached = CachedEmbedder(inner, max_size=100, ttl_seconds=1)

        cached.embed("hello")
        self.assertEqual(inner.call_count, 1)

        # Manually expire the entry by backdating timestamp
        key = cached._key("hello")
        result, _ = cached._cache[key]
        cached._cache[key] = (result, time.time() - 2)

        cached.embed("hello")
        self.assertEqual(inner.call_count, 2)

    def test_non_expired_entry_not_refetched(self):
        """Non-expired entries are served from cache."""
        inner = FakeEmbedder()
        cached = CachedEmbedder(inner, max_size=100, ttl_seconds=3600)

        cached.embed("hello")
        cached.embed("hello")
        self.assertEqual(inner.call_count, 1)


class TestCachedEmbedderBatch(unittest.TestCase):

    def test_embed_batch(self):
        """embed_batch caches individual texts."""
        inner = FakeEmbedder()
        cached = CachedEmbedder(inner, max_size=100, ttl_seconds=3600)

        results = cached.embed_batch(["a", "b", "c"])
        self.assertEqual(len(results), 3)
        self.assertEqual(inner.call_count, 3)

        # Re-embedding same batch should all be hits
        results2 = cached.embed_batch(["a", "b", "c"])
        self.assertEqual(inner.call_count, 3)  # no new calls
        for r1, r2 in zip(results, results2):
            self.assertEqual(r1.dense_vector, r2.dense_vector)


class TestCachedEmbedderClose(unittest.TestCase):

    def test_close_clears_cache(self):
        inner = FakeEmbedder()
        cached = CachedEmbedder(inner, max_size=100, ttl_seconds=3600)
        cached.embed("test")
        self.assertEqual(cached.stats["cache_size"], 1)

        cached.close()
        self.assertEqual(cached.stats["cache_size"], 0)


if __name__ == "__main__":
    unittest.main()
