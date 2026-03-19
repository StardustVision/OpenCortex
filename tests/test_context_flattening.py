import unittest


class TestContextFlatteningLogic(unittest.TestCase):
    def test_document_embed_text_format(self):
        title = "My Paper"
        section = "Introduction > Background"
        abstract = "Some important findings."
        parts = []
        if title:
            parts.append(f"[{title}]")
        if section:
            parts.append(f"[{section}]")
        parts.append(abstract)
        embed_text = " ".join(parts)
        self.assertIn("[My Paper]", embed_text)
        self.assertIn("[Introduction > Background]", embed_text)
        self.assertTrue(embed_text.endswith(abstract))

    def test_conversation_embed_text_format(self):
        text = "user: What is the capital of France?"
        speaker = text.split(":", 1)[0] if ":" in text else ""
        parts = []
        if speaker:
            parts.append(f"[{speaker}]")
        parts.append(text)
        embed_text = " ".join(parts)
        self.assertIn("[user]", embed_text)

    def test_empty_title_skipped(self):
        title = ""
        abstract = "Some text."
        parts = []
        if title:
            parts.append(f"[{title}]")
        parts.append(abstract)
        embed_text = " ".join(parts)
        self.assertNotIn("[]", embed_text)
        self.assertEqual(embed_text, abstract)
