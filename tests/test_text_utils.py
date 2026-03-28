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


import asyncio


class TestChunkedLLMDerive(unittest.TestCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def test_single_chunk_passthrough(self):
        from opencortex.utils.text import chunked_llm_derive

        async def mock_llm(prompt):
            return '{"abstract": "Sum", "overview": "Over", "keywords": ["k1"]}'

        def mock_parse(response):
            import json
            return json.loads(response)

        result = self._run(chunked_llm_derive(
            content="Short content",
            prompt_builder=lambda c: f"Summarize: {c}",
            llm_fn=mock_llm,
            parse_fn=mock_parse,
            max_chars_per_chunk=3000,
        ))
        self.assertEqual(result["abstract"], "Sum")
        self.assertEqual(result["overview"], "Over")
        self.assertEqual(result["keywords"], ["k1"])

    def test_multi_chunk_merges_keywords(self):
        from opencortex.utils.text import chunked_llm_derive

        call_count = 0
        async def mock_llm(prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return '{"abstract": "First", "overview": "Overview 1.", "keywords": ["a", "b"]}'
            elif call_count == 2:
                return '{"abstract": "Second", "overview": "Overview 2.", "keywords": ["b", "c"]}'
            # Compression call for overview
            return '{"abstract": "Compressed", "overview": "Compressed overview.", "keywords": []}'

        def mock_parse(response):
            import json
            return json.loads(response)

        text = "Para one content.\n\n" + "Para two content."
        result = self._run(chunked_llm_derive(
            content=text,
            prompt_builder=lambda c: f"Summarize: {c}",
            llm_fn=mock_llm,
            parse_fn=mock_parse,
            max_chars_per_chunk=20,
        ))
        self.assertEqual(result["abstract"], "First")
        self.assertIn("a", result["keywords"])
        self.assertIn("b", result["keywords"])
        self.assertIn("c", result["keywords"])

    def test_abstract_overview_merge_policy(self):
        from opencortex.utils.text import chunked_llm_derive

        async def mock_llm(prompt):
            if "compress" in prompt.lower() or "Compress" in prompt:
                return '{"abstract": "compressed abs", "overview": "compressed over"}'
            return '{"abstract": "abs", "overview": "over"}'

        def mock_parse(response):
            import json
            return json.loads(response)

        text = "Chunk one.\n\nChunk two."
        result = self._run(chunked_llm_derive(
            content=text,
            prompt_builder=lambda c: f"Summarize: {c}",
            llm_fn=mock_llm,
            parse_fn=mock_parse,
            merge_policy="abstract_overview",
            max_chars_per_chunk=15,
        ))
        self.assertIn("abstract", result)
        self.assertIn("overview", result)
        self.assertNotIn("keywords", result)


if __name__ == "__main__":
    unittest.main()
