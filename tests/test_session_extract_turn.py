# SPDX-License-Identifier: Apache-2.0
"""Tests for SessionManager.extract_turn() — mid-session memory extraction."""

import asyncio
import unittest
from unittest.mock import AsyncMock


class TestExtractTurn(unittest.TestCase):

    def test_extract_turn_returns_extraction_result(self):
        async def _test():
            from opencortex.session.manager import SessionManager
            from opencortex.session.types import ExtractionResult

            llm = AsyncMock(return_value='[{"abstract": "User likes dark mode", "content": "Explicit preference", "category": "preferences", "context_type": "memory", "confidence": 0.9}]')
            store = AsyncMock()
            store.return_value = type("Ctx", (), {"meta": {"dedup_action": "created"}})()

            mgr = SessionManager(llm_completion=llm, store_fn=store)
            await mgr.begin("s1")
            await mgr.add_message("s1", "user", "I prefer dark mode")
            await mgr.add_message("s1", "assistant", "Noted, dark mode it is")

            result = await mgr.extract_turn("s1")
            self.assertIsInstance(result, ExtractionResult)
            self.assertGreater(result.stored_count + result.merged_count + result.skipped_count, 0)

        asyncio.run(_test())

    def test_extract_turn_no_session(self):
        async def _test():
            from opencortex.session.manager import SessionManager
            from opencortex.session.types import ExtractionResult

            mgr = SessionManager()
            result = await mgr.extract_turn("nonexistent")
            self.assertIsInstance(result, ExtractionResult)
            self.assertEqual(result.stored_count, 0)

        asyncio.run(_test())

    def test_extract_turn_does_not_remove_session(self):
        """extract_turn must keep the session alive, unlike end()."""
        async def _test():
            from opencortex.session.manager import SessionManager

            llm = AsyncMock(return_value='[]')
            mgr = SessionManager(llm_completion=llm)
            await mgr.begin("s1")
            await mgr.add_message("s1", "user", "hello")
            await mgr.add_message("s1", "assistant", "hi")

            await mgr.extract_turn("s1")
            # Session should still be active
            self.assertIsNotNone(mgr.get_session("s1"))
            self.assertIn("s1", mgr.active_sessions())

        asyncio.run(_test())

    def test_extract_turn_uses_last_two_messages(self):
        """extract_turn should only pass the last 2 messages to the extractor."""
        async def _test():
            from opencortex.session.manager import SessionManager

            llm = AsyncMock(return_value='[]')
            mgr = SessionManager(llm_completion=llm)
            await mgr.begin("s1")
            await mgr.add_message("s1", "user", "first message")
            await mgr.add_message("s1", "assistant", "first reply")
            await mgr.add_message("s1", "user", "second message")
            await mgr.add_message("s1", "assistant", "second reply")

            await mgr.extract_turn("s1")

            # The LLM should have been called with a prompt containing only
            # the last 2 messages, not all 4.
            call_args = llm.call_args[0][0]
            self.assertIn("second message", call_args)
            self.assertIn("second reply", call_args)
            # The first messages should NOT be in the extraction prompt
            self.assertNotIn("first message", call_args)
            self.assertNotIn("first reply", call_args)

        asyncio.run(_test())

    def test_extract_turn_no_extractor(self):
        """Without an LLM, extract_turn should return empty result."""
        async def _test():
            from opencortex.session.manager import SessionManager
            from opencortex.session.types import ExtractionResult

            mgr = SessionManager()  # no llm_completion
            await mgr.begin("s1")
            await mgr.add_message("s1", "user", "hello")
            await mgr.add_message("s1", "assistant", "hi")

            result = await mgr.extract_turn("s1")
            self.assertIsInstance(result, ExtractionResult)
            self.assertEqual(result.stored_count, 0)
            self.assertEqual(result.merged_count, 0)
            self.assertEqual(result.skipped_count, 0)

        asyncio.run(_test())

    def test_extract_turn_low_confidence_skipped(self):
        """Memories below _MIN_CONFIDENCE (0.3) should be skipped."""
        async def _test():
            from opencortex.session.manager import SessionManager

            llm = AsyncMock(return_value='[{"abstract": "Low conf", "content": "test", "category": "general", "context_type": "memory", "confidence": 0.1}]')
            store = AsyncMock()

            mgr = SessionManager(llm_completion=llm, store_fn=store)
            await mgr.begin("s1")
            await mgr.add_message("s1", "user", "something")
            await mgr.add_message("s1", "assistant", "ok")

            result = await mgr.extract_turn("s1")
            self.assertEqual(result.stored_count, 0)
            self.assertGreater(result.skipped_count, 0)
            store.assert_not_called()

        asyncio.run(_test())


if __name__ == "__main__":
    unittest.main()
