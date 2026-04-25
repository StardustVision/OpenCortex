"""DTO validation tests for BenchmarkConversationIngestResponse.

Locks the U3 contract: the new Pydantic models in
``src/opencortex/http/models.py`` round-trip the existing benchmark
ingest response shape without modification, so wiring the DTO through
the admin route in U5 produces byte-identical JSON.
"""

from __future__ import annotations

import unittest

from opencortex.http.models import (
    BenchmarkConversationIngestRecord,
    BenchmarkConversationIngestResponse,
)


class TestBenchmarkConversationIngestResponse(unittest.TestCase):
    """Round-trip the response shape the service has been returning."""

    def test_merged_recompose_response_validates(self):
        """Representative merged_recompose response dict validates cleanly."""
        payload = {
            "status": "ok",
            "session_id": "bench_conv_01",
            "source_uri": "opencortex://t/u/session/conversations/bench_conv_01/source",
            "summary_uri": None,
            "records": [
                {
                    "uri": "opencortex://t/u/memories/events/bench_conv_01-000000-000001",
                    "abstract": "[Alice] moved to Hangzhou",
                    "overview": "User Alice notes relocation; Bob notes diet change.",
                    "content": "[Alice]: I moved to Hangzhou.\n\n[Bob]: You also stopped eating spicy food.",
                    "meta": {
                        "layer": "merged",
                        "msg_range": [0, 1],
                        "session_id": "bench_conv_01",
                        "source_uri": "opencortex://t/u/session/conversations/bench_conv_01/source",
                        "recomposition_stage": "benchmark_offline",
                    },
                    "abstract_json": {"slots": {"entities": ["Alice", "Bob"]}},
                    "session_id": "bench_conv_01",
                    "speaker": "",
                    "event_date": "2023-05-01T09:00:00Z",
                    "msg_range": [0, 1],
                    "recomposition_stage": "benchmark_offline",
                    "source_uri": "opencortex://t/u/session/conversations/bench_conv_01/source",
                }
            ],
        }
        model = BenchmarkConversationIngestResponse.model_validate(payload)
        self.assertEqual(model.status, "ok")
        self.assertEqual(model.session_id, "bench_conv_01")
        self.assertEqual(len(model.records), 1)
        self.assertEqual(model.records[0].msg_range, [0, 1])
        self.assertIsNone(model.ingest_shape)

    def test_direct_evidence_response_validates(self):
        """direct_evidence path adds ingest_shape; otherwise identical shape."""
        payload = {
            "status": "ok",
            "session_id": "bench_lme_01",
            "source_uri": "opencortex://t/u/session/conversations/bench_lme_01/source",
            "summary_uri": None,
            "ingest_shape": "direct_evidence",
            "records": [
                {
                    "uri": "opencortex://t/u/memory/events/bench_lme_01/benchmark_evidence_0_0_1",
                    "content": "user: I moved to Hangzhou.\nassistant: Noted.",
                    "meta": {"lme_session_id": "s1"},
                    "session_id": "bench_lme_01",
                    "msg_range": [0, 1],
                    "recomposition_stage": "benchmark_direct_evidence",
                }
            ],
        }
        model = BenchmarkConversationIngestResponse.model_validate(payload)
        self.assertEqual(model.ingest_shape, "direct_evidence")
        self.assertEqual(len(model.records), 1)

    def test_empty_records_list_validates(self):
        """Empty segments → empty records list → still validates."""
        payload = {
            "status": "ok",
            "session_id": "bench_empty",
            "source_uri": None,
            "summary_uri": None,
            "records": [],
        }
        model = BenchmarkConversationIngestResponse.model_validate(payload)
        self.assertEqual(model.records, [])
        self.assertIsNone(model.source_uri)

    def test_minimum_required_record_fields(self):
        """Record only needs ``uri``; everything else has a sensible default."""
        record = BenchmarkConversationIngestRecord.model_validate(
            {"uri": "opencortex://x/y/z"}
        )
        self.assertEqual(record.uri, "opencortex://x/y/z")
        self.assertEqual(record.abstract, "")
        self.assertEqual(record.content, "")
        self.assertEqual(record.meta, {})
        self.assertIsNone(record.msg_range)

    def test_response_round_trip_preserves_fields(self):
        """``model.model_dump()`` round-trips back to the same field set."""
        payload = {
            "status": "ok",
            "session_id": "bench_rt",
            "source_uri": "opencortex://t/u/session/conversations/bench_rt/source",
            "summary_uri": "opencortex://t/u/session/conversations/bench_rt/summary",
            "records": [
                {
                    "uri": "opencortex://t/u/memories/events/bench_rt-000000-000001",
                    "msg_range": [0, 1],
                    "recomposition_stage": "benchmark_offline",
                }
            ],
        }
        model = BenchmarkConversationIngestResponse.model_validate(payload)
        dumped = model.model_dump()
        self.assertEqual(dumped["session_id"], "bench_rt")
        self.assertEqual(dumped["summary_uri"], payload["summary_uri"])
        self.assertEqual(dumped["records"][0]["msg_range"], [0, 1])


if __name__ == "__main__":
    unittest.main()
