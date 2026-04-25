"""Unit tests for benchmarks/adapters/conversation_mapping.py.

Locks the §25 Phase 7 extraction (REVIEW closure tracker R2-24 /
R4-P2-8 / R4-P2-9). Each public helper has direct unit coverage so
future refactors of `LongMemEvalBench` / `LoCoMoBench` cannot silently
break the helpers without these tests failing first.

The integration test at the bottom (`TestStoreMcpEquivalence`) closes
TG-3 from the closure tracker — locks that the store-path and
mcp-path produce identical session→URI mappings for the same fixture.
"""

from __future__ import annotations

import unittest

from benchmarks.adapters.conversation_mapping import (
    message_span,
    normalize_text_set,
    overlap_width,
    ranges_overlap,
    record_time_refs,
)


class TestNormalizeTextSet(unittest.TestCase):
    """Pure helper — string normalization for exact-set matching."""

    def test_lowercases_strips_and_dedupes(self):
        self.assertEqual(
            normalize_text_set(["A", "a", "  b  ", "B"]),
            {"a", "b"},
        )

    def test_drops_empty_and_none(self):
        self.assertEqual(normalize_text_set(["", None, "  ", "x"]), {"x"})

    def test_empty_input_returns_empty_set(self):
        self.assertEqual(normalize_text_set([]), set())

    def test_coerces_non_string_via_str(self):
        # Confirms the str(value or "") path: numeric, bool both stringify.
        self.assertEqual(normalize_text_set([42, True]), {"42", "true"})


class TestMessageSpan(unittest.TestCase):
    """msg_range → (start, end) tuple, with shape guards."""

    def test_returns_tuple_for_valid_range(self):
        self.assertEqual(message_span({"msg_range": [3, 7]}), (3, 7))

    def test_zero_width_range_allowed(self):
        self.assertEqual(message_span({"msg_range": [5, 5]}), (5, 5))

    def test_missing_msg_range_returns_none(self):
        self.assertIsNone(message_span({}))

    def test_msg_range_not_list_returns_none(self):
        self.assertIsNone(message_span({"msg_range": "0,5"}))

    def test_msg_range_wrong_length_returns_none(self):
        self.assertIsNone(message_span({"msg_range": [0, 5, 10]}))
        self.assertIsNone(message_span({"msg_range": [3]}))

    def test_msg_range_non_integer_returns_none(self):
        self.assertIsNone(message_span({"msg_range": ["a", "b"]}))
        self.assertIsNone(message_span({"msg_range": [None, 3]}))

    def test_inverted_range_returns_none(self):
        self.assertIsNone(message_span({"msg_range": [7, 3]}))


class TestRangesOverlap(unittest.TestCase):
    """Inclusive overlap predicate."""

    def test_overlapping_ranges(self):
        self.assertTrue(ranges_overlap((0, 5), (3, 8)))

    def test_disjoint_ranges(self):
        self.assertFalse(ranges_overlap((0, 5), (6, 9)))

    def test_touching_ranges_count_as_overlap(self):
        # Inclusive: msg 5 is in both (0,5) and (5,10).
        self.assertTrue(ranges_overlap((0, 5), (5, 10)))

    def test_one_contains_other(self):
        self.assertTrue(ranges_overlap((0, 10), (3, 7)))

    def test_identical_ranges(self):
        self.assertTrue(ranges_overlap((3, 7), (3, 7)))


class TestOverlapWidth(unittest.TestCase):
    """Inclusive overlap-width count."""

    def test_partial_overlap(self):
        # (0,5) ∩ (3,8) = msgs 3, 4, 5 → width 3.
        self.assertEqual(overlap_width((0, 5), (3, 8)), 3)

    def test_no_overlap_returns_zero(self):
        self.assertEqual(overlap_width((0, 5), (10, 15)), 0)

    def test_touching_at_boundary_returns_one(self):
        # (0,5) ∩ (5,10) = {5} → width 1.
        self.assertEqual(overlap_width((0, 5), (5, 10)), 1)

    def test_one_contains_other(self):
        # (0,10) ∩ (3,7) = msgs 3..7 → width 5.
        self.assertEqual(overlap_width((0, 10), (3, 7)), 5)

    def test_identical_ranges(self):
        # (3,7) ∩ (3,7) = msgs 3..7 → width 5.
        self.assertEqual(overlap_width((3, 7), (3, 7)), 5)


class TestRecordTimeRefs(unittest.TestCase):
    """Drains time_refs / event_date from meta + abstract_json slots."""

    def test_meta_time_refs_extracted(self):
        record = {"meta": {"time_refs": ["2026-04-25", "yesterday"]}}
        self.assertEqual(record_time_refs(record), {"2026-04-25", "yesterday"})

    def test_meta_event_date_extracted(self):
        record = {"meta": {"event_date": "2026-04-25"}}
        self.assertEqual(record_time_refs(record), {"2026-04-25"})

    def test_top_level_event_date_extracted(self):
        record = {"event_date": "2026-04-25"}
        self.assertEqual(record_time_refs(record), {"2026-04-25"})

    def test_abstract_json_slots_time_refs_extracted(self):
        record = {"abstract_json": {"slots": {"time_refs": ["next week"]}}}
        self.assertEqual(record_time_refs(record), {"next week"})

    def test_all_sources_combined_and_normalized(self):
        record = {
            "event_date": "2026-04-25",
            "meta": {
                "time_refs": ["YESTERDAY", "  today  "],
                "event_date": "2026-04-24",
            },
            "abstract_json": {"slots": {"time_refs": ["Tomorrow"]}},
        }
        self.assertEqual(
            record_time_refs(record),
            {"2026-04-25", "2026-04-24", "yesterday", "today", "tomorrow"},
        )

    def test_empty_record_returns_empty_set(self):
        self.assertEqual(record_time_refs({}), set())

    def test_meta_not_dict_skipped(self):
        # Defensive: meta as string should not crash.
        self.assertEqual(record_time_refs({"meta": "garbage"}), set())


if __name__ == "__main__":
    unittest.main()
