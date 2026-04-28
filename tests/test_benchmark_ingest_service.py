"""Direct unit tests for BenchmarkConversationIngestService.

REVIEW closure tracker T-01 / T-02 / T-03 — exercises the service in
isolation so the six responsibilities (normalize, idempotent_hit,
write_merged_leaves with sibling-cancel, recompose+summarize with
RecompositionError drain, build_response, ingest_direct_evidence) have
direct coverage that does not require the full ASGI stack.

The existing benchmark lifecycle / HTTP tests run the same code through
the public route, but they cannot reach a few branches:

- ``ValueError`` on unsupported ``ingest_shape`` (line 110 of the
  service) never fires from the route because the request DTO accepts
  any string and the service is the validator.
- Empty-segments early-exit returns before touching storage; HTTP
  tests with empty segments hit the legacy 410 shim instead.
- ``except RecompositionError`` drain branch needs the wrapper
  exception specifically — existing lifecycle tests inject
  ``RuntimeError`` and bypass the wrapper.
- Sibling-cancel-on-first-derive-failure runs after every leaf write
  has already returned in the lifecycle tests.

These tests intentionally use minimal in-memory fakes for the manager
dependencies the service touches. The service borrows ~15 manager
helpers via ``self._manager.X``; the fakes provide just the surface
each scenario exercises so the test failure mode is "service contract
broke" rather than "manager mock is wrong."
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from opencortex.context.benchmark_ingest_service import (
    BenchmarkConversationIngestService,
    BenchmarkRunCleanup,
)
from opencortex.context.manager import RecompositionError


@dataclass
class _FakeContext:
    """Minimal stand-in for the Context object orchestrator.add() returns."""

    uri: str


class _FakeOrchestrator:
    """Records add() / _complete_deferred_derive() calls for assertions."""

    def __init__(self) -> None:
        self.add_calls: List[Dict[str, Any]] = []
        self.derive_calls: List[Dict[str, Any]] = []
        self._records: Dict[str, Dict[str, Any]] = {}

    async def add(self, **kwargs: Any) -> _FakeContext:
        self.add_calls.append(kwargs)
        uri = kwargs["uri"]
        self._records[uri] = {
            "uri": uri,
            "content": kwargs.get("content", ""),
            "meta": dict(kwargs.get("meta") or {}),
        }
        return _FakeContext(uri=uri)

    async def _complete_deferred_derive(self, **kwargs: Any) -> None:
        self.derive_calls.append(kwargs)

    async def _get_record_by_uri(self, uri: str) -> Optional[Dict[str, Any]]:
        return self._records.get(uri)

    async def remove(self, uri: str) -> None:
        self._records.pop(uri, None)


class _FakeManager:
    """Manager surface the service borrows from. Just enough for the test."""

    def __init__(self) -> None:
        self._orchestrator = _FakeOrchestrator()
        self._session_locks: Dict[Tuple[str, ...], asyncio.Lock] = {}
        self._derive_semaphore = asyncio.Semaphore(4)

    # --- session-key plumbing ---
    def _make_session_key(
        self, tenant_id: str, user_id: str, session_id: str
    ) -> Tuple[str, str, str, str]:
        return ("collection", tenant_id, user_id, session_id)

    def _touch_session(self, _sk: Tuple[str, ...]) -> None:
        pass

    def _remember_session_project(self, _sk: Tuple[str, ...]) -> None:
        pass

    # --- source persist (fake — returns deterministic URI) ---
    async def _persist_rendered_conversation_source(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        transcript: List[Dict[str, Any]],
        enforce_transcript_hash: bool,
    ) -> str:
        return f"opencortex://{tenant_id}/{user_id}/session/conversations/{session_id}/source"

    # --- merged-leaf URI builders ---
    def _merged_leaf_uri(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
        msg_range: List[int],
    ) -> str:
        return (
            f"opencortex://{tenant_id}/{user_id}/memories/events/"
            f"{session_id}-{msg_range[0]:06d}-{msg_range[1]:06d}"
        )

    def _build_recomposition_segments(self, entries: List[Any]) -> List[Dict[str, Any]]:
        return [
            {
                "messages": [entry["text"]],
                "msg_range": [entry["msg_start"], entry["msg_end"]],
                "source_records": [entry["source_record"]],
            }
            for entry in entries
        ]

    async def _aggregate_records_metadata(
        self, _records: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        return {}

    @staticmethod
    def _decorate_message_text(text: str, _meta: Dict[str, Any]) -> str:
        return text

    # --- recompose + summary stubs (override per test) ---
    async def _run_full_session_recomposition(self, **_kwargs: Any) -> List[str]:
        return []

    async def _generate_session_summary(self, **_kwargs: Any) -> Optional[str]:
        return None

    def _session_summary_uri(
        self, tenant_id: str, user_id: str, session_id: str
    ) -> str:
        return f"opencortex://{tenant_id}/{user_id}/session/{session_id}/summary"

    async def _purge_records_and_fs_subtree(self, _uris: List[str]) -> None:
        pass

    @staticmethod
    def _segment_anchor_terms(_record: Dict[str, Any]) -> set:
        return set()

    @staticmethod
    def _segment_time_refs(_record: Dict[str, Any]) -> set:
        return set()


class _FakeRepo:
    """Repository stand-in that lets each test seed return values."""

    def __init__(self) -> None:
        self.merged_records: List[Dict[str, Any]] = []
        self.summary_record: Optional[Dict[str, Any]] = None
        self.load_summary_should_raise: Optional[Exception] = None

    async def load_merged(self, **_kwargs: Any) -> List[Dict[str, Any]]:
        return list(self.merged_records)

    async def load_summary(self, _uri: str) -> Optional[Dict[str, Any]]:
        if self.load_summary_should_raise is not None:
            raise self.load_summary_should_raise
        return self.summary_record

    async def load_directories(self, **_kwargs: Any) -> List[Dict[str, Any]]:
        return []


class TestNormalizeSegments(unittest.TestCase):
    """Pure static method — no service construction needed."""

    def test_strips_empty_role_and_content(self):
        segments = [
            [
                {"role": "user", "content": "hi"},
                {"role": "", "content": "no role"},
                {"role": "assistant", "content": ""},
                {"role": "user", "content": "  "},  # whitespace-only
                {"role": "assistant", "content": "ok"},
            ]
        ]
        normalized, transcript = BenchmarkConversationIngestService._normalize_segments(
            segments
        )
        # Whitespace-only content is also stripped out.
        self.assertEqual(len(normalized), 1)
        self.assertEqual(len(normalized[0]), 2)
        self.assertEqual(normalized[0][0]["role"], "user")
        self.assertEqual(normalized[0][1]["role"], "assistant")
        self.assertEqual(transcript, normalized[0])

    def test_drops_segment_when_all_messages_empty(self):
        segments = [
            [{"role": "", "content": ""}, {"role": "user", "content": ""}],
            [{"role": "user", "content": "kept"}],
        ]
        normalized, transcript = BenchmarkConversationIngestService._normalize_segments(
            segments
        )
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0][0]["content"], "kept")
        self.assertEqual(len(transcript), 1)

    def test_returns_meta_dict_copy(self):
        meta = {"event_date": "2026-04-25"}
        segments = [[{"role": "user", "content": "hi", "meta": meta}]]
        normalized, _ = BenchmarkConversationIngestService._normalize_segments(segments)
        self.assertEqual(normalized[0][0]["meta"], meta)
        # Mutating the input meta does not affect the normalized copy.
        meta["event_date"] = "MUTATED"
        self.assertEqual(normalized[0][0]["meta"]["event_date"], "2026-04-25")


class TestIngestDispatch(unittest.TestCase):
    """Public ``ingest()`` entry point — shape validation + early exits."""

    def _run(self, coro):
        return asyncio.run(coro)

    def _service(self):
        manager = _FakeManager()
        repo = _FakeRepo()
        return (
            BenchmarkConversationIngestService(manager=manager, repo=repo),
            manager,
            repo,
        )

    def test_unsupported_ingest_shape_raises_value_error(self):
        async def check():
            service, _, _ = self._service()
            with self.assertRaises(ValueError) as ctx:
                await service.ingest(
                    session_id="s",
                    tenant_id="t",
                    user_id="u",
                    segments=[[{"role": "user", "content": "hi"}]],
                    ingest_shape="not_a_real_shape",
                )
            self.assertIn("not_a_real_shape", str(ctx.exception))

        self._run(check())

    def test_empty_normalized_segments_returns_empty_response(self):
        async def check():
            service, manager, _ = self._service()
            response = await service.ingest(
                session_id="empty",
                tenant_id="t",
                user_id="u",
                segments=[
                    [{"role": "", "content": ""}],
                    [{"role": "user", "content": "  "}],
                ],
            )
            self.assertEqual(
                response,
                {
                    "status": "ok",
                    "session_id": "empty",
                    "source_uri": None,
                    "summary_uri": None,
                    "records": [],
                },
            )
            # Nothing written to storage on the early-exit path.
            self.assertEqual(manager._orchestrator.add_calls, [])

        self._run(check())

    def test_idempotent_hit_summary_lookup_failure_degrades_to_none(self):
        """REVIEW correctness-001 — transient storage error on summary
        lookup must not fail the entire idempotent replay."""

        async def check():
            service, manager, repo = self._service()
            # Seed: prior records exist AND source is run_complete.
            source_uri = "opencortex://t/u/session/conversations/s/source"
            # Prime the orchestrator with the source record marked complete.
            manager._orchestrator._records[source_uri] = {
                "uri": source_uri,
                "meta": {"run_complete": True},
            }
            repo.merged_records = [
                {
                    "uri": "opencortex://t/u/memories/events/s-000000-000000",
                    "content": "prior",
                    "meta": {"layer": "merged", "msg_range": [0, 0]},
                }
            ]
            # Summary lookup raises a transient storage error.
            repo.load_summary_should_raise = RuntimeError("transient blip")

            response = await service.ingest(
                session_id="s",
                tenant_id="t",
                user_id="u",
                segments=[[{"role": "user", "content": "hi"}]],
            )
            # Must degrade gracefully rather than propagating.
            self.assertEqual(response["status"], "ok")
            self.assertIsNone(response["summary_uri"])
            self.assertEqual(len(response["records"]), 1)

        self._run(check())


class TestRecompositionErrorDrain(unittest.TestCase):
    """REVIEW T-03 — partial directory URIs must flow into the cleanup
    tracker when ``_run_full_session_recomposition`` raises with the
    proper wrapper. Existing lifecycle tests raise RuntimeError directly
    and bypass this branch."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_recomposition_error_drains_partial_directory_uris(self):
        async def check():
            manager = _FakeManager()
            repo = _FakeRepo()
            partial_dirs = [
                "opencortex://t/u/memories/events/s/dir-aa",
                "opencortex://t/u/memories/events/s/dir-bb",
            ]

            # Patch recompose to raise with partial URIs.
            async def _failing_recompose(**_kwargs: Any) -> List[str]:
                raise RecompositionError(
                    original=RuntimeError("disk full mid-recompose"),
                    created_uris=partial_dirs,
                )

            manager._run_full_session_recomposition = _failing_recompose
            service = BenchmarkConversationIngestService(manager=manager, repo=repo)

            cleanup = BenchmarkRunCleanup(source_uri="opencortex://t/u/src")
            with self.assertRaises(RuntimeError) as ctx:
                await service._recompose_and_summarize(
                    session_id="s",
                    tenant_id="t",
                    user_id="u",
                    source_uri="opencortex://t/u/src",
                    include_session_summary=False,
                    cleanup=cleanup,
                )
            self.assertIn("disk full", str(ctx.exception))
            # Drain happened: cleanup tracker now owns the partial dirs.
            self.assertEqual(cleanup.directory_uris, partial_dirs)

        self._run(check())


class TestSiblingCancelOnFirstDeriveFailure(unittest.TestCase):
    """REVIEW T-02 — when one derive task raises mid-flight, every other
    in-flight derive task must be cancelled before the exception
    propagates to the cleanup tracker. The lifecycle test injects
    failure AFTER all derives complete and never reaches this branch."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_sibling_derive_tasks_cancelled_when_first_fails(self):
        async def check():
            manager = _FakeManager()
            repo = _FakeRepo()

            # Track which sibling derives observe cancellation.
            cancelled_uris: List[str] = []
            failed_uri = "opencortex://t/u/memories/events/s-000001-000001"

            async def _selective_derive(**kwargs: Any) -> None:
                uri = kwargs["uri"]
                if uri == failed_uri:
                    # Brief await so siblings actually start before raising.
                    await asyncio.sleep(0)
                    raise RuntimeError(f"derive blew up on {uri}")
                # Sibling — block long enough that the failed task has
                # time to raise and trigger cancellation.
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    cancelled_uris.append(uri)
                    raise

            manager._orchestrator._complete_deferred_derive = _selective_derive

            service = BenchmarkConversationIngestService(manager=manager, repo=repo)
            cleanup = BenchmarkRunCleanup(source_uri="opencortex://t/u/src")

            # Two-segment payload so we get one failing leaf and one sibling.
            normalized = [
                [{"role": "user", "content": "first"}],
                [{"role": "user", "content": "second"}],
            ]

            with self.assertRaises(RuntimeError) as ctx:
                await service._write_merged_leaves(
                    session_id="s",
                    tenant_id="t",
                    user_id="u",
                    source_uri="opencortex://t/u/src",
                    normalized_segments=normalized,
                    cleanup=cleanup,
                )
            self.assertIn("derive blew up", str(ctx.exception))
            # Sibling observed cancellation — the gather() loop did its
            # job (REVIEW F1 / REL-01 / ADV-001 guarantee).
            self.assertGreaterEqual(len(cancelled_uris), 1)
            # Both leaves were written (cleanup tracker holds them).
            self.assertEqual(len(cleanup.merged_uris), 2)

        self._run(check())


class TestDirectEvidenceNoExtraFetch(unittest.TestCase):
    """REVIEW closure tracker PERF-03 — _ingest_direct_evidence used to
    do one ``orchestrator._get_record_by_uri`` per segment after each
    ``add()``, paying N extra sequential point lookups on the critical
    path. The fix builds the record dict from the local meta + content
    instead. This test pins both invariants: (a) the per-segment
    re-fetch never fires, and (b) the assembled record dict carries
    the fields adapters consume."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_direct_evidence_does_not_call_get_record_by_uri(self):
        async def check():
            manager = _FakeManager()
            repo = _FakeRepo()
            # Spy: count get_record_by_uri calls.
            original_get = manager._orchestrator._get_record_by_uri
            calls: List[str] = []

            async def _spy(uri: str):
                calls.append(uri)
                return await original_get(uri)

            manager._orchestrator._get_record_by_uri = _spy

            service = BenchmarkConversationIngestService(manager=manager, repo=repo)
            response = await service.ingest(
                session_id="bench_de",
                tenant_id="t",
                user_id="u",
                segments=[
                    [{"role": "user", "content": "first"}],
                    [{"role": "user", "content": "second"}],
                    [{"role": "user", "content": "third"}],
                ],
                ingest_shape="direct_evidence",
            )
            self.assertEqual(response["ingest_shape"], "direct_evidence")
            self.assertEqual(len(response["records"]), 3)
            # Per-segment re-fetch must NOT fire under the PERF-03 fix.
            self.assertEqual(calls, [])

        self._run(check())

    def test_direct_evidence_record_carries_required_export_fields(self):
        """The record dict assembled locally must round-trip through
        ``_export_memory_record`` with every field downstream HTTP
        tests assert (uri, session_id, msg_range, recomposition_stage,
        meta with benchmark anchors, content non-empty)."""

        async def check():
            manager = _FakeManager()
            repo = _FakeRepo()
            service = BenchmarkConversationIngestService(manager=manager, repo=repo)
            response = await service.ingest(
                session_id="bench_de",
                tenant_id="t",
                user_id="u",
                segments=[
                    [{"role": "user", "content": "I moved to Hangzhou."}],
                ],
                ingest_shape="direct_evidence",
            )
            record = response["records"][0]
            # The fields we care about for direct_evidence (msg_range,
            # recomposition_stage, session_id, source_uri) live in meta
            # on the assembled record dict.
            self.assertEqual(record["meta"]["session_id"], "bench_de")
            self.assertEqual(record["meta"]["msg_range"], [0, 0])
            self.assertEqual(
                record["meta"]["recomposition_stage"],
                "benchmark_direct_evidence",
            )
            # Content was hydrated from the in-memory map.
            self.assertNotEqual(record["content"], "")
            self.assertIn("Hangzhou", record["content"])

        self._run(check())


if __name__ == "__main__":
    unittest.main()
