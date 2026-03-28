"""Tests for smart_truncate and smart_split utilities."""
import unittest


class TestSmartTruncate(unittest.TestCase):

    def test_short_text_unchanged(self):
        from opencortex.utils.text import smart_truncate
        self.assertEqual(smart_truncate("hello", 100), "hello")

    def test_empty_text(self):
        from opencortex.utils.text import smart_truncate
        self.assertEqual(smart_truncate("", 100), "")

    def test_truncate_at_paragraph_boundary(self):
        from opencortex.utils.text import smart_truncate
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        result = smart_truncate(text, 40)
        self.assertEqual(result, "First paragraph.\n\nSecond paragraph.")

    def test_truncate_at_line_boundary(self):
        from opencortex.utils.text import smart_truncate
        text = "Line one.\nLine two.\nLine three is long."
        result = smart_truncate(text, 25)
        self.assertEqual(result, "Line one.\nLine two.")

    def test_truncate_at_sentence_boundary(self):
        from opencortex.utils.text import smart_truncate
        text = "First sentence. Second sentence. Third sentence."
        result = smart_truncate(text, 35)
        self.assertEqual(result, "First sentence. Second sentence.")

    def test_truncate_at_word_boundary(self):
        from opencortex.utils.text import smart_truncate
        text = "one two three four five six seven"
        result = smart_truncate(text, 20)
        self.assertLessEqual(len(result), 20)
        self.assertFalse(result.endswith(" "))

    def test_guarantee_max_chars(self):
        from opencortex.utils.text import smart_truncate
        text = "a" * 500
        result = smart_truncate(text, 100)
        self.assertLessEqual(len(result), 100)

    def test_exactly_at_limit(self):
        from opencortex.utils.text import smart_truncate
        text = "Exact."
        result = smart_truncate(text, 6)
        self.assertEqual(result, "Exact.")

    def test_chinese_text(self):
        from opencortex.utils.text import smart_truncate
        text = "第一段话。\n\n第二段话。\n\n第三段话。"
        result = smart_truncate(text, 15)
        self.assertLessEqual(len(result), 15)


class TestSmartSplit(unittest.TestCase):

    def test_short_text_single_chunk(self):
        from opencortex.utils.text import smart_split
        self.assertEqual(smart_split("hello", 100), ["hello"])

    def test_split_at_paragraphs(self):
        from opencortex.utils.text import smart_split
        text = "Para one.\n\nPara two.\n\nPara three."
        chunks = smart_split(text, 20)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 20)
        self.assertEqual("\n\n".join(chunks), text)

    def test_no_content_loss(self):
        from opencortex.utils.text import smart_split
        text = "A" * 50 + "\n\n" + "B" * 50 + "\n\n" + "C" * 50
        chunks = smart_split(text, 60)
        rejoined = "\n\n".join(chunks)
        self.assertEqual(rejoined, text)

    def test_single_long_paragraph(self):
        from opencortex.utils.text import smart_split
        text = "word " * 100  # 500 chars
        chunks = smart_split(text, 60)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 60)
        self.assertTrue(len(chunks) > 1)

    def test_empty_text(self):
        from opencortex.utils.text import smart_split
        self.assertEqual(smart_split("", 100), [""])


if __name__ == "__main__":
    unittest.main()
