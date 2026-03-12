"""Test multi-query concurrent retrieval from LLM intent analysis."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.retrieve.intent_router import IntentRouter


class TestMultiQueryConcurrent(unittest.TestCase):
    def test_llm_produces_multiple_queries(self):
        async def mock_llm(prompt):
            return '{"intent_type": "deep_analysis", "top_k": 10, "queries": [{"query": "auth middleware", "context_type": "any", "intent": "lookup"}, {"query": "JWT token validation", "context_type": "any", "intent": "lookup"}]}'

        router = IntentRouter(llm_completion=mock_llm)

        async def run():
            session_ctx = {"summary": "Discussion about auth"}
            intent = await router.route("Tell me about auth middleware and JWT", session_context=session_ctx)
            self.assertGreater(len(intent.queries), 1, "Should produce multiple queries from LLM")

        asyncio.run(run())

    def test_no_session_produces_single_query(self):
        router = IntentRouter(llm_completion=None)

        async def run():
            intent = await router.route("What is authentication?", session_context=None)
            self.assertEqual(len(intent.queries), 1, "No-session should produce single query")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
