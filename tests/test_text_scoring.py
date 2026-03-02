"""Tests for lexical text scoring helpers."""
import asyncio
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


class TestEmbedTimeout(unittest.TestCase):
    def test_timeout_returns_none(self):
        """Embedding timeout returns None vectors."""
        from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever
        from opencortex.models.embedder.base import EmbedResult
        import time

        class SlowEmbedder:
            def embed(self, text):
                time.sleep(3.0)
                return EmbedResult(dense_vector=[1.0, 0.0, 0.0, 0.0])
            def get_dimension(self):
                return 4

        retriever = HierarchicalRetriever(
            storage=None, embedder=SlowEmbedder(),
            embed_timeout=0.1,
        )
        result = asyncio.run(retriever._embed_with_timeout("test query"))
        self.assertIsNone(result)

    def test_fast_embed_returns_result(self):
        """Fast embedding returns normal result."""
        from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever
        from opencortex.models.embedder.base import EmbedResult

        class FastEmbedder:
            def embed(self, text):
                return EmbedResult(dense_vector=[1.0, 0.0, 0.0, 0.0])
            def get_dimension(self):
                return 4

        retriever = HierarchicalRetriever(
            storage=None, embedder=FastEmbedder(),
            embed_timeout=5.0,
        )
        result = asyncio.run(retriever._embed_with_timeout("test query"))
        self.assertIsNotNone(result)
        self.assertEqual(result.dense_vector, [1.0, 0.0, 0.0, 0.0])

    def test_no_embedder_returns_none(self):
        """No embedder returns None."""
        from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever

        retriever = HierarchicalRetriever(
            storage=None, embedder=None,
            embed_timeout=5.0,
        )
        result = asyncio.run(retriever._embed_with_timeout("test query"))
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
