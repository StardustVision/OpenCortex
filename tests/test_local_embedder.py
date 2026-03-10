"""Tests for LocalEmbedder (BGE-M3 via FastEmbed)."""

import unittest
from unittest.mock import MagicMock, patch
import numpy as np

from opencortex.models.embedder.base import EmbedResult


class TestLocalEmbedderInit(unittest.TestCase):

    @patch("opencortex.models.embedder.local_embedder.TextEmbedding", create=True)
    def test_init_success(self, MockTextEmbedding):
        """Model loads and dimension detected from test embedding."""
        mock_model = MagicMock()
        fake_vec = MagicMock()
        fake_vec.__len__ = lambda self: 1024
        mock_model.embed.return_value = iter([fake_vec])
        MockTextEmbedding.return_value = mock_model

        with patch.dict("sys.modules", {"fastembed": MagicMock(TextEmbedding=MockTextEmbedding)}):
            from opencortex.models.embedder.local_embedder import LocalEmbedder
            embedder = LocalEmbedder.__new__(LocalEmbedder)
            embedder.model_name = "BAAI/bge-m3"
            embedder.config = {}
            embedder._model = None
            embedder._dimension = None
            embedder._init_model()

        self.assertTrue(embedder.is_available)
        self.assertEqual(embedder._dimension, 1024)

    def test_init_without_fastembed(self):
        """LocalEmbedder handles missing fastembed gracefully."""
        from opencortex.models.embedder.local_embedder import LocalEmbedder
        with patch.object(LocalEmbedder, "_init_model", side_effect=_set_model_none):
            embedder = LocalEmbedder.__new__(LocalEmbedder)
            embedder.model_name = "BAAI/bge-m3"
            embedder.config = {}
            embedder._model = None
            embedder._dimension = None
        self.assertFalse(embedder.is_available)


class TestLocalEmbedderEmbed(unittest.TestCase):

    def _make_embedder(self):
        """Create a LocalEmbedder with mocked model."""
        from opencortex.models.embedder.local_embedder import LocalEmbedder
        embedder = LocalEmbedder.__new__(LocalEmbedder)
        embedder.model_name = "BAAI/bge-m3"
        embedder.config = {}
        embedder._dimension = 4
        embedder._model = MagicMock()
        return embedder

    def test_embed_single(self):
        embedder = self._make_embedder()
        fake_array = MagicMock()
        fake_array.tolist.return_value = [0.1, 0.2, 0.3, 0.4]
        embedder._model.embed.return_value = iter([fake_array])

        result = embedder.embed("hello world")
        self.assertIsInstance(result, EmbedResult)
        self.assertEqual(result.dense_vector, [0.1, 0.2, 0.3, 0.4])

    def test_embed_batch(self):
        embedder = self._make_embedder()
        fake1 = MagicMock()
        fake1.tolist.return_value = [0.1, 0.2, 0.3, 0.4]
        fake2 = MagicMock()
        fake2.tolist.return_value = [0.5, 0.6, 0.7, 0.8]
        embedder._model.embed.return_value = iter([fake1, fake2])

        results = embedder.embed_batch(["hello", "world"])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].dense_vector, [0.1, 0.2, 0.3, 0.4])
        self.assertEqual(results[1].dense_vector, [0.5, 0.6, 0.7, 0.8])

    def test_embed_raises_when_model_not_loaded(self):
        from opencortex.models.embedder.local_embedder import LocalEmbedder
        embedder = LocalEmbedder.__new__(LocalEmbedder)
        embedder.model_name = "BAAI/bge-m3"
        embedder.config = {}
        embedder._model = None
        embedder._dimension = None

        with self.assertRaises(RuntimeError):
            embedder.embed("test")

    def test_get_dimension_default(self):
        from opencortex.models.embedder.local_embedder import LocalEmbedder
        embedder = LocalEmbedder.__new__(LocalEmbedder)
        embedder.model_name = "BAAI/bge-m3"
        embedder.config = {}
        embedder._model = None
        embedder._dimension = None
        self.assertEqual(embedder.get_dimension(), 1024)

    def test_get_dimension_detected(self):
        embedder = self._make_embedder()
        self.assertEqual(embedder.get_dimension(), 4)

    def test_close(self):
        embedder = self._make_embedder()
        embedder.close()
        self.assertIsNone(embedder._model)


def _set_model_none(self):
    self._model = None


if __name__ == "__main__":
    unittest.main()
