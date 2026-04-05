import unittest
from opencortex.retrieve.entity_index import EntityIndex
from opencortex.retrieve.cone_scorer import ConeScorer


class TestConeE2E(unittest.TestCase):

    def test_entity_path_boosts_related_memory(self):
        idx = EntityIndex()
        idx.add("col", "m_strong", ["melanie"])
        idx.add("col", "m_weak", ["melanie"])
        idx.add("col", "m_noise", ["unrelated"])
        idx._built.add("col")
        scorer = ConeScorer(idx)
        candidates = [
            {"id": "m_strong", "_score": 0.9},
            {"id": "m_weak", "_score": 0.4},
            {"id": "m_noise", "_score": 0.5},
        ]
        result = scorer.compute_cone_scores(candidates, {"melanie"}, "col")
        scores = {r["id"]: r["_cone_bonus"] for r in result}
        self.assertGreater(scores["m_weak"], scores["m_noise"])

    def test_no_entity_graceful_degradation(self):
        idx = EntityIndex()
        idx._built.add("col")
        scorer = ConeScorer(idx)
        candidates = [
            {"id": "m1", "_score": 0.8},
            {"id": "m2", "_score": 0.6},
        ]
        result = scorer.compute_cone_scores(candidates, set(), "col")
        self.assertGreaterEqual(result[0]["_cone_bonus"], result[1]["_cone_bonus"])

    def test_ranking_with_cone_weight(self):
        """Cone bonus narrows gap when weaker candidate shares entity with a
        high-score anchor that the stronger candidate does not."""
        idx = EntityIndex()
        # m_anchor is a high-score anchor that shares entity 'redis' with m2
        idx.add("col", "m_anchor", ["redis"])
        idx.add("col", "m2", ["redis"])
        # m1 has no entity links at all
        idx._built.add("col")
        scorer = ConeScorer(idx)
        candidates = [
            {"id": "m_anchor", "_score": 0.95, "_final_score": 0.90},
            {"id": "m1", "_score": 0.7, "_final_score": 0.65},
            {"id": "m2", "_score": 0.4, "_final_score": 0.35},
        ]
        result = scorer.compute_cone_scores(candidates, {"redis"}, "col")
        cone_weight = 0.1
        for r in result:
            r["_final_with_cone"] = r.get("_final_score", 0) + cone_weight * r["_cone_bonus"]
        m1 = next(r for r in result if r["id"] == "m1")
        m2 = next(r for r in result if r["id"] == "m2")
        # m2 should get a higher cone bonus than m1 (m2 shares entity with anchor)
        self.assertGreater(m2["_cone_bonus"], m1["_cone_bonus"])
        # The gap should narrow after applying cone weight
        gap_before = m1["_final_score"] - m2["_final_score"]
        gap_after = m1["_final_with_cone"] - m2["_final_with_cone"]
        self.assertLess(gap_after, gap_before)


if __name__ == "__main__":
    unittest.main()
