"""Tests for the event noise reduction pipeline (tool_calls three-way split)."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.alpha.observer import Observer


class TestObserverToolCalls(unittest.TestCase):
    """Observer.record_batch must preserve tool_calls in transcript."""

    def test_record_batch_with_tool_calls(self):
        obs = Observer()
        obs.begin_session("s1", "team", "user")
        messages = [
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "Fixed the selection logic."},
        ]
        tool_calls = [
            {"name": "Read", "summary": "Memories.tsx"},
            {"name": "Edit", "summary": "modified useEffect"},
        ]
        obs.record_batch("s1", messages, "team", "user", tool_calls=tool_calls)

        transcript = obs.get_transcript("s1")
        self.assertEqual(len(transcript), 2)
        self.assertNotIn("tool_calls", transcript[0])
        self.assertIn("tool_calls", transcript[1])
        self.assertEqual(len(transcript[1]["tool_calls"]), 2)
        self.assertEqual(transcript[1]["tool_calls"][0]["name"], "Read")

    def test_record_batch_without_tool_calls_backward_compat(self):
        obs = Observer()
        obs.begin_session("s2", "team", "user")
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        obs.record_batch("s2", messages, "team", "user")

        transcript = obs.get_transcript("s2")
        self.assertEqual(len(transcript), 2)
        self.assertNotIn("tool_calls", transcript[0])
        self.assertNotIn("tool_calls", transcript[1])
