"""Test session-aware routing: zero LLM when no session context."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.retrieve.intent_router import IntentRouter
from opencortex.retrieve.types import ContextType


class TestSessionAwareRouting(unittest.TestCase):
    def test_no_session_skips_llm(self):
        llm_called = {"count": 0}

        async def mock_llm(prompt):
            llm_called["count"] += 1
            return '{"intent_type": "quick_lookup", "top_k": 5}'

        router = IntentRouter(llm_completion=mock_llm)

        async def run():
            intent = await router.route("What is authentication?", session_context=None)
            self.assertEqual(llm_called["count"], 0, "LLM should not be called without session context")
            self.assertTrue(len(intent.queries) > 0, "Should still produce queries")

        asyncio.run(run())

    def test_with_session_calls_llm(self):
        llm_called = {"count": 0}

        async def mock_llm(prompt):
            llm_called["count"] += 1
            return '{"intent_type": "deep_analysis", "top_k": 10, "queries": [{"query": "auth middleware", "context_type": "any", "intent": "lookup"}]}'

        router = IntentRouter(llm_completion=mock_llm)

        async def run():
            session_ctx = {
                "summary": "Discussion about authentication system",
                "recent_messages": ["We talked about JWT tokens"],
            }
            intent = await router.route("What about the middleware?", session_context=session_ctx)
            self.assertGreater(llm_called["count"], 0, "LLM should be called with session context")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
