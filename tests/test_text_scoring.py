"""Tests for lexical text scoring helpers."""
import unittest


class TestTokenizeForScoring(unittest.TestCase):
    def test_english_words(self):
        from opencortex.storage.qdrant.adapter import _tokenize_for_scoring
        tokens = _tokenize_for_scoring("hello world")
        self.assertIn("hello", tokens)
        self.assertIn("world", tokens)

    def test_chinese_chars(self):
        from opencortex.storage.qdrant.adapter import _tokenize_for_scoring
        tokens = _tokenize_for_scoring("查询记忆")
        self.assertIn("查", tokens)
        self.assertIn("询", tokens)
        self.assertIn("记", tokens)
        self.assertIn("忆", tokens)

    def test_mixed_chinese_english(self):
        from opencortex.storage.qdrant.adapter import _tokenize_for_scoring
        tokens = _tokenize_for_scoring("python 开发指南")
        self.assertIn("python", tokens)
        self.assertIn("开", tokens)

    def test_error_codes_and_paths(self):
        from opencortex.storage.qdrant.adapter import _tokenize_for_scoring
        tokens = _tokenize_for_scoring("error-404 config.yaml")
        self.assertIn("error-404", tokens)
        self.assertIn("config.yaml", tokens)

    def test_empty_string(self):
        from opencortex.storage.qdrant.adapter import _tokenize_for_scoring
        tokens = _tokenize_for_scoring("")
        self.assertEqual(tokens, set())

    def test_none_input(self):
        from opencortex.storage.qdrant.adapter import _tokenize_for_scoring
        tokens = _tokenize_for_scoring(None)
        self.assertEqual(tokens, set())


class TestComputeTextScore(unittest.TestCase):
    def test_exact_match_abstract(self):
        from opencortex.storage.qdrant.adapter import _compute_text_score
        score = _compute_text_score("python", "python project setup", "")
        self.assertGreater(score, 0.0)

    def test_exact_match_overview(self):
        from opencortex.storage.qdrant.adapter import _compute_text_score
        score = _compute_text_score("python", "", "python project setup")
        self.assertGreater(score, 0.0)

    def test_abstract_weighted_higher(self):
        from opencortex.storage.qdrant.adapter import _compute_text_score
        score_abstract = _compute_text_score("python", "python", "")
        score_overview = _compute_text_score("python", "", "python")
        self.assertGreater(score_abstract, score_overview)

    def test_no_match(self):
        from opencortex.storage.qdrant.adapter import _compute_text_score
        score = _compute_text_score("python", "java setup", "ruby guide")
        self.assertEqual(score, 0.0)

    def test_chinese_query_matches(self):
        from opencortex.storage.qdrant.adapter import _compute_text_score
        score = _compute_text_score("记忆", "用户记忆存储", "")
        self.assertGreater(score, 0.0)

    def test_empty_query(self):
        from opencortex.storage.qdrant.adapter import _compute_text_score
        score = _compute_text_score("", "some text", "other text")
        self.assertEqual(score, 0.0)

    def test_score_capped_at_one(self):
        from opencortex.storage.qdrant.adapter import _compute_text_score
        score = _compute_text_score("a", "a a a a a", "a a a a a")
        self.assertLessEqual(score, 1.0)


if __name__ == "__main__":
    unittest.main()
