"""Test conversation merge layer: buffer accumulation + threshold merge."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestConversationBuffer(unittest.TestCase):
    def test_buffer_dataclass(self):
        from opencortex.context.manager import ConversationBuffer
        buf = ConversationBuffer()
        self.assertEqual(buf.messages, [])
        self.assertEqual(buf.token_count, 0)
        self.assertEqual(buf.start_msg_index, 0)
        self.assertEqual(buf.immediate_uris, [])

    def test_buffer_accumulates(self):
        from opencortex.context.manager import ConversationBuffer
        buf = ConversationBuffer()
        buf.messages.append("Hello world")
        buf.token_count += 100
        buf.immediate_uris.append("opencortex://test/uri")
        self.assertEqual(len(buf.messages), 1)
        self.assertEqual(buf.token_count, 100)


if __name__ == "__main__":
    unittest.main()
