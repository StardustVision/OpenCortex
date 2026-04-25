"""Behavior parity tests for SessionRecordsRepository.

Locks the U1 mechanical extraction (see
``docs/plans/2026-04-25-005-refactor-benchmark-ingest-server-side-design-patterns-plan.md``):
the repository methods produce identical results to the legacy
``ContextManager._load_session_*`` / ``_session_layer_counts`` /
session_summary helpers for the same fixtures. U2 will extend this
file with scope + paging + overflow scenarios; U1 only checks parity
on the existing helper shape.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Dict, List

from opencortex.context.session_records import (
    SessionRecordOverflowError,
    SessionRecordsRepository,
    record_msg_range,
    record_text,
)


class _FakeStorage:
    """Minimal storage stand-in: holds records in a single collection."""

    def __init__(self, records: List[Dict[str, Any]]) -> None:
        self._records = records

    async def filter(self, _collection, where, limit=10000):
        # Mirror the in-memory test storage: emit records matching the
        # ``session_id`` / ``uri`` conds the legacy helpers used.
        targets: List[str] = []
        field = ""
        if isinstance(where, dict) and where.get("op") == "and":
            for cond in where.get("conds", []) or []:
                if cond.get("field") == "session_id":
                    field = "session_id"
                    targets = list(cond.get("conds") or [])
                    break
        elif isinstance(where, dict) and where.get("op") == "must":
            field = where.get("field", "")
            targets = list(where.get("conds") or [])
        if not field:
            return list(self._records)[:limit]

        out: List[Dict[str, Any]] = []
        for record in self._records:
            value = (
                record.get(field)
                if field != "uri"
                else str(record.get("uri", "") or "")
            )
            if field == "session_id":
                value = (
                    str((record.get("meta") or {}).get("session_id"))
                    if "meta" in record and record["meta"].get("session_id")
                    else value
                )
            if value in targets:
                out.append(record)
            if len(out) >= limit:
                break
        return out


def _merged(uri: str, session_id: str, msg_range, source_uri="src"):
    return {
        "uri": uri,
        "session_id": session_id,
        "meta": {
            "layer": "merged",
            "session_id": session_id,
            "source_uri": source_uri,
            "msg_range": list(msg_range),
        },
    }


def _directory(uri: str, session_id: str, msg_range, source_uri="src"):
    return {
        "uri": uri,
        "session_id": session_id,
        "meta": {
            "layer": "directory",
            "session_id": session_id,
            "source_uri": source_uri,
            "msg_range": list(msg_range),
        },
    }


def _summary(uri: str, session_id: str):
    return {
        "uri": uri,
        "session_id": session_id,
        "meta": {
            "layer": "session_summary",
            "session_id": session_id,
        },
    }


class TestSessionRecordsRepository(unittest.TestCase):
    """Repo-level happy path / edge case parity with legacy helpers."""

    def _run(self, coro):
        return asyncio.run(coro)

    def _repo(self, records):
        storage = _FakeStorage(records)
        return SessionRecordsRepository(
            storage=storage, collection_resolver=lambda: "context"
        )

    def test_load_merged_filters_layer_and_sorts_by_msg_range(self):
        """Merged-layer filter + msg_range ascending sort, source_uri scope."""

        async def check():
            records = [
                _merged("u3", "s1", [4, 5]),
                _directory("d1", "s1", [0, 9]),  # wrong layer
                _merged("u1", "s1", [0, 1]),
                _merged("u2", "s1", [2, 3]),
                _merged("ux", "s1", [0, 0], source_uri="other"),  # other source
                _merged("uy", "s2", [0, 1]),  # other session
            ]
            repo = self._repo(records)
            out = await repo.load_merged(session_id="s1", source_uri="src")
            self.assertEqual([r["uri"] for r in out], ["u1", "u2", "u3"])

        self._run(check())

    def test_load_merged_no_source_uri_scope_returns_all_layers_for_session(self):
        """source_uri=None preserves legacy behavior — no source filter."""

        async def check():
            records = [
                _merged("u1", "s1", [0, 1], source_uri="A"),
                _merged("u2", "s1", [2, 3], source_uri="B"),
            ]
            repo = self._repo(records)
            out = await repo.load_merged(session_id="s1")
            self.assertEqual([r["uri"] for r in out], ["u1", "u2"])

        self._run(check())

    def test_load_directories_filters_layer_and_sorts(self):
        """Directory-layer filter + msg_range ascending sort."""

        async def check():
            records = [
                _directory("d2", "s1", [10, 20]),
                _merged("u1", "s1", [0, 1]),  # wrong layer
                _directory("d1", "s1", [0, 9]),
            ]
            repo = self._repo(records)
            out = await repo.load_directories(session_id="s1", source_uri="src")
            self.assertEqual([r["uri"] for r in out], ["d1", "d2"])

        self._run(check())

    def test_layer_counts_groups_by_layer(self):
        """Layer counts dict uses ``meta.layer`` as key."""

        async def check():
            records = [
                _merged("u1", "s1", [0, 1]),
                _merged("u2", "s1", [2, 3]),
                _directory("d1", "s1", [0, 1]),
                _summary("sum1", "s1"),
            ]
            repo = self._repo(records)
            out = await repo.layer_counts("s1")
            self.assertEqual(out, {"merged": 2, "directory": 1, "session_summary": 1})

        self._run(check())

    def test_load_summary_returns_record_or_none(self):
        """``load_summary`` fetches by URI; returns None when absent."""

        async def check():
            records = [_summary("opencortex://t/u/session/s1/summary", "s1")]
            repo = self._repo(records)
            hit = await repo.load_summary("opencortex://t/u/session/s1/summary")
            self.assertIsNotNone(hit)
            self.assertEqual(hit["uri"], "opencortex://t/u/session/s1/summary")

            miss = await repo.load_summary("opencortex://nonexistent")
            self.assertIsNone(miss)

        self._run(check())

    def test_record_msg_range_extracts_normalized_range(self):
        """Pure helper: meta.msg_range → tuple, fallback to msg_index."""
        self.assertEqual(record_msg_range({"meta": {"msg_range": [0, 5]}}), (0, 5))
        self.assertEqual(record_msg_range({"msg_range": [3, 3]}), (3, 3))
        self.assertEqual(record_msg_range({"meta": {"msg_index": 7}}), (7, 7))
        self.assertIsNone(record_msg_range({}))
        self.assertIsNone(record_msg_range({"meta": {"msg_range": [5, 0]}}))  # inverted

    def test_record_text_prefers_content_then_overview_then_abstract(self):
        """Pure helper: text picker order."""
        self.assertEqual(record_text({"content": "c", "overview": "o"}), "c")
        self.assertEqual(record_text({"overview": "o", "abstract": "a"}), "o")
        self.assertEqual(record_text({"abstract": "a"}), "a")
        self.assertEqual(record_text({}), "")
        self.assertEqual(record_text({"content": "  "}), "")  # whitespace stripped


class TestSessionRecordsRepositoryScopeAndOverflow(unittest.TestCase):
    """U2: tenant/user scope discipline + cursor pagination + overflow guard."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_tenant_user_kwargs_push_scope_into_filter(self):
        """``load_merged(tenant_id=..., user_id=...)`` adds source_tenant_id/user_id conds."""

        async def check():
            captured: List[Dict[str, Any]] = []

            class _CapturingStorage:
                async def filter(self, _coll, where, limit=10000):
                    captured.append(where)
                    return []

            repo = SessionRecordsRepository(
                storage=_CapturingStorage(), collection_resolver=lambda: "context"
            )
            await repo.load_merged(
                session_id="s1", tenant_id="t1", user_id="u1"
            )
            self.assertEqual(len(captured), 1)
            conds = captured[0]["conds"]
            fields = {c["field"] for c in conds}
            self.assertIn("session_id", fields)
            self.assertIn("source_tenant_id", fields)
            self.assertIn("source_user_id", fields)

        self._run(check())

    def test_no_tenant_user_preserves_legacy_filter_shape(self):
        """When tenant/user not provided, only session_id cond is pushed."""

        async def check():
            captured: List[Dict[str, Any]] = []

            class _CapturingStorage:
                async def filter(self, _coll, where, limit=10000):
                    captured.append(where)
                    return []

            repo = SessionRecordsRepository(
                storage=_CapturingStorage(), collection_resolver=lambda: "context"
            )
            await repo.load_merged(session_id="s1")
            self.assertEqual(len(captured), 1)
            conds = captured[0]["conds"]
            fields = {c["field"] for c in conds}
            self.assertEqual(fields, {"session_id"})

        self._run(check())

    def test_overflow_raises_session_record_overflow_error(self):
        """Filter-fallback path: hitting the page-size * max-pages cap raises."""

        async def check():
            class _SaturatedStorage:
                # No `scroll` attribute → falls through to filter() path.
                async def filter(self, _coll, _where, limit=10000):
                    # Return exactly `limit` records — triggers overflow.
                    return [
                        {"meta": {"layer": "merged", "msg_range": [i, i]}}
                        for i in range(limit)
                    ]

            # Tiny budget so the test runs fast.
            repo = SessionRecordsRepository(
                storage=_SaturatedStorage(),
                collection_resolver=lambda: "context",
                page_size=10,
                max_pages=2,
            )
            with self.assertRaises(SessionRecordOverflowError) as ctx:
                await repo.load_merged(session_id="huge")
            self.assertEqual(ctx.exception.session_id, "huge")
            self.assertEqual(ctx.exception.method, "load_merged")
            self.assertEqual(ctx.exception.count_at_stop, 20)

        self._run(check())

    def test_scroll_pagination_loops_until_cursor_none(self):
        """Scroll-supporting storage: loop continues until cursor exhausted."""

        async def check():
            class _ScrollingStorage:
                def __init__(self):
                    # Three pages worth of data.
                    self._pages = [
                        ([{"meta": {"layer": "merged", "msg_range": [0, 0]}}], "c1"),
                        ([{"meta": {"layer": "merged", "msg_range": [1, 1]}}], "c2"),
                        ([{"meta": {"layer": "merged", "msg_range": [2, 2]}}], None),
                    ]
                    self._index = 0

                async def scroll(self, _coll, filter=None, limit=10, cursor=None):
                    page = self._pages[self._index]
                    self._index += 1
                    return page

            repo = SessionRecordsRepository(
                storage=_ScrollingStorage(),
                collection_resolver=lambda: "context",
                page_size=1,
                max_pages=10,
            )
            out = await repo.load_merged(session_id="s")
            # 3 records collected across 3 scroll calls; sorted by msg_range.
            self.assertEqual([r["meta"]["msg_range"] for r in out], [[0, 0], [1, 1], [2, 2]])

        self._run(check())


if __name__ == "__main__":
    unittest.main()
