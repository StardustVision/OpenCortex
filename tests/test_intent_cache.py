"""Test LRU intent cache with TTL."""
import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.retrieve.intent_router import IntentRouter


class TestIntentCache(unittest.TestCase):
    def test_cache_hit_skips_llm(self):
        call_count = {"n": 0}

        async def mock_llm(prompt):
            call_count["n"] += 1
            return '{"intent_type": "quick_lookup", "top_k": 3}'

        router = IntentRouter(llm_completion=mock_llm)

        async def run():
            ctx = {"summary": "test session"}
            await router.route("What is X?", session_context=ctx)
            first_count = call_count["n"]
            await router.route("What is X?", session_context=ctx)
            self.assertEqual(call_count["n"], first_count, "Second call should use cache")

        asyncio.run(run())

    def test_different_query_misses_cache(self):
        call_count = {"n": 0}

        async def mock_llm(prompt):
            call_count["n"] += 1
            return '{"intent_type": "quick_lookup", "top_k": 3}'

        router = IntentRouter(llm_completion=mock_llm)

        async def run():
            ctx = {"summary": "test"}
            await router.route("What is X?", session_context=ctx)
            await router.route("What is Y?", session_context=ctx)
            self.assertEqual(call_count["n"], 2, "Different queries should both call LLM")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
