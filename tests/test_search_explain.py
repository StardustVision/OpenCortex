import unittest
from opencortex.retrieve.types import SearchExplain, SearchExplainSummary


class TestSearchExplain(unittest.TestCase):
    def test_search_explain_fields(self):
        e = SearchExplain(
            query_class="fact_lookup", path="fast_path",
            intent_ms=0.0, embed_ms=5.2, search_ms=12.3,
            rerank_ms=0.0, assemble_ms=3.1,
            doc_scope_hit=False, time_filter_hit=False,
            candidates_before_rerank=10, candidates_after_rerank=10,
            frontier_waves=0, frontier_budget_exceeded=False,
            total_ms=20.6,
        )
        self.assertEqual(e.query_class, "fact_lookup")
        self.assertEqual(e.total_ms, 20.6)

    def test_search_explain_summary_fields(self):
        s = SearchExplainSummary(
            total_ms=25.0, query_count=2,
            primary_query_class="document_scoped",
            primary_path="fast_path",
            doc_scope_hit=True, time_filter_hit=False,
            rerank_triggered=False,
        )
        self.assertEqual(s.query_count, 2)
        self.assertTrue(s.doc_scope_hit)
