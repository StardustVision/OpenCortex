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

import asyncio
import unittest
from typing import Any, Dict, List

from benchmarks.adapters.conversation_mapping import (
    extract_records_by_uri,
    map_session_uris,
    memory_record_snapshot,
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


class TestExtractRecordsByUri(unittest.TestCase):
    """Drains payload[records] into {uri: dict(record)}."""

    def test_happy_path_two_records(self):
        payload = {
            "records": [
                {"uri": "u1", "content": "x", "msg_range": [0, 1]},
                {"uri": "u2", "content": "y", "msg_range": [2, 3]},
            ]
        }
        out = extract_records_by_uri(payload)
        self.assertEqual(set(out.keys()), {"u1", "u2"})
        self.assertEqual(out["u1"]["content"], "x")
        self.assertEqual(out["u2"]["msg_range"], [2, 3])

    def test_missing_records_key_returns_empty_dict(self):
        self.assertEqual(extract_records_by_uri({}), {})

    def test_empty_records_list_returns_empty_dict(self):
        self.assertEqual(extract_records_by_uri({"records": []}), {})

    def test_records_none_returns_empty_dict(self):
        self.assertEqual(extract_records_by_uri({"records": None}), {})

    def test_filters_empty_uri(self):
        payload = {
            "records": [
                {"uri": "", "content": "x"},
                {"uri": "u1", "content": "y"},
            ]
        }
        self.assertEqual(set(extract_records_by_uri(payload).keys()), {"u1"})

    def test_filters_missing_uri(self):
        payload = {"records": [{"content": "no uri"}, {"uri": "u1"}]}
        self.assertEqual(set(extract_records_by_uri(payload).keys()), {"u1"})

    def test_returns_shallow_copy_of_each_record(self):
        original = {"uri": "u1", "meta": {"a": 1}}
        payload = {"records": [original]}
        out = extract_records_by_uri(payload)
        # Mutating top-level keys on the copy must not touch the input.
        out["u1"]["new_key"] = "added"
        self.assertNotIn("new_key", original)


class _StubOC:
    """Minimal oc stand-in that satisfies memory_record_snapshot's contract."""

    def __init__(self, pages: List[List[Dict[str, Any]]]) -> None:
        # Each `pages[i]` is the ``results`` list returned for offset i*limit.
        self._pages = pages
        self._limit = 500
        self.calls: List[Dict[str, Any]] = []

    async def memory_list(
        self,
        *,
        context_type: str,
        category: str,
        limit: int,
        offset: int,
        include_payload: bool,
    ) -> Dict[str, Any]:
        self.calls.append({
            "context_type": context_type,
            "category": category,
            "limit": limit,
            "offset": offset,
            "include_payload": include_payload,
        })
        # Pages indexed by offset / limit; out-of-range returns an empty
        # results list (so the loop terminates).
        page_idx = offset // limit
        if page_idx < len(self._pages):
            return {"results": list(self._pages[page_idx])}
        return {"results": []}


class TestMemoryRecordSnapshot(unittest.TestCase):
    """Pages oc.memory_list until exhausted; keys by uri."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_single_page_returns_record_dict(self):
        async def check():
            oc = _StubOC([[{"uri": "u1", "content": "a"}, {"uri": "u2"}]])
            result = await memory_record_snapshot(oc)
            self.assertEqual(set(result.keys()), {"u1", "u2"})
            self.assertEqual(result["u1"]["content"], "a")
            # Single call because the result count was below the limit.
            self.assertEqual(len(oc.calls), 1)
            self.assertEqual(oc.calls[0]["context_type"], "memory")
            self.assertEqual(oc.calls[0]["category"], "events")
            self.assertTrue(oc.calls[0]["include_payload"])

        self._run(check())

    def test_pages_until_partial_page(self):
        async def check():
            # First page is full (500), second page is partial → loop exits.
            full_page = [{"uri": f"u{i}"} for i in range(500)]
            partial_page = [{"uri": "u500"}, {"uri": "u501"}]
            oc = _StubOC([full_page, partial_page])
            result = await memory_record_snapshot(oc)
            self.assertEqual(len(result), 502)
            self.assertEqual(len(oc.calls), 2)
            self.assertEqual(oc.calls[0]["offset"], 0)
            self.assertEqual(oc.calls[1]["offset"], 500)

        self._run(check())

    def test_filters_records_with_empty_uri(self):
        async def check():
            oc = _StubOC([[{"uri": ""}, {"uri": "u1"}, {"content": "no uri"}]])
            result = await memory_record_snapshot(oc)
            self.assertEqual(set(result.keys()), {"u1"})

        self._run(check())

    def test_empty_first_page_returns_empty_dict(self):
        async def check():
            oc = _StubOC([[]])
            result = await memory_record_snapshot(oc)
            self.assertEqual(result, {})
            self.assertEqual(len(oc.calls), 1)

        self._run(check())


class TestMapSessionUris(unittest.TestCase):
    """Two-pass span+time-ref mapping with return_all kwarg."""

    def _records(self, *records: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {str(r["uri"]): dict(r) for r in records}

    def test_return_all_false_picks_single_best_per_session(self):
        # Two sessions, each with two candidates of varying tightness.
        records = self._records(
            {
                "uri": "opencortex://m/session1-tight",
                "session_id": "conv-1",
                "msg_range": [0, 1],
            },
            {
                "uri": "opencortex://m/cumulative",
                "session_id": "conv-1",
                "msg_range": [0, 2],
            },
        )
        result = map_session_uris(
            session_spans={1: (0, 1)},
            session_time_refs={1: set()},
            records_by_uri=records,
            conversation_session_id="conv-1",
            return_all=False,
        )
        # Tightest overlap wins (session1-tight: [0,1] vs (0,1) → width 2,
        # cumulative: [0,2] vs (0,1) → width 2 too — tie-break on smaller
        # record-width: session1-tight has width=1 vs cumulative width=2).
        self.assertEqual(result[1], ["opencortex://m/session1-tight"])

    def test_return_all_true_returns_full_sorted_list(self):
        # Reproduces test_ingest_prefers_tightest_overlapping_merged_record
        # (tests/test_locomo_bench.py:197-235) at the helper level so the
        # locomo-side semantic is locked in this module's own tests.
        records = self._records(
            {
                "uri": "opencortex://m/cumulative",
                "session_id": "locomo-conv-1",
                "msg_range": [0, 2],
            },
            {
                "uri": "opencortex://m/session1-tight",
                "session_id": "locomo-conv-1",
                "msg_range": [0, 1],
            },
            {
                "uri": "opencortex://m/session2-tight",
                "session_id": "locomo-conv-1",
                "msg_range": [2, 2],
            },
        )
        result = map_session_uris(
            session_spans={1: (0, 1), 2: (2, 2)},
            session_time_refs={1: set(), 2: set()},
            records_by_uri=records,
            conversation_session_id="locomo-conv-1",
            return_all=True,
        )
        # Session 1 — tightest first: session1-tight (overlap 2),
        # cumulative (overlap 2 but wider record).
        self.assertEqual(result[1][0], "opencortex://m/session1-tight")
        self.assertIn("opencortex://m/cumulative", result[1])
        # Session 2 — both session2-tight (msg_range [2,2]) and
        # cumulative (msg_range [0,2]) overlap with span (2,2). Tightest
        # ranks first: session2-tight has overlap_width=1 vs cumulative
        # also overlap_width=1, but cumulative has wider record-width
        # (2) so session2-tight wins the tie-break.
        self.assertEqual(result[2][0], "opencortex://m/session2-tight")
        self.assertIn("opencortex://m/cumulative", result[2])

    def test_filters_records_with_mismatched_session_id(self):
        records = self._records(
            {
                "uri": "opencortex://m/wrong-conv",
                "session_id": "conv-OTHER",
                "msg_range": [0, 1],
            },
            {
                "uri": "opencortex://m/right",
                "session_id": "conv-1",
                "msg_range": [0, 1],
            },
        )
        result = map_session_uris(
            session_spans={1: (0, 1)},
            session_time_refs={1: set()},
            records_by_uri=records,
            conversation_session_id="conv-1",
            return_all=False,
        )
        self.assertEqual(result[1], ["opencortex://m/right"])

    def test_record_with_missing_session_id_passes_through(self):
        # Records without a session_id meta are not filtered out.
        records = self._records(
            {"uri": "opencortex://m/no-sid", "msg_range": [0, 1]},
        )
        result = map_session_uris(
            session_spans={1: (0, 1)},
            session_time_refs={1: set()},
            records_by_uri=records,
            conversation_session_id="conv-1",
            return_all=False,
        )
        self.assertEqual(result[1], ["opencortex://m/no-sid"])

    def test_session_with_no_overlap_returns_empty_list(self):
        records = self._records(
            {
                "uri": "opencortex://m/far-away",
                "session_id": "conv-1",
                "msg_range": [100, 200],
            },
        )
        result = map_session_uris(
            session_spans={1: (0, 5)},
            session_time_refs={1: set()},
            records_by_uri=records,
            conversation_session_id="conv-1",
            return_all=False,
        )
        self.assertEqual(result[1], [])

    def test_time_refs_fallback_when_no_msg_range_match(self):
        # No record has overlapping msg_range, but one record has a
        # matching time_ref so the time_refs fallback kicks in.
        records = self._records(
            {
                "uri": "opencortex://m/no-range-but-time",
                "session_id": "conv-1",
                "meta": {"time_refs": ["2026-04-25"]},
            },
        )
        result = map_session_uris(
            session_spans={1: (0, 5)},
            session_time_refs={1: {"2026-04-25"}},
            records_by_uri=records,
            conversation_session_id="conv-1",
            return_all=False,
        )
        self.assertEqual(result[1], ["opencortex://m/no-range-but-time"])

    def test_time_refs_fallback_skipped_when_msg_range_already_matched(self):
        # If span-based pass found a candidate, time_refs fallback does
        # NOT add more candidates for that session.
        records = self._records(
            {
                "uri": "opencortex://m/span-match",
                "session_id": "conv-1",
                "msg_range": [0, 1],
            },
            {
                "uri": "opencortex://m/time-only",
                "session_id": "conv-1",
                "meta": {"time_refs": ["2026-04-25"]},
            },
        )
        result = map_session_uris(
            session_spans={1: (0, 1)},
            session_time_refs={1: {"2026-04-25"}},
            records_by_uri=records,
            conversation_session_id="conv-1",
            return_all=True,
        )
        # Only span-match is returned; time-only never enters the candidate list.
        self.assertEqual(result[1], ["opencortex://m/span-match"])

    def test_empty_records_dict_returns_empty_lists_per_session(self):
        result = map_session_uris(
            session_spans={1: (0, 1), 2: (2, 3)},
            session_time_refs={1: set(), 2: set()},
            records_by_uri={},
            conversation_session_id="conv-1",
            return_all=False,
        )
        self.assertEqual(result, {1: [], 2: []})


class _DualPathOCStub:
    """OC stub that supports both store-path and mcp-path call chains.

    Returns the same record set via either path so the equivalence test
    can assert both produce identical session→URI mappings.
    """

    def __init__(self, records: List[Dict[str, Any]]) -> None:
        self._records = records
        self.benchmark_ingest_calls: List[Dict[str, Any]] = []
        self.context_end_calls: List[str] = []
        self.memory_list_calls: List[Dict[str, Any]] = []

    async def benchmark_conversation_ingest(self, **kwargs: Any) -> Dict[str, Any]:
        self.benchmark_ingest_calls.append(dict(kwargs))
        # Store path: response carries the new records directly.
        return {"records": [dict(r) for r in self._records]}

    async def context_end(self, *, session_id: str) -> Dict[str, Any]:
        self.context_end_calls.append(session_id)
        return {"status": "closed"}

    async def memory_list(self, **kwargs: Any) -> Dict[str, Any]:
        self.memory_list_calls.append(dict(kwargs))
        # mcp path: a memory_list snapshot returns the same records.
        # First page is full result; second page empty so the loop exits.
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", 500)
        if offset == 0:
            return {"results": [dict(r) for r in self._records]}
        return {"results": []}


class TestStoreMcpEquivalence(unittest.IsolatedAsyncioTestCase):
    """REVIEW closure tracker TG-3 — store-path and mcp-path must produce
    identical session→URI mappings for the same fixture.

    Locks the equivalence the §25 Phase 7 refactor preserves. If a future
    change silently mutates either path's record-extraction shape,
    this test surfaces the divergence with a clear assertion.
    """

    SESSION_SPANS = {1: (0, 1), 2: (2, 3)}
    SESSION_TIME_REFS = {1: set(), 2: set()}
    CONVERSATION_SESSION_ID = "conv-1"

    FIXTURE_RECORDS: List[Dict[str, Any]] = [
        {
            "uri": "opencortex://m/sess1-tight",
            "session_id": "conv-1",
            "msg_range": [0, 1],
            "content": "session 1 leaf",
        },
        {
            "uri": "opencortex://m/sess2-tight",
            "session_id": "conv-1",
            "msg_range": [2, 3],
            "content": "session 2 leaf",
        },
        {
            "uri": "opencortex://m/cumulative",
            "session_id": "conv-1",
            "msg_range": [0, 3],
            "content": "spans both sessions",
        },
    ]

    async def _store_path_mapping(
        self, *, return_all: bool
    ) -> Dict[int, List[str]]:
        """Exercise the store-path chain end-to-end with the helpers."""
        oc = _DualPathOCStub(self.FIXTURE_RECORDS)
        payload = await oc.benchmark_conversation_ingest(
            session_id=self.CONVERSATION_SESSION_ID,
            segments=[],
            include_session_summary=False,
        )
        records_by_uri = extract_records_by_uri(payload)
        return map_session_uris(
            session_spans=self.SESSION_SPANS,
            session_time_refs=self.SESSION_TIME_REFS,
            records_by_uri=records_by_uri,
            conversation_session_id=self.CONVERSATION_SESSION_ID,
            return_all=return_all,
        )

    async def _mcp_path_mapping(
        self, *, return_all: bool
    ) -> Dict[int, List[str]]:
        """Exercise the mcp-path chain: context_end + before/after diff."""
        # before snapshot — empty (nothing ingested yet)
        empty_oc = _DualPathOCStub([])
        before_records = await memory_record_snapshot(empty_oc)
        # ingest happens (mocked); then after snapshot returns the records
        oc = _DualPathOCStub(self.FIXTURE_RECORDS)
        await oc.context_end(session_id=self.CONVERSATION_SESSION_ID)
        after_records = await memory_record_snapshot(oc)
        # Diff: only records new since `before`
        new_records = {
            uri: record
            for uri, record in after_records.items()
            if uri not in before_records
        }
        return map_session_uris(
            session_spans=self.SESSION_SPANS,
            session_time_refs=self.SESSION_TIME_REFS,
            records_by_uri=new_records,
            conversation_session_id=self.CONVERSATION_SESSION_ID,
            return_all=return_all,
        )

    async def test_store_and_mcp_paths_produce_equivalent_uri_mapping(self):
        """The two paths must produce the same Dict[int, List[str]] for
        the same fixture. Asserts both ``return_all=False`` and
        ``return_all=True`` shapes match.
        """
        # Single-best (conversation.py contract)
        store_single = await self._store_path_mapping(return_all=False)
        mcp_single = await self._mcp_path_mapping(return_all=False)
        self.assertEqual(store_single, mcp_single)

        # Full sorted (locomo.py contract)
        store_all = await self._store_path_mapping(return_all=True)
        mcp_all = await self._mcp_path_mapping(return_all=True)
        self.assertEqual(store_all, mcp_all)

    async def test_empty_records_produces_empty_mapping_on_both_paths(self):
        """Edge case: no records in either path → both return empty lists
        per session. Confirms divergence cannot creep in via the empty
        case (a common refactor footgun)."""
        empty_oc = _DualPathOCStub([])

        # store path with empty payload
        payload = await empty_oc.benchmark_conversation_ingest(session_id="x")
        store_mapping = map_session_uris(
            session_spans=self.SESSION_SPANS,
            session_time_refs=self.SESSION_TIME_REFS,
            records_by_uri=extract_records_by_uri(payload),
            conversation_session_id=self.CONVERSATION_SESSION_ID,
            return_all=False,
        )

        # mcp path with empty memory_list
        snapshot = await memory_record_snapshot(empty_oc)
        mcp_mapping = map_session_uris(
            session_spans=self.SESSION_SPANS,
            session_time_refs=self.SESSION_TIME_REFS,
            records_by_uri=snapshot,
            conversation_session_id=self.CONVERSATION_SESSION_ID,
            return_all=False,
        )

        self.assertEqual(store_mapping, mcp_mapping)
        self.assertEqual(store_mapping, {1: [], 2: []})


if __name__ == "__main__":
    unittest.main()
