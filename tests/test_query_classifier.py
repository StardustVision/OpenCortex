import unittest
from unittest.mock import MagicMock
import numpy as np

class TestQueryFastClassifier(unittest.TestCase):
    def _make_classifier(self):
        from opencortex.retrieve.query_classifier import QueryFastClassifier
        embedder = MagicMock()
        def fake_embed(text):
            if "文档" in text or "论文" in text:
                return np.array([1.0, 0.0, 0.0, 0.0])
            if "最近" in text or "昨天" in text:
                return np.array([0.0, 1.0, 0.0, 0.0])
            if "人名" in text or "术语" in text:
                return np.array([0.0, 0.0, 1.0, 0.0])
            return np.array([0.25, 0.25, 0.25, 0.25])
        embedder.embed = fake_embed

        config = MagicMock()
        config.query_classifier_classes = {
            "document_scoped": "查找特定文档、论文、文件中的内容",
            "temporal_lookup": "查找最近、上次、昨天等时间相关的记忆",
            "fact_lookup": "查找特定人名、数字、术语、文件名等精确事实",
            "simple_recall": "简单的记忆召回",
        }
        config.query_classifier_threshold = 0.3
        config.query_classifier_hybrid_weights = {
            "document_scoped": {"dense": 0.5, "lexical": 0.5},
            "fact_lookup": {"dense": 0.3, "lexical": 0.7},
            "temporal_lookup": {"dense": 0.6, "lexical": 0.4},
            "simple_recall": {"dense": 0.7, "lexical": 0.3},
            "complex": {"dense": 0.7, "lexical": 0.3},
        }
        return QueryFastClassifier(embedder, config)

    def test_target_doc_id_forces_document_scoped(self):
        clf = self._make_classifier()
        result = clf.classify("anything", target_doc_id="doc_abc123")
        self.assertEqual(result.query_class, "document_scoped")
        self.assertFalse(result.need_llm_intent)
        self.assertEqual(result.doc_scope_hint, "doc_abc123")

    def test_classify_returns_query_classification(self):
        clf = self._make_classifier()
        result = clf.classify("这篇论文讲了什么", target_doc_id=None)
        self.assertIn(result.query_class,
                      ["document_scoped", "temporal_lookup", "fact_lookup", "simple_recall", "complex"])
        self.assertIsInstance(result.lexical_boost, float)

    def test_low_confidence_falls_back_to_complex(self):
        from opencortex.retrieve.query_classifier import QueryFastClassifier
        embedder = MagicMock()
        embedder.embed = lambda text: np.array([0.0, 0.0, 0.0, 0.0])  # zero vector
        config = MagicMock()
        config.query_classifier_classes = {"simple_recall": "test"}
        config.query_classifier_threshold = 0.3
        config.query_classifier_hybrid_weights = {"complex": {"dense": 0.7, "lexical": 0.3}}
        clf = QueryFastClassifier(embedder, config)
        result = clf.classify("anything")
        self.assertEqual(result.query_class, "complex")
        self.assertTrue(result.need_llm_intent)
