"""Test delegate parsers."""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.parse.parsers.text import TextParser


class TestTextParser(unittest.TestCase):
    def test_parse_plain_text(self):
        parser = TextParser()
        content = "Just plain text content.\n\nAnother paragraph."
        chunks = asyncio.run(parser.parse_content(content))
        self.assertGreater(len(chunks), 0)
        self.assertEqual(chunks[0].source_format, "text")

    def test_supported_extensions(self):
        parser = TextParser()
        self.assertIn(".txt", parser.supported_extensions)

    def test_parse_file(self):
        parser = TextParser()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Hello world.")
            f.flush()
            chunks = asyncio.run(parser.parse(f.name))
        self.assertGreater(len(chunks), 0)
        os.unlink(f.name)


class TestWordParser(unittest.TestCase):
    def test_supported_extensions(self):
        from opencortex.parse.parsers.word import WordParser
        parser = WordParser()
        self.assertIn(".docx", parser.supported_extensions)


class TestExcelParser(unittest.TestCase):
    def test_supported_extensions(self):
        from opencortex.parse.parsers.excel import ExcelParser
        parser = ExcelParser()
        self.assertIn(".xlsx", parser.supported_extensions)


class TestPowerPointParser(unittest.TestCase):
    def test_supported_extensions(self):
        from opencortex.parse.parsers.powerpoint import PowerPointParser
        parser = PowerPointParser()
        self.assertIn(".pptx", parser.supported_extensions)


class TestPDFParser(unittest.TestCase):
    def test_supported_extensions(self):
        from opencortex.parse.parsers.pdf import PDFParser
        parser = PDFParser()
        self.assertIn(".pdf", parser.supported_extensions)


class TestEPubParser(unittest.TestCase):
    def test_supported_extensions(self):
        from opencortex.parse.parsers.epub import EPubParser
        parser = EPubParser()
        self.assertIn(".epub", parser.supported_extensions)


if __name__ == "__main__":
    unittest.main()
