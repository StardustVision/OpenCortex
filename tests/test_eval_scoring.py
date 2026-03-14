"""Unit tests for benchmarks/scoring.py — F1 token overlap + LLM judge parsing."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.scoring import f1_score, score_qa, _normalize, _parse_judge_score


class TestNormalize(unittest.TestCase):
    def test_lowercase_and_strip_articles(self):
        self.assertEqual(_normalize("The quick Brown fox"), "quick brown fox")

    def test_remove_punctuation(self):
        self.assertEqual(_normalize("hello, world!"), "hello world")

    def test_comma_removal(self):
        self.assertEqual(_normalize("1,000 items"), "1000 items")

    def test_empty_string(self):
        self.assertEqual(_normalize(""), "")


class TestF1Score(unittest.TestCase):
    def test_exact_match(self):
        self.assertAlmostEqual(f1_score("dark roast coffee", "dark roast coffee"), 1.0)

    def test_partial_overlap(self):
        score = f1_score("dark roast", "dark roast coffee")
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1.0)

    def test_no_overlap(self):
        self.assertAlmostEqual(f1_score("completely different", "dark roast coffee"), 0.0)

    def test_empty_prediction(self):
        self.assertAlmostEqual(f1_score("", "dark roast coffee"), 0.0)

    def test_case_insensitive(self):
        self.assertAlmostEqual(f1_score("Dark Roast Coffee", "dark roast coffee"), 1.0)

    def test_article_only_inputs(self):
        """Both inputs normalize to empty after article removal → 0.0."""
        self.assertAlmostEqual(f1_score("the", "the"), 0.0)

    def test_both_empty(self):
        self.assertAlmostEqual(f1_score("", ""), 0.0)


class TestScoreQA(unittest.TestCase):
    def test_category_1_multi_answer(self):
        """Single-hop: comma-separated multi-answer F1."""
        score = score_qa("coffee, tea", "coffee, tea", category=1)
        self.assertAlmostEqual(score, 1.0)

    def test_category_3_semicolon_first(self):
        """Commonsense: use first semicolon alternative."""
        score = score_qa("happy", "happy; joyful; glad", category=3)
        self.assertAlmostEqual(score, 1.0)

    def test_category_5_adversarial_refusal(self):
        """Adversarial: refusal phrases → 1.0."""
        self.assertAlmostEqual(score_qa("No information available", "anything", category=5), 1.0)
        self.assertAlmostEqual(score_qa("Not mentioned in text", "anything", category=5), 1.0)

    def test_category_5_adversarial_no_refusal(self):
        """Adversarial: no refusal → 0.0."""
        self.assertAlmostEqual(score_qa("The answer is 42", "anything", category=5), 0.0)

    def test_category_2_temporal(self):
        """Temporal: standard F1."""
        score = score_qa("January 2024", "January 2024", category=2)
        self.assertAlmostEqual(score, 1.0)

    def test_category_4_multihop(self):
        """Multi-hop: standard F1."""
        score = score_qa("dark roast", "dark roast coffee", category=4)
        self.assertGreater(score, 0.5)


class TestParseJudgeScore(unittest.TestCase):
    def test_parse_1_0(self):
        self.assertAlmostEqual(_parse_judge_score("1.0"), 1.0)

    def test_parse_0_5(self):
        self.assertAlmostEqual(_parse_judge_score("0.5"), 0.5)

    def test_parse_0_0(self):
        self.assertAlmostEqual(_parse_judge_score("0.0"), 0.0)

    def test_parse_with_whitespace(self):
        self.assertAlmostEqual(_parse_judge_score("  1.0  \n"), 1.0)

    def test_parse_garbage_returns_0(self):
        self.assertAlmostEqual(_parse_judge_score("not a number"), 0.0)

    def test_parse_out_of_range_clamps(self):
        self.assertAlmostEqual(_parse_judge_score("2.5"), 0.0)
        self.assertAlmostEqual(_parse_judge_score("-1.0"), 0.0)


if __name__ == "__main__":
    unittest.main()
