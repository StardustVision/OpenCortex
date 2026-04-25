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
from unittest.mock import AsyncMock, MagicMock, patch

from opencortex.context.session_records import (
    SessionRecordOverflowError,
    SessionRecordsRepository,
    record_msg_range,
    record_text,
)
from opencortex.storage.qdrant.adapter import QdrantStorageAdapter


class _FakeStorage:
    """Minimal storage stand-in: holds records in a single collection.

    Evaluates the filter DSL recursively so every cond pushed into
    ``_build_session_filter`` (session_id, source_tenant_id,
    source_user_id, meta.source_uri) actually filters records — this
    is required for repository tests to faithfully exercise the
    server-side filter (PERF-02) instead of accidentally relying on
    an in-memory post-filter that no longer exists.
    """

    def __init__(self, records: List[Dict[str, Any]]) -> None:
        self._records = records

    @staticmethod
    def _resolve_field(record: Dict[str, Any], field_name: str) -> Any:
        """Walk dot-paths into nested dicts; mirrors Qdrant's nested key."""
        if "." not in field_name:
            # session_id may live at the top level OR inside meta —
            # check meta first because that's where session_records
            # writes set it.
            if field_name == "session_id":
                meta = record.get("meta") or {}
                if isinstance(meta, dict) and meta.get("session_id"):
                    return meta["session_id"]
            return record.get(field_name)
        cursor: Any = record
        for part in field_name.split("."):
            if isinstance(cursor, dict):
                cursor = cursor.get(part)
            else:
                return None
            if cursor is None:
                return None
        return cursor

    def _eval(self, record: Dict[str, Any], filt: Dict[str, Any]) -> bool:
        op = filt.get("op", "")
        if op == "and":
            return all(self._eval(record, c) for c in filt.get("conds", []) or [])
        if op == "must":
            val = self._resolve_field(record, filt.get("field", ""))
            return val in (filt.get("conds") or [])
        # Unknown ops are a test-fixture bug, not a silent match — if a
        # production caller starts passing ``or``/``range``/``contains``
        # through SessionRecordsRepository, the fixture must be taught
        # the new op explicitly rather than letting every record slip
        # through (REVIEW MAINT-02 — latent correctness trap).
        raise NotImplementedError(
            f"_FakeStorage._eval does not support op={op!r}; "
            "extend the fixture if SessionRecordsRepository started "
            "pushing this op into the storage filter."
        )

    async def filter(self, _collection, where, limit=10000):
        if not isinstance(where, dict):
            return list(self._records)[:limit]
        out: List[Dict[str, Any]] = []
        for record in self._records:
            if self._eval(record, where):
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

    def test_load_layers_partitions_by_layer_in_one_scroll(self):
        """``load_layers`` returns multiple layers from a single storage call.

        REVIEW closure tracker PERF-01 — used by
        ``_generate_session_summary`` to halve storage round-trips
        when both ``merged`` and ``directory`` layers are needed for
        the same session.
        """

        class _CountingStorage:
            def __init__(self, records):
                self._records = records
                self.filter_calls = 0

            async def filter(self, _coll, _where, limit=10000):
                self.filter_calls += 1
                return list(self._records)

        async def check():
            records = [
                _merged("u1", "s1", [0, 1]),
                _merged("u2", "s1", [2, 3]),
                _directory("d1", "s1", [0, 3]),
                _summary("sum1", "s1"),
            ]
            storage = _CountingStorage(records)
            repo = SessionRecordsRepository(
                storage=storage, collection_resolver=lambda: "context"
            )
            out = await repo.load_layers(
                layers=["merged", "directory"],
                session_id="s1",
                source_uri="src",
            )
            self.assertEqual([r["uri"] for r in out["merged"]], ["u1", "u2"])
            self.assertEqual([r["uri"] for r in out["directory"]], ["d1"])
            # Single storage round-trip — the whole point of the API.
            self.assertEqual(storage.filter_calls, 1)
            # Non-requested layer (session_summary) absent from result.
            self.assertNotIn("session_summary", out)

        self._run(check())

    def test_load_layers_returns_empty_list_for_missing_layer(self):
        """Requested layer with zero records still gets an empty list key."""

        async def check():
            records = [_merged("u1", "s1", [0, 1])]
            repo = self._repo(records)
            out = await repo.load_layers(
                layers=["merged", "directory"],
                session_id="s1",
            )
            self.assertEqual([r["uri"] for r in out["merged"]], ["u1"])
            self.assertEqual(out["directory"], [])

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
            # cap = page_size * max_pages = 20. Repo requests cap+1 = 21
            # records and only raises when len(page) > cap, so the
            # storage returns 21 and count_at_stop reflects that.
            # REVIEW correctness-002 / ADV-U-002: the legacy ``>=`` check
            # raised at exactly cap, a false-positive boundary case.
            self.assertEqual(ctx.exception.count_at_stop, 21)

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


class TestSessionRecordsRepositorySourceUriPushdown(unittest.TestCase):
    """PERF-02: source_uri pushed into Qdrant filter via meta.source_uri.

    These tests lock the contract that the repository TRUSTS the
    server-side filter (no in-memory post-filter exists). If a future
    change re-adds the in-memory pass, the trust-the-server tests
    catch it because they seed a CapturingStorage that returns
    records with mismatched source_uri values — under the post-fix
    contract those records flow through unchanged.
    """

    def _run(self, coro):
        return asyncio.run(coro)

    def test_load_merged_pushes_source_uri_into_filter(self):
        """load_merged(source_uri=...) appends meta.source_uri must-cond."""

        async def check():
            captured: List[Dict[str, Any]] = []

            class _CapturingStorage:
                async def filter(self, _coll, where, limit=10000):
                    captured.append(where)
                    return []

            repo = SessionRecordsRepository(
                storage=_CapturingStorage(), collection_resolver=lambda: "context"
            )
            await repo.load_merged(session_id="s1", source_uri="opencortex://t/u/src")
            self.assertEqual(len(captured), 1)
            conds = captured[0]["conds"]
            source_conds = [c for c in conds if c.get("field") == "meta.source_uri"]
            self.assertEqual(len(source_conds), 1)
            self.assertEqual(source_conds[0]["op"], "must")
            self.assertEqual(source_conds[0]["conds"], ["opencortex://t/u/src"])

        self._run(check())

    def test_load_merged_omits_source_uri_when_not_provided(self):
        """source_uri=None / '' produces no meta.source_uri cond."""

        async def check():
            captured: List[Dict[str, Any]] = []

            class _CapturingStorage:
                async def filter(self, _coll, where, limit=10000):
                    captured.append(where)
                    return []

            repo = SessionRecordsRepository(
                storage=_CapturingStorage(), collection_resolver=lambda: "context"
            )
            # None
            await repo.load_merged(session_id="s1")
            # Empty string (legacy fallback — falsy, no filter added)
            await repo.load_merged(session_id="s2", source_uri="")

            self.assertEqual(len(captured), 2)
            for where in captured:
                fields = {c["field"] for c in where["conds"]}
                self.assertNotIn(
                    "meta.source_uri",
                    fields,
                    "meta.source_uri cond must be omitted when "
                    "source_uri is None or empty string",
                )

        self._run(check())

    def test_load_merged_trusts_server_filter(self):
        """Repository must NOT re-filter source_uri in Python.

        Two-part regression lock:
        1. The mismatched record flows through unchanged — proves no
           in-memory post-filter exists.
        2. The captured ``where`` actually contains the meta.source_uri
           cond with the requested value — proves the push-down ran
           (otherwise part 1 would pass for the wrong reason: a missing
           filter still returns the record but without any server-side
           scoping).
        """

        async def check():
            captured: List[Dict[str, Any]] = []

            class _ServerLyingStorage:
                """Pretends Qdrant returned records that don't actually match
                the source_uri filter — exercises whether the repository
                trusts the server result."""

                async def filter(self, _coll, where, limit=10000):
                    captured.append(where)
                    return [
                        {
                            "uri": "u1",
                            "session_id": "s1",
                            "meta": {
                                "layer": "merged",
                                "session_id": "s1",
                                "source_uri": "opencortex://wrong/source",
                                "msg_range": [0, 0],
                            },
                        }
                    ]

            repo = SessionRecordsRepository(
                storage=_ServerLyingStorage(), collection_resolver=lambda: "context"
            )
            out = await repo.load_merged(
                session_id="s1", source_uri="opencortex://requested/src"
            )
            self.assertEqual(
                [r["uri"] for r in out],
                ["u1"],
                "repository must trust the server-side filter and return "
                "the record unchanged; if this fails, an in-memory "
                "post-filter has been re-introduced (REVIEW PERF-02 "
                "regression).",
            )
            # Push-down actually happened — protects against a degenerate
            # pass where the in-memory filter is removed without the
            # server-side cond being added (REVIEW TG-02).
            self.assertEqual(len(captured), 1)
            source_conds = [
                c for c in captured[0]["conds"]
                if c.get("field") == "meta.source_uri"
            ]
            self.assertEqual(len(source_conds), 1)
            self.assertEqual(source_conds[0]["op"], "must")
            self.assertEqual(
                source_conds[0]["conds"], ["opencortex://requested/src"]
            )

        self._run(check())

    def test_load_directories_pushes_source_uri_into_filter(self):
        """load_directories shares the push-down with load_merged.

        Asserts both that the meta.source_uri field is present AND that
        the cond value matches the requested source_uri — a missing
        value would still satisfy the looser `field in fields` check
        but would not actually scope the query (REVIEW TG-03)."""

        async def check():
            captured: List[Dict[str, Any]] = []

            class _CapturingStorage:
                async def filter(self, _coll, where, limit=10000):
                    captured.append(where)
                    return []

            repo = SessionRecordsRepository(
                storage=_CapturingStorage(), collection_resolver=lambda: "context"
            )
            await repo.load_directories(
                session_id="s1", source_uri="opencortex://t/u/src"
            )
            source_conds = [
                c for c in captured[0]["conds"]
                if c.get("field") == "meta.source_uri"
            ]
            self.assertEqual(len(source_conds), 1)
            self.assertEqual(source_conds[0]["op"], "must")
            self.assertEqual(
                source_conds[0]["conds"], ["opencortex://t/u/src"]
            )

        self._run(check())

    def test_load_layers_pushes_source_uri_into_filter(self):
        """load_layers shares the push-down with load_merged/load_directories.

        Same value-level assertion as load_directories — locks parity
        across all three repository entry points (REVIEW TG-03)."""

        async def check():
            captured: List[Dict[str, Any]] = []

            class _CapturingStorage:
                async def filter(self, _coll, where, limit=10000):
                    captured.append(where)
                    return []

            repo = SessionRecordsRepository(
                storage=_CapturingStorage(), collection_resolver=lambda: "context"
            )
            await repo.load_layers(
                layers=["merged", "directory"],
                session_id="s1",
                source_uri="opencortex://t/u/src",
            )
            source_conds = [
                c for c in captured[0]["conds"]
                if c.get("field") == "meta.source_uri"
            ]
            self.assertEqual(len(source_conds), 1)
            self.assertEqual(source_conds[0]["op"], "must")
            self.assertEqual(
                source_conds[0]["conds"], ["opencortex://t/u/src"]
            )

        self._run(check())


class TestEnsureScalarIndexes(unittest.TestCase):
    """U1 verification: payload index ensure runs idempotently on every
    create_collection call — both the new-collection path and the
    existing-collection path.

    This test exercises the QdrantStorageAdapter directly by mocking
    its client. We don't need a live Qdrant — just need to assert
    that create_payload_index is called for every ScalarIndex field
    on both branches.
    """

    def _run(self, coro):
        return asyncio.run(coro)

    def _build_adapter_with_mock_client(self, *, exists: bool):
        adapter = QdrantStorageAdapter(path="/tmp/_pinst", embedding_dim=4)
        mock_client = MagicMock()
        mock_client.collection_exists = AsyncMock(return_value=exists)
        mock_client.create_payload_index = AsyncMock()
        mock_client.create_collection = AsyncMock()
        return adapter, mock_client

    def test_existing_collection_still_runs_index_ensure(self):
        """create_collection(name) on an existing collection still
        invokes _ensure_scalar_indexes — proves the migration path
        for newly-declared indexes (e.g. meta.source_uri) does not
        require recreating the collection."""

        async def check():
            adapter, mock_client = self._build_adapter_with_mock_client(exists=True)
            with patch.object(
                adapter, "_ensure_client", AsyncMock(return_value=mock_client)
            ):
                schema = {
                    "Fields": [{"FieldName": "uri", "FieldType": "string"}],
                    "ScalarIndex": ["uri", "meta.source_uri"],
                }
                result = await adapter.create_collection("ctx", schema)
                self.assertFalse(
                    result, "existing collection should return False"
                )
                # No collection recreated on the exists=True branch.
                mock_client.create_collection.assert_not_called()
                # Both index fields ensured even though collection wasn't new.
                indexed_fields = {
                    call.kwargs.get("field_name")
                    for call in mock_client.create_payload_index.call_args_list
                }
                self.assertEqual(indexed_fields, {"uri", "meta.source_uri"})

        self._run(check())

    def test_new_collection_runs_index_ensure(self):
        """create_collection(name) on a new collection creates the
        collection AND invokes _ensure_scalar_indexes for every
        ScalarIndex field — locks parity between the new-collection
        path and the migration path so a fresh deploy and a long-lived
        deploy end up with the same index set (REVIEW TG-01)."""

        async def check():
            adapter, mock_client = self._build_adapter_with_mock_client(exists=False)
            with patch.object(
                adapter, "_ensure_client", AsyncMock(return_value=mock_client)
            ):
                schema = {
                    "Fields": [{"FieldName": "uri", "FieldType": "string"}],
                    "ScalarIndex": ["uri", "meta.source_uri"],
                }
                result = await adapter.create_collection("ctx", schema)
                self.assertTrue(
                    result, "new collection should return True"
                )
                # Collection actually created on the exists=False branch.
                mock_client.create_collection.assert_called_once()
                # Same index fields as the migration path — fresh and
                # long-lived deploys end up identical.
                indexed_fields = {
                    call.kwargs.get("field_name")
                    for call in mock_client.create_payload_index.call_args_list
                }
                self.assertEqual(indexed_fields, {"uri", "meta.source_uri"})

        self._run(check())


if __name__ == "__main__":
    unittest.main()
