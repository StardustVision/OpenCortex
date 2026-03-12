"""Test parser base classes and utilities."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.parse.base import ParsedChunk, format_table_to_markdown, lazy_import, ParserConfig


class TestParsedChunk(unittest.TestCase):
    def test_create_chunk(self):
        chunk = ParsedChunk(
            content="Hello world", title="Intro", level=1,
            parent_index=-1, source_format="markdown", meta={},
        )
        self.assertEqual(chunk.content, "Hello world")
        self.assertEqual(chunk.title, "Intro")
        self.assertEqual(chunk.level, 1)
        self.assertEqual(chunk.parent_index, -1)

    def test_chunk_defaults(self):
        chunk = ParsedChunk(content="text", title="", level=0, parent_index=-1, source_format="text")
        self.assertEqual(chunk.meta, {})


class TestFormatTable(unittest.TestCase):
    def test_simple_table(self):
        rows = [["Name", "Age"], ["Alice", "30"], ["Bob", "25"]]
        result = format_table_to_markdown(rows)
        self.assertIn("| Name", result)
        self.assertIn("| ---", result)
        self.assertIn("| Alice", result)

    def test_empty_table(self):
        self.assertEqual(format_table_to_markdown([]), "")


class TestParserConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = ParserConfig()
        self.assertEqual(cfg.max_section_size, 1024)
        self.assertEqual(cfg.min_section_tokens, 512)

    def test_custom(self):
        cfg = ParserConfig(max_section_size=2048, min_section_tokens=256)
        self.assertEqual(cfg.max_section_size, 2048)


class TestLazyImport(unittest.TestCase):
    def test_existing_module(self):
        mod = lazy_import("os")
        self.assertTrue(hasattr(mod, "path"))

    def test_missing_module(self):
        with self.assertRaises(ImportError):
            lazy_import("nonexistent_module_xyz_12345")


if __name__ == "__main__":
    unittest.main()
