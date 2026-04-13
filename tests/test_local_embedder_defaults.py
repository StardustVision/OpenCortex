import unittest

from opencortex.config import CortexConfig, DEFAULT_LOCAL_RERANK_MODEL
from opencortex.models.embedder.local_embedder import DEFAULT_LOCAL_EMBEDDING_MODEL


class TestLocalEmbedderDefaults(unittest.TestCase):
    def test_default_local_embedding_model_matches_expected_probe_model(self):
        self.assertEqual(
            DEFAULT_LOCAL_EMBEDDING_MODEL,
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        )

    def test_config_defaults_match_local_embedding_runtime(self):
        cfg = CortexConfig()
        self.assertEqual(cfg.embedding_provider, "local")
        self.assertEqual(cfg.embedding_model, DEFAULT_LOCAL_EMBEDDING_MODEL)
        self.assertEqual(cfg.embedding_dimension, 384)

    def test_config_defaults_match_local_rerank_runtime(self):
        cfg = CortexConfig()
        self.assertEqual(cfg.rerank_provider, "local")
        self.assertEqual(cfg.rerank_model, DEFAULT_LOCAL_RERANK_MODEL)
        self.assertEqual(
            cfg.rerank_model,
            "jinaai/jina-reranker-v2-base-multilingual",
        )
