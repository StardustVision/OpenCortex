"""
Tests for uri_path_scorer.compute_uri_path_scores().

Each test is focused on a single scenario with explicit expected values
so regressions are immediately obvious.
"""

import unittest
from opencortex.retrieve.uri_path_scorer import (
    compute_uri_path_scores,
    URI_DIRECT_PENALTY,
    URI_HOP_COST,
    HIGH_CONFIDENCE_THRESHOLD,
    HIGH_CONFIDENCE_DISCOUNT,
)


class TestComputeUriPathScoresHappyPaths(unittest.TestCase):

    def test_leaf_only_direct_hit(self):
        """Leaf with only a direct hit: cost = distance + URI_DIRECT_PENALTY."""
        leaf_hits = [{"uri": "oc://t/u/memory/cat/leaf1", "_score": 0.7}]
        result = compute_uri_path_scores(leaf_hits, [], [])
        self.assertIn("oc://t/u/memory/cat/leaf1", result)
        expected = (1.0 - 0.7) + URI_DIRECT_PENALTY
        self.assertAlmostEqual(result["oc://t/u/memory/cat/leaf1"], expected, places=10)

    def test_min_of_direct_anchor_fp(self):
        """Leaf reachable via all three paths: result must be min of all three."""
        leaf_uri = "oc://t/u/memory/cat/leaf1"
        # direct path cost: (1-0.6) + 0.15 = 0.55
        leaf_hits = [{"uri": leaf_uri, "_score": 0.6}]
        # anchor path cost: (1-0.7) + 0.05 = 0.35
        anchor_hits = [{"uri": "oc://t/u/memory/cat/leaf1/anchors/abc", "_score": 0.7,
                        "projection_target_uri": leaf_uri}]
        # fp path cost: (1-0.85) + 0.05 = 0.20  (dist=0.15 >= 0.10, no discount)
        fp_hits = [{"uri": "oc://t/u/memory/cat/leaf1/fact_points/xyz", "_score": 0.85,
                    "projection_target_uri": leaf_uri}]
        result = compute_uri_path_scores(leaf_hits, anchor_hits, fp_hits)
        self.assertAlmostEqual(result[leaf_uri], 0.20, places=10)

    def test_fp_high_confidence_discount(self):
        """fp distance < HIGH_CONFIDENCE_THRESHOLD triggers hop discount."""
        leaf_uri = "oc://t/u/memory/cat/leaf1"
        # fp _score=0.95 → dist=0.05, which is < 0.10 → hop = 0.05 * 0.5 = 0.025
        # cost = 0.05 + 0.025 = 0.075
        fp_hits = [{"uri": "oc://t/u/memory/cat/leaf1/fact_points/abc", "_score": 0.95,
                    "projection_target_uri": leaf_uri}]
        result = compute_uri_path_scores([], [], fp_hits)
        self.assertAlmostEqual(result[leaf_uri], 0.075, places=10)

    def test_anchor_discovers_leaf_not_in_leaf_hits(self):
        """Anchor pointing to a leaf absent from leaf_hits still adds that leaf."""
        leaf_uri = "oc://t/u/memory/cat/leaf_not_searched"
        anchor_hits = [{"uri": "oc://t/u/memory/cat/leaf_not_searched/anchors/a1",
                        "_score": 0.8, "projection_target_uri": leaf_uri}]
        result = compute_uri_path_scores([], anchor_hits, [])
        self.assertIn(leaf_uri, result)
        expected = (1.0 - 0.8) + URI_HOP_COST
        self.assertAlmostEqual(result[leaf_uri], expected, places=10)

    def test_perfect_anchor_score(self):
        """_score=1.0 → dist=0.0, anchor cost = 0.0 + URI_HOP_COST."""
        leaf_uri = "oc://t/u/memory/cat/leafA"
        anchor_hits = [{"uri": "oc://t/u/memory/cat/leafA/anchors/p", "_score": 1.0,
                        "projection_target_uri": leaf_uri}]
        result = compute_uri_path_scores([], anchor_hits, [])
        self.assertAlmostEqual(result[leaf_uri], URI_HOP_COST, places=10)


class TestComputeUriPathScoresEdgeCases(unittest.TestCase):

    def test_empty_inputs(self):
        """All three lists empty → empty dict."""
        result = compute_uri_path_scores([], [], [])
        self.assertEqual(result, {})

    def test_multiple_fps_same_leaf_min_wins(self):
        """Multiple fp hits pointing to the same leaf → min cost wins."""
        leaf_uri = "oc://t/u/memory/cat/leaf1"
        # fp1: dist=0.4 → cost = 0.4 + 0.05 = 0.45
        # fp2: dist=0.1 → cost = 0.1 + 0.05 = 0.15  (dist==threshold, no discount)
        # fp3: dist=0.05 → cost = 0.05 + 0.025 = 0.075  (discounted)
        fp_hits = [
            {"uri": "oc://t/u/memory/cat/leaf1/fact_points/a", "_score": 0.6,
             "projection_target_uri": leaf_uri},
            {"uri": "oc://t/u/memory/cat/leaf1/fact_points/b", "_score": 0.9,
             "projection_target_uri": leaf_uri},
            {"uri": "oc://t/u/memory/cat/leaf1/fact_points/c", "_score": 0.95,
             "projection_target_uri": leaf_uri},
        ]
        result = compute_uri_path_scores([], [], fp_hits)
        self.assertAlmostEqual(result[leaf_uri], 0.075, places=10)

    def test_fp_distance_above_threshold_no_discount(self):
        """fp distance > HIGH_CONFIDENCE_THRESHOLD: no discount applied."""
        # HIGH_CONFIDENCE_THRESHOLD = 0.10
        # _score = 0.89 → dist ≈ 0.11 → NOT < 0.10 → hop = URI_HOP_COST (no discount)
        leaf_uri = "oc://t/u/memory/cat/leafB"
        fp_hits = [{"uri": "oc://t/u/memory/cat/leafB/fact_points/x", "_score": 0.89,
                    "projection_target_uri": leaf_uri}]
        result = compute_uri_path_scores([], [], fp_hits)
        dist = 1.0 - 0.89
        expected = dist + URI_HOP_COST  # no discount: dist >= 0.10
        self.assertAlmostEqual(result[leaf_uri], expected, places=10)
        # Verify discount was NOT applied (would give dist + URI_HOP_COST * 0.5 instead)
        discounted = dist + URI_HOP_COST * HIGH_CONFIDENCE_DISCOUNT
        self.assertNotAlmostEqual(result[leaf_uri], discounted, places=5)

    def test_projection_target_uri_in_meta_fallback(self):
        """projection_target_uri absent at top level but present in meta → still works."""
        leaf_uri = "oc://t/u/memory/cat/leafC"
        anchor_hits = [{"uri": "oc://t/u/memory/cat/leafC/anchors/m",
                        "_score": 0.75,
                        "meta": {"projection_target_uri": leaf_uri}}]
        result = compute_uri_path_scores([], anchor_hits, [])
        self.assertIn(leaf_uri, result)
        expected = (1.0 - 0.75) + URI_HOP_COST
        self.assertAlmostEqual(result[leaf_uri], expected, places=10)

    def test_projection_target_uri_missing_hit_ignored(self):
        """anchor/fp with no resolvable projection_target_uri are silently ignored."""
        anchor_hits = [{"uri": "oc://t/u/memory/cat/leafD/anchors/z", "_score": 0.8}]
        fp_hits = [{"uri": "oc://t/u/memory/cat/leafD/fact_points/w", "_score": 0.9,
                    "meta": {}}]  # meta present but key absent
        result = compute_uri_path_scores([], anchor_hits, fp_hits)
        self.assertEqual(result, {})

    def test_leaf_without_uri_ignored(self):
        """Leaf hit missing 'uri' field is silently ignored."""
        leaf_hits = [{"_score": 0.8}]  # no uri
        result = compute_uri_path_scores(leaf_hits, [], [])
        self.assertEqual(result, {})

    def test_score_clamped_above_one(self):
        """_score > 1.0 is clamped to 1.0 so dist = 0."""
        leaf_uri = "oc://t/u/memory/cat/leafE"
        leaf_hits = [{"uri": leaf_uri, "_score": 1.5}]
        result = compute_uri_path_scores(leaf_hits, [], [])
        expected = 0.0 + URI_DIRECT_PENALTY
        self.assertAlmostEqual(result[leaf_uri], expected, places=10)

    def test_score_clamped_below_zero(self):
        """_score < 0.0 is clamped to 0.0 so dist = 1.0."""
        leaf_uri = "oc://t/u/memory/cat/leafF"
        leaf_hits = [{"uri": leaf_uri, "_score": -0.3}]
        result = compute_uri_path_scores(leaf_hits, [], [])
        expected = 1.0 + URI_DIRECT_PENALTY
        self.assertAlmostEqual(result[leaf_uri], expected, places=10)

    def test_direct_more_expensive_than_anchor_similar_distance(self):
        """When leaf and anchor have similar vector distance, anchor path wins (lower cost)."""
        leaf_uri = "oc://t/u/memory/cat/leafG"
        # direct: dist=0.3, cost = 0.3 + 0.15 = 0.45
        # anchor: dist=0.3, cost = 0.3 + 0.05 = 0.35  → anchor wins
        leaf_hits = [{"uri": leaf_uri, "_score": 0.7}]
        anchor_hits = [{"uri": "oc://t/u/memory/cat/leafG/anchors/a", "_score": 0.7,
                        "projection_target_uri": leaf_uri}]
        result = compute_uri_path_scores(leaf_hits, anchor_hits, [])
        self.assertAlmostEqual(result[leaf_uri], 0.35, places=10)

    def test_multiple_leaves_independent(self):
        """Scores for different leaves are computed independently."""
        leaf1 = "oc://t/u/memory/cat/leaf1"
        leaf2 = "oc://t/u/memory/cat/leaf2"
        leaf_hits = [
            {"uri": leaf1, "_score": 0.9},   # cost = 0.1 + 0.15 = 0.25
            {"uri": leaf2, "_score": 0.5},   # cost = 0.5 + 0.15 = 0.65
        ]
        result = compute_uri_path_scores(leaf_hits, [], [])
        self.assertAlmostEqual(result[leaf1], 0.25, places=10)
        self.assertAlmostEqual(result[leaf2], 0.65, places=10)

    def test_fp_top_level_takes_priority_over_meta(self):
        """projection_target_uri at top level is used, not the one in meta."""
        leaf_top = "oc://t/u/memory/cat/leaf_top"
        leaf_meta = "oc://t/u/memory/cat/leaf_meta"
        fp_hits = [{"uri": "oc://t/u/memory/cat/leaf_top/fact_points/q",
                    "_score": 0.8,
                    "projection_target_uri": leaf_top,
                    "meta": {"projection_target_uri": leaf_meta}}]
        result = compute_uri_path_scores([], [], fp_hits)
        self.assertIn(leaf_top, result)
        self.assertNotIn(leaf_meta, result)


if __name__ == "__main__":
    unittest.main()
