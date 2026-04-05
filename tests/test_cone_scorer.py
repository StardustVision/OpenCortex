import unittest
from unittest.mock import AsyncMock
from opencortex.retrieve.entity_index import EntityIndex
from opencortex.retrieve.cone_scorer import ConeScorer


class TestConeScorer(unittest.TestCase):

    def setUp(self):
        self.idx = EntityIndex()
        self.idx.add("col", "m1", ["melanie"])
        self.idx.add("col", "m2", ["melanie", "caroline"])
        self.idx.add("col", "m3", ["caroline"])
        self.idx.add("col", "m4", ["redis"])
        self.idx._built.add("col")  # Mark as fully built for testing
        self.scorer = ConeScorer(self.idx)

    def test_direct_hit_no_entity(self):
        candidates = [{"id": "m99", "_score": 0.8}]
        result = self.scorer.compute_cone_scores(candidates, set(), "col")
        self.assertIn("_cone_bonus", result[0])

    def test_entity_propagation(self):
        candidates = [
            {"id": "m1", "_score": 0.9},
            {"id": "m2", "_score": 0.5},
        ]
        result = self.scorer.compute_cone_scores(candidates, set(), "col")
        m2 = next(r for r in result if r["id"] == "m2")
        self.assertGreater(m2["_cone_bonus"], 0.5)

    def test_query_entity_half_hop(self):
        candidates = [
            {"id": "m1", "_score": 0.9},
            {"id": "m2", "_score": 0.5},
        ]
        result_no = self.scorer.compute_cone_scores(candidates, set(), "col")
        result_qe = self.scorer.compute_cone_scores(
            [{"id": "m1", "_score": 0.9}, {"id": "m2", "_score": 0.5}],
            {"melanie"}, "col",
        )
        m2_no = next(r for r in result_no if r["id"] == "m2")
        m2_qe = next(r for r in result_qe if r["id"] == "m2")
        self.assertGreaterEqual(m2_qe["_cone_bonus"], m2_no["_cone_bonus"])

    def test_high_degree_suppression(self):
        for i in range(60):
            self.idx.add("col", f"pop{i}", ["popular"])
        self.idx.add("col", "target", ["popular"])
        candidates = [
            {"id": "pop0", "_score": 0.9},
            {"id": "target", "_score": 0.3},
        ]
        result = self.scorer.compute_cone_scores(candidates, set(), "col")
        target = next(r for r in result if r["id"] == "target")
        self.assertLess(target["_cone_bonus"], 0.5)

    def test_broad_match_penalty(self):
        candidates = [{"id": "no_entity", "_score": 0.6}]
        result = self.scorer.compute_cone_scores(candidates, set(), "col")
        self.assertLess(result[0]["_cone_bonus"], 0.6)

    def test_empty_candidates(self):
        self.assertEqual(self.scorer.compute_cone_scores([], set(), "col"), [])

    def test_index_not_ready(self):
        candidates = [{"id": "m1", "_score": 0.8}]
        result = self.scorer.compute_cone_scores(candidates, set(), "unknown_col")
        self.assertAlmostEqual(result[0]["_cone_bonus"], 0.8, places=1)


class TestQueryEntityExtraction(unittest.TestCase):

    def setUp(self):
        self.idx = EntityIndex()
        self.idx.add("col", "m1", ["melanie", "caroline"])
        self.idx.add("col", "m2", ["redis"])
        self.scorer = ConeScorer(self.idx)

    def test_extracts_matching(self):
        candidates = [{"id": "m1", "_score": 0.8}]
        entities = self.scorer.extract_query_entities("melanie有孩子吗", candidates, "col")
        self.assertIn("melanie", entities)

    def test_no_match(self):
        candidates = [{"id": "m1", "_score": 0.8}]
        entities = self.scorer.extract_query_entities("天气怎么样", candidates, "col")
        self.assertEqual(len(entities), 0)


class TestCandidateExpansion(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.idx = EntityIndex()
        self.idx.add("col", "m1", ["melanie"])
        self.idx.add("col", "m2", ["melanie"])
        self.scorer = ConeScorer(self.idx)

    async def test_expansion_adds_related(self):
        candidates = [{"id": "m1", "_score": 0.9}]
        storage = AsyncMock()
        storage.get = AsyncMock(return_value=[
            {"id": "m2", "abstract": "test", "entities": ["melanie"]},
        ])
        expanded = await self.scorer.expand_candidates(candidates, {"melanie"}, "col", storage)
        ids = {str(c["id"]) for c in expanded}
        self.assertIn("m2", ids)

    async def test_expansion_limited(self):
        for i in range(30):
            self.idx.add("col", f"x{i}", ["common"])
        candidates = [{"id": "x0", "_score": 0.9}]
        storage = AsyncMock()
        storage.get = AsyncMock(return_value=[
            {"id": f"x{i}", "abstract": "test"} for i in range(1, 21)
        ])
        expanded = await self.scorer.expand_candidates(candidates, {"common"}, "col", storage)
        self.assertLessEqual(len(expanded), 21)


if __name__ == "__main__":
    unittest.main()
