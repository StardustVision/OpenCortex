"""Tests for SessionMetaExtractor — pure-code metric extraction."""
import unittest
from opencortex.alpha.types import Turn, Trace, TurnStatus
from opencortex.insights.extractor import SessionMetaExtractor


def _make_trace(turns: list, session_id: str = "s1") -> Trace:
    return Trace(
        trace_id="tr1", session_id=session_id,
        tenant_id="t1", user_id="u1", source="claude_code",
        turns=turns, created_at="2026-03-31T10:00:00+00:00",
    )


class TestSessionMetaExtractor(unittest.TestCase):
    def setUp(self):
        self.extractor = SessionMetaExtractor()

    def test_tool_counting(self):
        turns = [
            Turn(turn_id="1", tool_calls=[
                {"name": "Read", "summary": "read file"},
                {"name": "Edit", "summary": "edit file", "input_params": {"file_path": "/src/app.py"}},
            ]),
            Turn(turn_id="2", tool_calls=[
                {"name": "Read", "summary": "read another"},
            ]),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.tool_counts["Read"], 2)
        self.assertEqual(meta.tool_counts["Edit"], 1)

    def test_language_detection(self):
        turns = [
            Turn(turn_id="1", tool_calls=[
                {"name": "Edit", "input_params": {"file_path": "/src/app.py"}},
                {"name": "Edit", "input_params": {"file_path": "/src/index.ts"}},
                {"name": "Edit", "input_params": {"file_path": "/src/utils.ts"}},
            ]),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.languages["Python"], 1)
        self.assertEqual(meta.languages["TypeScript"], 2)

    def test_git_detection(self):
        turns = [
            Turn(turn_id="1", tool_calls=[
                {"name": "Bash", "input_params": {"command": "git commit -m 'fix'"}},
                {"name": "Bash", "input_params": {"command": "git push origin main"}},
                {"name": "Bash", "input_params": {"command": "ls -la"}},
            ]),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.git_commits, 1)
        self.assertEqual(meta.git_pushes, 1)

    def test_error_classification(self):
        turns = [
            Turn(turn_id="1", tool_calls=[
                {"name": "Bash", "is_error": True, "error_text": "exit code 1"},
                {"name": "Edit", "is_error": True, "error_text": "string to replace not found"},
                {"name": "Read", "is_error": True, "error_text": "file not found: /missing.py"},
            ]),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.tool_errors, 3)
        self.assertEqual(meta.tool_error_categories["Command Failed"], 1)
        self.assertEqual(meta.tool_error_categories["Edit Failed"], 1)
        self.assertEqual(meta.tool_error_categories["File Not Found"], 1)

    def test_special_tool_detection(self):
        turns = [
            Turn(turn_id="1", tool_calls=[
                {"name": "Agent", "summary": "sub-agent"},
                {"name": "mcp__memory_store", "summary": "store"},
                {"name": "WebSearch", "summary": "search"},
                {"name": "WebFetch", "summary": "fetch"},
            ]),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertTrue(meta.uses_agent)
        self.assertTrue(meta.uses_mcp)
        self.assertTrue(meta.uses_web_search)
        self.assertTrue(meta.uses_web_fetch)

    def test_user_interruption_count(self):
        turns = [
            Turn(turn_id="1", turn_status=TurnStatus.INTERRUPTED),
            Turn(turn_id="2", turn_status=TurnStatus.COMPLETE),
            Turn(turn_id="3", turn_status=TurnStatus.INTERRUPTED),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.user_interruptions, 2)

    def test_message_counting(self):
        turns = [
            Turn(turn_id="1", prompt_text="Hello"),
            Turn(turn_id="2", prompt_text="Fix bug", final_text="Done"),
            Turn(turn_id="3", final_text="Here's the result"),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.user_message_count, 2)
        self.assertEqual(meta.assistant_message_count, 2)

    def test_files_modified(self):
        turns = [
            Turn(turn_id="1", tool_calls=[
                {"name": "Edit", "input_params": {"file_path": "/a.py"}},
                {"name": "Write", "input_params": {"file_path": "/b.py"}},
                {"name": "Edit", "input_params": {"file_path": "/a.py"}},
            ]),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.files_modified, 2)

    def test_first_prompt(self):
        turns = [
            Turn(turn_id="1", prompt_text="Fix the authentication bug in login.py"),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.first_prompt, "Fix the authentication bug in login.py")

    def test_empty_trace(self):
        meta = self.extractor.extract(_make_trace([]))
        self.assertEqual(meta.tool_counts, {})
        self.assertEqual(meta.user_message_count, 0)
        self.assertEqual(meta.first_prompt, "")


if __name__ == "__main__":
    unittest.main()
