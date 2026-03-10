import json
import unittest
from opencortex.alpha.trace_splitter import TraceSplitter


class TestTraceSplitter(unittest.IsolatedAsyncioTestCase):

    def _make_messages(self, count):
        messages = []
        for i in range(count):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append({"role": role, "content": f"message {i}"})
        return messages

    async def test_split_single_task_session(self):
        """Session with one task produces one trace."""
        async def mock_llm(prompt):
            return json.dumps([{
                "summary": "Fixed a bug",
                "key_steps": ["Read error", "Applied fix"],
                "turn_indices": [0, 1],
                "outcome": "success",
                "task_type": "debug",
            }])

        splitter = TraceSplitter(llm_fn=mock_llm)
        messages = self._make_messages(2)
        traces = await splitter.split(
            messages, session_id="s1",
            tenant_id="team", user_id="hugo",
        )
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].abstract, "Fixed a bug")
        self.assertEqual(traces[0].session_id, "s1")
        self.assertIsNotNone(traces[0].overview)

    async def test_split_multi_task_session(self):
        """Session with two tasks produces two traces."""
        async def mock_llm(prompt):
            return json.dumps([
                {
                    "summary": "Fixed auth bug",
                    "key_steps": ["Checked logs"],
                    "turn_indices": [0, 1],
                    "outcome": "success",
                    "task_type": "debug",
                },
                {
                    "summary": "Added tests",
                    "key_steps": ["Wrote test"],
                    "turn_indices": [2, 3],
                    "outcome": "success",
                    "task_type": "coding",
                },
            ])

        splitter = TraceSplitter(llm_fn=mock_llm)
        messages = self._make_messages(4)
        traces = await splitter.split(
            messages, session_id="s1",
            tenant_id="team", user_id="hugo",
        )
        self.assertEqual(len(traces), 2)
        self.assertEqual(traces[0].abstract, "Fixed auth bug")
        self.assertEqual(traces[1].abstract, "Added tests")

    async def test_empty_session(self):
        """Empty session produces no traces."""
        async def mock_llm(prompt):
            return "[]"

        splitter = TraceSplitter(llm_fn=mock_llm)
        traces = await splitter.split(
            [], session_id="s1",
            tenant_id="team", user_id="hugo",
        )
        self.assertEqual(len(traces), 0)

    async def test_llm_error_fallback(self):
        """LLM error produces single fallback trace."""
        async def mock_llm(prompt):
            return "not valid json!!"

        splitter = TraceSplitter(llm_fn=mock_llm)
        messages = self._make_messages(4)
        traces = await splitter.split(
            messages, session_id="s1",
            tenant_id="team", user_id="hugo",
        )
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].abstract, "Full session")

    async def test_sliding_window_long_session(self):
        """Session exceeding context triggers windowed splitting."""
        call_count = 0

        async def mock_llm(prompt):
            nonlocal call_count
            call_count += 1
            return json.dumps([{
                "summary": f"Task from window {call_count}",
                "key_steps": ["step"],
                "turn_indices": [0, 1],
                "outcome": "success",
                "task_type": "coding",
            }])

        # Small context to force windowing (each message ~30 chars, need room for context prefix)
        splitter = TraceSplitter(
            llm_fn=mock_llm,
            max_context_tokens=100,
            chars_per_token=1,
        )
        messages = self._make_messages(20)
        traces = await splitter.split(
            messages, session_id="s1",
            tenant_id="team", user_id="hugo",
        )
        self.assertGreater(call_count, 1, "Should have made multiple LLM calls")
        self.assertGreater(len(traces), 1, "Should have multiple traces from windows")

    async def test_trace_outcome_mapping(self):
        """Outcome string maps to TraceOutcome enum."""
        from opencortex.alpha.types import TraceOutcome

        async def mock_llm(prompt):
            return json.dumps([{
                "summary": "Failed task",
                "key_steps": [],
                "turn_indices": [0],
                "outcome": "failure",
                "task_type": "debug",
            }])

        splitter = TraceSplitter(llm_fn=mock_llm)
        messages = [{"role": "user", "content": "help"}]
        traces = await splitter.split(
            messages, session_id="s1",
            tenant_id="team", user_id="hugo",
        )
        self.assertEqual(traces[0].outcome, TraceOutcome.FAILURE)


if __name__ == "__main__":
    unittest.main()
