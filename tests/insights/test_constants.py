"""Tests for insights constants."""
import unittest
from opencortex.insights.constants import (
    MAX_SESSIONS_TO_LOAD,
    MAX_FACET_EXTRACTIONS,
    FACET_CONCURRENCY,
    TRANSCRIPT_THRESHOLD,
    CHUNK_SIZE,
    OVERLAP_WINDOW_MS,
    MIN_RESPONSE_TIME_SEC,
    MAX_RESPONSE_TIME_SEC,
    MIN_USER_MESSAGES,
    MIN_DURATION_MINUTES,
    RESPONSE_TIME_BUCKETS,
    SATISFACTION_ORDER,
    OUTCOME_ORDER,
    EXTENSION_TO_LANGUAGE,
    ERROR_CATEGORIES,
)


class TestConstants(unittest.TestCase):
    def test_session_limits(self):
        self.assertEqual(MAX_SESSIONS_TO_LOAD, 200)
        self.assertEqual(MAX_FACET_EXTRACTIONS, 50)
        self.assertEqual(FACET_CONCURRENCY, 50)

    def test_transcript_thresholds(self):
        self.assertEqual(TRANSCRIPT_THRESHOLD, 30000)
        self.assertEqual(CHUNK_SIZE, 25000)
        self.assertLess(CHUNK_SIZE, TRANSCRIPT_THRESHOLD)

    def test_response_time_buckets_cover_full_range(self):
        self.assertEqual(RESPONSE_TIME_BUCKETS[0][0], "2-10s")
        self.assertEqual(RESPONSE_TIME_BUCKETS[-1][0], ">15m")
        for i in range(len(RESPONSE_TIME_BUCKETS) - 1):
            self.assertEqual(RESPONSE_TIME_BUCKETS[i][2], RESPONSE_TIME_BUCKETS[i + 1][1])

    def test_satisfaction_order(self):
        self.assertEqual(SATISFACTION_ORDER[0], "frustrated")
        self.assertEqual(SATISFACTION_ORDER[-1], "unsure")
        self.assertEqual(len(SATISFACTION_ORDER), 6)

    def test_outcome_order(self):
        self.assertEqual(OUTCOME_ORDER[0], "not_achieved")
        self.assertEqual(OUTCOME_ORDER[-1], "unclear_from_transcript")
        self.assertEqual(len(OUTCOME_ORDER), 5)

    def test_extension_to_language(self):
        self.assertEqual(EXTENSION_TO_LANGUAGE[".py"], "Python")
        self.assertEqual(EXTENSION_TO_LANGUAGE[".ts"], "TypeScript")
        self.assertEqual(EXTENSION_TO_LANGUAGE[".tsx"], "TypeScript")
        self.assertGreaterEqual(len(EXTENSION_TO_LANGUAGE), 16)

    def test_error_categories(self):
        names = [cat[-1] for cat in ERROR_CATEGORIES]
        self.assertIn("Command Failed", names)
        self.assertIn("Edit Failed", names)
        self.assertIn("File Not Found", names)


if __name__ == "__main__":
    unittest.main()
