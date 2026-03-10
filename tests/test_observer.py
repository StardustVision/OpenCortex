import unittest
from opencortex.alpha.observer import Observer


class TestObserver(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.observer = Observer()

    async def test_record_message(self):
        self.observer.record_message(
            session_id="s1", role="user", content="fix the bug",
            tenant_id="team", user_id="hugo",
        )
        transcript = self.observer.get_transcript("s1")
        self.assertEqual(len(transcript), 1)
        self.assertEqual(transcript[0]["role"], "user")
        self.assertEqual(transcript[0]["content"], "fix the bug")

    async def test_record_batch(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        self.observer.record_batch(
            session_id="s1", messages=messages,
            tenant_id="team", user_id="hugo",
        )
        transcript = self.observer.get_transcript("s1")
        self.assertEqual(len(transcript), 2)

    async def test_multiple_sessions(self):
        self.observer.record_message("s1", "user", "a", "t", "u1")
        self.observer.record_message("s2", "user", "b", "t", "u2")
        self.assertEqual(len(self.observer.get_transcript("s1")), 1)
        self.assertEqual(len(self.observer.get_transcript("s2")), 1)

    async def test_get_empty_transcript(self):
        transcript = self.observer.get_transcript("nonexistent")
        self.assertEqual(transcript, [])

    async def test_flush_clears_session(self):
        self.observer.record_message("s1", "user", "x", "t", "u")
        transcript = self.observer.flush("s1")
        self.assertEqual(len(transcript), 1)
        self.assertEqual(self.observer.get_transcript("s1"), [])

    async def test_session_meta(self):
        self.observer.begin_session(
            session_id="s1", tenant_id="team", user_id="hugo",
            meta={"source": "claude_code"},
        )
        meta = self.observer.get_session_meta("s1")
        self.assertEqual(meta["source"], "claude_code")


if __name__ == "__main__":
    unittest.main()
