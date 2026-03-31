"""Tests for multi-clauding sliding window detection."""
import unittest
from opencortex.insights.multi_clauding import detect_multi_clauding
from opencortex.insights.types import SessionMeta


def _make_meta(sid: str, timestamps: list) -> SessionMeta:
    return SessionMeta(
        session_id=sid, tenant_id="t", user_id="u",
        project_path="", start_time="", duration_minutes=0,
        user_message_count=len(timestamps), assistant_message_count=0,
        tool_counts={}, languages={},
        git_commits=0, git_pushes=0,
        input_tokens=0, output_tokens=0, first_prompt="",
        user_message_timestamps=timestamps,
    )


class TestMultiClauding(unittest.TestCase):
    def test_no_overlap(self):
        """Two sessions that don't overlap → no multi-clauding."""
        s1 = _make_meta("s1", ["2026-03-31T10:00:00", "2026-03-31T10:05:00"])
        s2 = _make_meta("s2", ["2026-03-31T11:00:00", "2026-03-31T11:05:00"])
        result = detect_multi_clauding([s1, s2])
        self.assertEqual(result["overlap_events"], 0)
        self.assertEqual(result["sessions_involved"], 0)

    def test_interleaved_sessions(self):
        """s1 → s2 → s1 within 30 min → one overlap event."""
        s1 = _make_meta("s1", ["2026-03-31T10:00:00", "2026-03-31T10:10:00"])
        s2 = _make_meta("s2", ["2026-03-31T10:05:00"])
        result = detect_multi_clauding([s1, s2])
        self.assertGreaterEqual(result["overlap_events"], 1)
        self.assertEqual(result["sessions_involved"], 2)
        self.assertGreater(result["user_messages_during"], 0)

    def test_single_session(self):
        """Single session cannot multi-claude."""
        s1 = _make_meta("s1", ["2026-03-31T10:00:00", "2026-03-31T10:05:00"])
        result = detect_multi_clauding([s1])
        self.assertEqual(result["overlap_events"], 0)

    def test_empty(self):
        result = detect_multi_clauding([])
        self.assertEqual(result["overlap_events"], 0)
        self.assertEqual(result["sessions_involved"], 0)
        self.assertEqual(result["user_messages_during"], 0)

    def test_outside_window(self):
        """s1 → s2 → s1 but >30 min apart → no overlap."""
        s1 = _make_meta("s1", ["2026-03-31T10:00:00", "2026-03-31T11:00:00"])
        s2 = _make_meta("s2", ["2026-03-31T10:35:00"])
        result = detect_multi_clauding([s1, s2])
        self.assertEqual(result["overlap_events"], 0)


if __name__ == "__main__":
    unittest.main()
