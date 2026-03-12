"""Test MarkdownParser chunking and hierarchy."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.parse.parsers.markdown import MarkdownParser


class TestMarkdownParser(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_small_doc_single_chunk(self):
        parser = MarkdownParser()
        content = "# Title\n\nShort document content."
        chunks = self._run(parser.parse_content(content))
        self.assertEqual(len(chunks), 1)
        self.assertIn("Short document content", chunks[0].content)

    def test_heading_split(self):
        parser = MarkdownParser()
        section_text = "word " * 1500  # ~2250 tokens each, total ~6750 > 4000 threshold
        content = f"# Introduction\n\n{section_text}\n\n# Methods\n\n{section_text}\n\n# Results\n\n{section_text}"
        chunks = self._run(parser.parse_content(content))
        self.assertGreater(len(chunks), 1)
        titles = [c.title for c in chunks if c.title]
        self.assertTrue(any("Introduction" in t for t in titles))
        self.assertTrue(any("Methods" in t for t in titles))

    def test_parent_index_hierarchy(self):
        parser = MarkdownParser()
        long = "content " * 1500  # ~3600 tokens each, total ~10800 > 4000 threshold
        content = f"# Top\n\n{long}\n\n## Sub1\n\n{long}\n\n## Sub2\n\n{long}"
        chunks = self._run(parser.parse_content(content))
        has_child = any(c.parent_index >= 0 for c in chunks)
        self.assertTrue(has_child, "Should have parent-child relationships")

    def test_small_sections_merged(self):
        parser = MarkdownParser()
        sections = "\n\n".join(f"# Section {i}\n\nTiny." for i in range(10))
        padding = "word " * 2500
        content = f"{sections}\n\n# Big Section\n\n{padding}"
        chunks = self._run(parser.parse_content(content))
        self.assertLess(len(chunks), 11)

    def test_source_format(self):
        parser = MarkdownParser()
        long = "content " * 2500
        content = f"# A\n\n{long}\n\n# B\n\n{long}"
        chunks = self._run(parser.parse_content(content))
        for c in chunks:
            self.assertEqual(c.source_format, "markdown")

    def test_parse_file(self):
        import tempfile
        parser = MarkdownParser()
        long = "content " * 2500
        content = f"# Hello\n\n{long}\n\n# World\n\n{long}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            f.flush()
            chunks = self._run(parser.parse(f.name))
        self.assertGreater(len(chunks), 0)
        os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
