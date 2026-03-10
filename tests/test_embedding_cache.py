# SPDX-License-Identifier: Apache-2.0
"""Tests for CachedEmbedder (LRU cache with TTL)."""

import time
import unittest

from opencortex.models.embedder.base import EmbedderBase, EmbedResult
from opencortex.models.embedder.cache import CachedEmbedder


class MockEmbedder(EmbedderBase):
    """Mock embedder that counts calls."""

    def __init__(self):
        super().__init__(model_name="mock")
        self.call_count = 0

    def embed(self, text: str) -> EmbedResult:
        self.call_count += 1
        # Deterministic fake vector based on text hash
        h = hash(text) % 1000
        return EmbedResult(dense_vector=[float(h)] * 4)

    def get_dimension(self) -> int:
        return 4


class TestCachedEmbedder(unittest.TestCase):
    """Test CachedEmbedder behavior."""

    def test_cache_hit(self):
        """Same text should hit cache on second call."""
        mock = MockEmbedder()
        cached = CachedEmbedder(mock, max_size=100, ttl_seconds=60)

        r1 = cached.embed("hello world")
        r2 = cached.embed("hello world")

        self.assertEqual(r1.dense_vector, r2.dense_vector)
        self.assertEqual(mock.call_count, 1)  # Only called inner once
        self.assertEqual(cached.stats["hits"], 1)
        self.assertEqual(cached.stats["misses"], 1)
        self.assertAlmostEqual(cached.stats["hit_rate"], 0.5)

    def test_cache_miss_different_text(self):
        """Different texts should miss cache."""
        mock = MockEmbedder()
        cached = CachedEmbedder(mock, max_size=100, ttl_seconds=60)

        cached.embed("hello")
        cached.embed("world")

        self.assertEqual(mock.call_count, 2)
        self.assertEqual(cached.stats["hits"], 0)
        self.assertEqual(cached.stats["misses"], 2)

    def test_ttl_expiration(self):
        """Expired entries should be re-computed."""
        mock = MockEmbedder()
        cached = CachedEmbedder(mock, max_size=100, ttl_seconds=0)  # 0s TTL = instant expire

        cached.embed("hello")
        time.sleep(0.01)  # Ensure expiry
        cached.embed("hello")

        self.assertEqual(mock.call_count, 2)  # Both are misses
        self.assertEqual(cached.stats["hits"], 0)
        self.assertEqual(cached.stats["misses"], 2)

    def test_lru_eviction(self):
        """Oldest entry should be evicted when max_size reached."""
        mock = MockEmbedder()
        cached = CachedEmbedder(mock, max_size=3, ttl_seconds=60)

        cached.embed("a")  # cache: [a]
        cached.embed("b")  # cache: [a, b]
        cached.embed("c")  # cache: [a, b, c]
        cached.embed("d")  # cache: [b, c, d] — "a" evicted

        self.assertEqual(cached.stats["cache_size"], 3)
        self.assertEqual(mock.call_count, 4)

        # "a" was evicted, should miss
        cached.embed("a")
        self.assertEqual(mock.call_count, 5)  # a re-computed

        # "d" still cached, should hit
        cached.embed("d")
        self.assertEqual(mock.call_count, 5)  # no new call

    def test_lru_access_refreshes_position(self):
        """Accessing an entry moves it to end, preventing eviction."""
        mock = MockEmbedder()
        cached = CachedEmbedder(mock, max_size=3, ttl_seconds=60)

        cached.embed("a")
        cached.embed("b")
        cached.embed("a")  # refresh "a" → now "b" is oldest
        cached.embed("c")
        cached.embed("d")  # evicts "b" (oldest), not "a"

        # "a" should still be cached
        cached.embed("a")
        self.assertEqual(mock.call_count, 4)  # a,b,c,d — "a" hit twice

        # "b" was evicted
        cached.embed("b")
        self.assertEqual(mock.call_count, 5)  # b re-computed

    def test_embed_batch(self):
        """embed_batch should use per-item cache."""
        mock = MockEmbedder()
        cached = CachedEmbedder(mock, max_size=100, ttl_seconds=60)

        cached.embed("x")  # pre-warm
        results = cached.embed_batch(["x", "y", "x"])

        self.assertEqual(len(results), 3)
        self.assertEqual(mock.call_count, 2)  # "x" cached, "y" new, "x" cached
        self.assertEqual(cached.stats["hits"], 2)
        self.assertEqual(cached.stats["misses"], 2)  # first "x" + "y"

    def test_stats(self):
        """Stats should reflect accurate counts."""
        mock = MockEmbedder()
        cached = CachedEmbedder(mock, max_size=100, ttl_seconds=60)

        self.assertEqual(cached.stats["cache_size"], 0)
        self.assertEqual(cached.stats["hit_rate"], 0.0)

        cached.embed("a")
        cached.embed("a")
        cached.embed("b")
        cached.embed("a")

        stats = cached.stats
        self.assertEqual(stats["cache_size"], 2)
        self.assertEqual(stats["hits"], 2)
        self.assertEqual(stats["misses"], 2)
        self.assertAlmostEqual(stats["hit_rate"], 0.5)

    def test_get_dimension_delegates(self):
        """get_dimension should delegate to inner embedder."""
        mock = MockEmbedder()
        cached = CachedEmbedder(mock)
        self.assertEqual(cached.get_dimension(), 4)

    def test_close_clears_cache(self):
        """close() should clear cache."""
        mock = MockEmbedder()
        cached = CachedEmbedder(mock, max_size=100, ttl_seconds=60)

        cached.embed("a")
        cached.embed("b")
        self.assertEqual(cached.stats["cache_size"], 2)

        cached.close()
        self.assertEqual(cached.stats["cache_size"], 0)


if __name__ == "__main__":
    unittest.main()
