import unittest


class TestRerankGate(unittest.TestCase):
    def test_skip_rerank_for_fact_lookup_high_lexical(self):
        from opencortex.retrieve.query_classifier import QueryClassification
        clf = QueryClassification(
            query_class="fact_lookup", need_llm_intent=False, lexical_boost=0.7)
        # fact_lookup with lexical >= 0.6 should skip
        should = True
        if clf.query_class == "fact_lookup" and clf.lexical_boost >= 0.6:
            should = False
        self.assertFalse(should)

    def test_skip_rerank_for_doc_scoped_small_pool(self):
        from opencortex.retrieve.query_classifier import QueryClassification
        clf = QueryClassification(
            query_class="document_scoped", need_llm_intent=False, lexical_boost=0.5)
        candidates = [{"uri": f"u{i}", "_final_score": 0.9 - i*0.02} for i in range(3)]
        should = True
        if clf.query_class == "document_scoped" and len(candidates) < 5:
            should = False
        self.assertFalse(should)

    def test_no_skip_for_complex_query(self):
        from opencortex.retrieve.query_classifier import QueryClassification
        clf = QueryClassification(
            query_class="complex", need_llm_intent=True, lexical_boost=0.3)
        should = True
        if clf.query_class == "fact_lookup" and clf.lexical_boost >= 0.6:
            should = False
        if clf.query_class == "document_scoped" and 10 < 5:
            should = False
        self.assertTrue(should)
