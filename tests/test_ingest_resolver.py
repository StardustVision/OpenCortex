"""Test IngestModeResolver routing logic."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.ingest.resolver import IngestModeResolver


class TestIngestModeResolver(unittest.TestCase):

    def test_explicit_mode_wins(self):
        r = IngestModeResolver.resolve(content="short text", meta={"ingest_mode": "document"})
        self.assertEqual(r, "document")

    def test_source_path_implies_document(self):
        r = IngestModeResolver.resolve(content="some content", source_path="/tmp/report.pdf")
        self.assertEqual(r, "document")

    def test_scan_meta_implies_document(self):
        r = IngestModeResolver.resolve(content="code content", scan_meta={"has_git": True})
        self.assertEqual(r, "document")

    def test_session_id_implies_conversation(self):
        r = IngestModeResolver.resolve(content="User: Hello\nAssistant: Hi", session_id="sess-123")
        self.assertEqual(r, "conversation")

    def test_dialog_pattern_implies_conversation(self):
        r = IngestModeResolver.resolve(content="User: What is the weather?\nAssistant: It's sunny today.\nUser: Thanks!")
        self.assertEqual(r, "conversation")

    def test_long_headed_content_implies_document(self):
        content = "# Introduction\n" + "x " * 7000 + "\n## Methods\n" + "y " * 7000
        r = IngestModeResolver.resolve(content=content)
        self.assertEqual(r, "document")

    def test_short_content_defaults_memory(self):
        r = IngestModeResolver.resolve(content="The user prefers dark mode.")
        self.assertEqual(r, "memory")

    def test_empty_content_defaults_memory(self):
        r = IngestModeResolver.resolve(content="")
        self.assertEqual(r, "memory")

    def test_explicit_memory_overrides_patterns(self):
        r = IngestModeResolver.resolve(content="User: Hello\nAssistant: Hi", meta={"ingest_mode": "memory"})
        self.assertEqual(r, "memory")

    def test_batch_store_implies_document(self):
        r = IngestModeResolver.resolve(content="file content", is_batch=True)
        self.assertEqual(r, "document")


if __name__ == "__main__":
    unittest.main()
