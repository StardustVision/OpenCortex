"""Test ParserRegistry extension-based dispatch."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.parse.registry import ParserRegistry


class TestParserRegistry(unittest.TestCase):
    def test_markdown_dispatch(self):
        registry = ParserRegistry()
        parser = registry.get_parser_for_file("report.md")
        self.assertIsNotNone(parser)

    def test_txt_dispatch(self):
        registry = ParserRegistry()
        parser = registry.get_parser_for_file("readme.txt")
        self.assertIsNotNone(parser)

    def test_pdf_dispatch(self):
        registry = ParserRegistry()
        parser = registry.get_parser_for_file("document.pdf")
        if parser:
            self.assertIn(".pdf", parser.supported_extensions)

    def test_docx_dispatch(self):
        registry = ParserRegistry()
        parser = registry.get_parser_for_file("report.docx")
        if parser:
            self.assertIn(".docx", parser.supported_extensions)

    def test_xlsx_dispatch(self):
        registry = ParserRegistry()
        parser = registry.get_parser_for_file("data.xlsx")
        if parser:
            self.assertIn(".xlsx", parser.supported_extensions)

    def test_unknown_returns_none(self):
        registry = ParserRegistry()
        parser = registry.get_parser_for_file("image.png")
        self.assertIsNone(parser)

    def test_list_extensions(self):
        registry = ParserRegistry()
        exts = registry.list_supported_extensions()
        self.assertIn(".md", exts)
        self.assertIn(".txt", exts)

    def test_parse_markdown_content(self):
        registry = ParserRegistry()
        chunks = asyncio.run(
            registry.parse_content("# Hello\n\nWorld", source_format="markdown")
        )
        self.assertGreater(len(chunks), 0)


if __name__ == "__main__":
    unittest.main()
