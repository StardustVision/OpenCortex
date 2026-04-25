"""Lifecycle regression tests for the benchmark offline ingest path.

These tests exercise the post-Phase-1 contracts that the rest of the
HTTP test suite only covers via the happy path:

- AR3 — idempotent replay on identical transcript; HTTP 409 on
  same session_id + different transcript.
- AR4 — cancellation and exception paths run the cleanup tracker
  (no orphan merged / directory / summary records remain).
- AR5 — `layer_counts` is not in the response.
- AR7 — every benchmark merged leaf has its deferred derive
  scheduled and awaited before the response returns.

Reuses the in-memory app context from ``tests/test_http_server.py``
so the route, admin gate, payload-bound, source versioning, cleanup
tracker, and defer-derive scheduling are all exercised end-to-end.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Callable, Dict, List

from opencortex.http.request_context import (
    reset_request_role,
    set_request_role,
)

from tests.test_http_server import _test_app_context


_ADMIN_INGEST_URL = "/api/v1/admin/benchmark/conversation_ingest"


def _payload(session_id: str, content_suffix: str = "") -> Dict[str, Any]:
    """Build a minimal benchmark ingest payload."""
    return {
        "session_id": session_id,
        "include_session_summary": False,
        "segments": [
            {
                "messages": [
                    {
                        "role": "user",
                        "content": f"[Alice]: hello{content_suffix}",
                        "meta": {
                            "event_date": "2026-04-25",
                            "time_refs": ["2026-04-25"],
                        },
                    },
                    {
                        "role": "user",
                        "content": f"[Bob]: greetings{content_suffix}",
                        "meta": {
                            "event_date": "2026-04-25",
                            "time_refs": ["2026-04-25"],
                        },
                    },
                ]
            }
        ],
    }


def _list_session_records(orch, session_id: str) -> List[Dict[str, Any]]:
    """Return all stored records for a session, regardless of layer."""
    return orch._storage._records.get(orch._get_collection(), {}).values()


class TestBenchmarkIngestLifecycle(unittest.TestCase):
    """End-to-end lifecycle assertions for AR3 / AR4 / AR5 / AR7."""

    def _run(self, coro):
        return asyncio.run(coro)

    # -----------------------------------------------------------------
    # AR3 — Source versioning
    # -----------------------------------------------------------------

    def test_idempotent_replay_same_transcript_no_new_writes(self):
        """Same session_id + same transcript = idempotent (no new merged writes)."""

        async def check():
            async with _test_app_context() as client:
                role_token = set_request_role("admin")
                try:
                    payload = _payload("bench_idem_01")
                    r1 = await client.post(_ADMIN_INGEST_URL, json=payload)
                    self.assertEqual(r1.status_code, 200)
                    first = r1.json()
                    self.assertEqual(first["status"], "ok")

                    r2 = await client.post(_ADMIN_INGEST_URL, json=payload)
                    self.assertEqual(r2.status_code, 200)
                    second = r2.json()
                    self.assertEqual(second["status"], "ok")
                    # Idempotent reply must surface the same source URI
                    # and the same merged record set as the first ingest;
                    # the U5 short-circuit returns existing records.
                    self.assertEqual(first["source_uri"], second["source_uri"])
                    self.assertEqual(
                        sorted(r["uri"] for r in first["records"]),
                        sorted(r["uri"] for r in second["records"]),
                    )
                finally:
                    reset_request_role(role_token)

        self._run(check())

    def test_409_on_same_session_different_transcript(self):
        """Same session_id + different transcript returns 409 with hashes."""

        async def check():
            async with _test_app_context() as client:
                role_token = set_request_role("admin")
                try:
                    r1 = await client.post(
                        _ADMIN_INGEST_URL,
                        json=_payload("bench_conflict_01", content_suffix=" v1"),
                    )
                    self.assertEqual(r1.status_code, 200)

                    r2 = await client.post(
                        _ADMIN_INGEST_URL,
                        json=_payload("bench_conflict_01", content_suffix=" v2"),
                    )
                    self.assertEqual(r2.status_code, 409)
                    detail = r2.json().get("detail", {})
                    self.assertEqual(detail.get("reason"), "transcript_hash_mismatch")
                    self.assertEqual(detail.get("session_id"), "bench_conflict_01")
                    self.assertNotEqual(
                        detail.get("existing_hash"),
                        detail.get("supplied_hash"),
                    )
                    self.assertTrue(detail.get("existing_hash"))
                    self.assertTrue(detail.get("supplied_hash"))
                finally:
                    reset_request_role(role_token)

        self._run(check())

    # -----------------------------------------------------------------
    # AR4 — Cleanup tracker for failure / cancellation paths
    # -----------------------------------------------------------------

    def test_recompose_failure_cleans_up_merged_leaves(self):
        """Failure mid-recompose triggers cleanup of all written merged leaves."""

        async def check():
            async with _test_app_context() as client:
                # Reach into the orchestrator's context manager to inject
                # a recomposition failure. Patch the bound method so the
                # rest of the path runs unchanged.
                import opencortex.http.server as http_server

                orch = http_server._orchestrator
                cm = orch._context_manager
                original = cm._run_full_session_recomposition

                async def _boom(**kwargs):
                    raise RuntimeError("induced recomposition failure")

                cm._run_full_session_recomposition = _boom
                role_token = set_request_role("admin")
                try:
                    # The exception propagates through httpx ASGI
                    # transport without a server-side error handler,
                    # so we expect either 500 or the raw exception. We
                    # only care that cleanup ran afterward.
                    raised = False
                    try:
                        resp = await client.post(
                            _ADMIN_INGEST_URL,
                            json=_payload("bench_recompose_fail_01"),
                        )
                        # If FastAPI returns an envelope it is 500.
                        self.assertGreaterEqual(resp.status_code, 500)
                    except Exception:
                        raised = True
                    # Either path is acceptable; the contract under test
                    # is the cleanup, not the HTTP envelope shape.
                    del raised  # silence unused-var lint

                    # Cleanup tracker must have removed the merged leaf
                    # written before recomposition failed. The source URI
                    # is intentionally preserved (idempotent retry hook).
                    records = list(_list_session_records(orch, "bench_recompose_fail_01"))
                    layers = {
                        str((r.get("meta") or {}).get("layer", "")) for r in records
                    }
                    self.assertNotIn("merged", layers)
                    self.assertNotIn("directory", layers)
                finally:
                    cm._run_full_session_recomposition = original
                    reset_request_role(role_token)

        self._run(check())

    def test_cancelled_error_propagates_after_cleanup(self):
        """asyncio.CancelledError is caught, cleanup runs, then re-raises."""

        async def check():
            from opencortex.context.manager import _BenchmarkRunCleanup

            compensated: Dict[str, bool] = {"called": False}
            original = _BenchmarkRunCleanup.compensate

            async def _track_compensate(self, manager):
                compensated["called"] = True
                await original(self, manager)

            _BenchmarkRunCleanup.compensate = _track_compensate
            try:
                async with _test_app_context() as client:
                    import opencortex.http.server as http_server

                    cm = http_server._orchestrator._context_manager
                    saved = cm._run_full_session_recomposition

                    async def _cancelled(**kwargs):
                        raise asyncio.CancelledError()

                    cm._run_full_session_recomposition = _cancelled

                    role_token = set_request_role("admin")
                    try:
                        # CancelledError propagates through ASGI without
                        # a server-side handler. We catch BaseException
                        # because CancelledError descends from
                        # BaseException, not Exception — which is the
                        # entire point of U3.
                        try:
                            await client.post(
                                _ADMIN_INGEST_URL,
                                json=_payload("bench_cancel_01"),
                            )
                        except BaseException:
                            pass
                        self.assertTrue(
                            compensated["called"],
                            "cleanup tracker compensate should run on CancelledError",
                        )
                    finally:
                        cm._run_full_session_recomposition = saved
                        reset_request_role(role_token)
            finally:
                _BenchmarkRunCleanup.compensate = original

        self._run(check())

    # -----------------------------------------------------------------
    # AR5 — Scope leak fix (already covered in test_04d, repeat here
    # so this module is self-contained as a contract lock).
    # -----------------------------------------------------------------

    def test_response_has_no_layer_counts(self):
        """Response must not surface `layer_counts` (cross-tenant leak)."""

        async def check():
            async with _test_app_context() as client:
                role_token = set_request_role("admin")
                try:
                    resp = await client.post(
                        _ADMIN_INGEST_URL,
                        json=_payload("bench_no_layercounts"),
                    )
                    self.assertEqual(resp.status_code, 200)
                    self.assertNotIn("layer_counts", resp.json())
                finally:
                    reset_request_role(role_token)

        self._run(check())

    # -----------------------------------------------------------------
    # AR7 — Defer-derive parity
    # -----------------------------------------------------------------

    def test_deferred_derive_scheduled_for_each_merged_leaf(self):
        """Every benchmark merged leaf gets _complete_deferred_derive scheduled."""

        async def check():
            async with _test_app_context() as client:
                import opencortex.http.server as http_server

                orch = http_server._orchestrator
                derive_uris: List[str] = []
                original = orch._complete_deferred_derive

                async def _track_derive(uri, *args, **kwargs):
                    derive_uris.append(uri)
                    return await original(uri, *args, **kwargs)

                orch._complete_deferred_derive = _track_derive

                role_token = set_request_role("admin")
                try:
                    resp = await client.post(
                        _ADMIN_INGEST_URL,
                        json=_payload("bench_derive_parity_01"),
                    )
                    self.assertEqual(resp.status_code, 200)
                    records = resp.json().get("records", [])
                    response_uris = {r["uri"] for r in records}
                    # Every merged-leaf URI in the response must have had
                    # _complete_deferred_derive invoked on it (R2-01 / R3-P-12).
                    self.assertTrue(
                        response_uris.issubset(set(derive_uris)),
                        f"derive missing for {response_uris - set(derive_uris)}",
                    )
                finally:
                    orch._complete_deferred_derive = original
                    reset_request_role(role_token)

        self._run(check())


if __name__ == "__main__":
    unittest.main()
