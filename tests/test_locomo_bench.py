import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from benchmarks.adapters.conversation import LongMemEvalBench
from benchmarks.adapters.locomo import LoCoMoBench
from benchmarks.unified_eval import _get_adapter


_LOCOMO_FIXTURE = [
    {
        "sample_id": "conv-1",
        "conversation": {
            "speaker_a": "Alice",
            "speaker_b": "Bob",
            "session_1_date_time": "9:00 am on 1 May, 2023",
            "session_1": [
                {"speaker": "Alice", "text": "I moved to Hangzhou."},
                {"speaker": "Bob", "text": "You also stopped eating spicy food."},
            ],
            "session_2_date_time": "10:00 am on 3 May, 2023",
            "session_2": [
                {"speaker": "Alice", "text": "I will visit West Lake next week."},
            ],
        },
        "qa": [
            {
                "question": "Where did I move?",
                "answer": "Hangzhou",
                "category": "1",
                "evidence": ["D1:1", "D1:2"],
            },
            {
                "question": "What will I visit next week?",
                "answer": "West Lake",
                "category": "2",
                "evidence": [["D2:1"]],
            },
        ],
    }
]


class _OCStub:
    def __init__(self):
        self.commit_calls = []
        self.end_calls = []
        self.recall_calls = []
        self.memory_list_calls = []
        self.recall_result = {"memory": []}
        self.list_results = []

    async def context_commit(self, session_id, turn_id, messages):
        self.commit_calls.append(
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "messages": list(messages),
            }
        )
        return {"accepted": True}

    async def context_end(self, session_id):
        self.end_calls.append(session_id)
        return {"status": "closed"}

    async def memory_list(self, **kwargs):
        self.memory_list_calls.append(dict(kwargs))
        if self.list_results:
            return self.list_results.pop(0)
        return {"results": [], "total": 0}

    async def context_recall(self, **kwargs):
        self.recall_calls.append(dict(kwargs))
        return self.recall_result


class TestLoCoMoBench(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dataset_path = os.path.join(self.temp_dir.name, "locomo.json")
        with open(self.dataset_path, "w", encoding="utf-8") as file_obj:
            json.dump(_LOCOMO_FIXTURE, file_obj)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_build_qa_items_falls_back_to_conversation_uri_before_ingest(self):
        bench = LoCoMoBench()
        bench.load_dataset(self.dataset_path)

        items = bench.build_qa_items()

        self.assertEqual(len(items), 2)
        self.assertEqual(
            items[0].expected_uris,
            ["locomo-conversation://locomo-conv-1"],
        )
        self.assertEqual(
            items[1].expected_uris,
            ["locomo-conversation://locomo-conv-1"],
        )
        self.assertEqual(items[0].meta["evidence_sessions"], [1])
        self.assertEqual(items[1].meta["evidence_sessions"], [2])

    async def test_ingest_uses_one_context_session_per_conversation(self):
        bench = LoCoMoBench()
        bench.load_dataset(self.dataset_path)
        oc = _OCStub()
        oc.list_results = [
            {"results": [], "total": 0},
            {
                "results": [
                    {
                        "uri": "opencortex://m/session1",
                        "session_id": "locomo-conv-1",
                        "meta": {"msg_range": [0, 1]},
                        "abstract_json": {"slots": {"time_refs": ["1 May, 2023"]}},
                        "event_date": "2023-05-01T09:00:00Z",
                    },
                    {
                        "uri": "opencortex://m/session2",
                        "session_id": "locomo-conv-1",
                        "meta": {"msg_range": [2, 2]},
                        "abstract_json": {"slots": {"time_refs": ["3 May, 2023"]}},
                        "event_date": "2023-05-03T10:00:00Z",
                    },
                ],
                "total": 2,
            },
        ]

        result = await bench.ingest(oc)

        self.assertEqual(result.total_items, 1)
        self.assertEqual(result.ingested_items, 1)
        self.assertEqual(
            [call["session_id"] for call in oc.commit_calls],
            ["locomo-conv-1", "locomo-conv-1"],
        )
        self.assertEqual(
            [call["turn_id"] for call in oc.commit_calls],
            ["turn-1", "turn-2"],
        )
        self.assertEqual(oc.end_calls, ["locomo-conv-1"])
        self.assertTrue(all(call["include_payload"] for call in oc.memory_list_calls))
        self.assertTrue(
            oc.commit_calls[0]["messages"][0]["content"].startswith("[Alice]:")
        )
        first_meta = oc.commit_calls[0]["messages"][0]["meta"]
        self.assertEqual(first_meta["event_date"], "2023-05-01T09:00:00Z")
        self.assertIn("9:00 am on 1 May, 2023", first_meta["time_refs"])
        items = bench.build_qa_items()
        self.assertEqual(
            items[0].expected_uris,
            ["opencortex://m/session1"],
        )
        self.assertEqual(
            items[1].expected_uris,
            ["opencortex://m/session2"],
        )

    async def test_ingest_prefers_tightest_overlapping_merged_record(self):
        bench = LoCoMoBench()
        bench.load_dataset(self.dataset_path)
        oc = _OCStub()
        oc.list_results = [
            {"results": [], "total": 0},
            {
                "results": [
                    {
                        "uri": "opencortex://m/cumulative",
                        "session_id": "locomo-conv-1",
                        "meta": {"msg_range": [0, 2]},
                        "abstract_json": {"slots": {"time_refs": ["1 May, 2023"]}},
                        "event_date": "2023-05-01T09:00:00Z",
                    },
                    {
                        "uri": "opencortex://m/session1-tight",
                        "session_id": "locomo-conv-1",
                        "meta": {"msg_range": [0, 1]},
                        "abstract_json": {"slots": {"time_refs": ["1 May, 2023"]}},
                        "event_date": "2023-05-01T09:00:00Z",
                    },
                    {
                        "uri": "opencortex://m/session2-tight",
                        "session_id": "locomo-conv-1",
                        "meta": {"msg_range": [2, 2]},
                        "abstract_json": {"slots": {"time_refs": ["3 May, 2023"]}},
                        "event_date": "2023-05-03T10:00:00Z",
                    },
                ],
                "total": 3,
            },
        ]

        await bench.ingest(oc)

        items = bench.build_qa_items()
        self.assertEqual(items[0].expected_uris, ["opencortex://m/session1-tight"])
        self.assertEqual(items[1].expected_uris, ["opencortex://m/session2-tight"])

    async def test_ingest_falls_back_to_time_refs_when_msg_range_is_missing(self):
        bench = LoCoMoBench()
        bench.load_dataset(self.dataset_path)
        oc = _OCStub()
        oc.list_results = [
            {"results": [], "total": 0},
            {
                "results": [
                    {
                        "uri": "opencortex://m/session1",
                        "session_id": "locomo-conv-1",
                        "meta": {"time_refs": ["9:00 am on 1 May, 2023"]},
                        "abstract_json": {"slots": {"time_refs": ["2023-05-01"]}},
                        "event_date": "2023-05-01T09:00:00Z",
                    },
                    {
                        "uri": "opencortex://m/session2",
                        "session_id": "locomo-conv-1",
                        "meta": {"time_refs": ["10:00 am on 3 May, 2023"]},
                        "abstract_json": {"slots": {"time_refs": ["2023-05-03"]}},
                        "event_date": "2023-05-03T10:00:00Z",
                    },
                ],
                "total": 2,
            },
        ]

        result = await bench.ingest(oc)

        self.assertEqual(result.ingested_items, 1)
        items = bench.build_qa_items()
        self.assertEqual(items[0].expected_uris, ["opencortex://m/session1"])
        self.assertEqual(items[1].expected_uris, ["opencortex://m/session2"])

    async def test_retrieve_keeps_runtime_uris_for_metric_matching(self):
        bench = LoCoMoBench()
        bench.load_dataset(self.dataset_path)
        oc = _OCStub()
        oc.recall_result = {
            "intent": {
                "memory_pipeline": {
                    "probe": {"should_recall": True},
                    "planner": {"retrieval_depth": "l1"},
                    "runtime": {
                        "trace": {"effective": {"raw_candidate_cap": 12}},
                        "degrade": {"applied": False, "actions": []},
                    },
                }
            },
            "memory": [
                {
                    "uri": "opencortex://m/1",
                    "session_id": "locomo-conv-1-s1",
                    "abstract": "Alice moved to Hangzhou.",
                },
                {
                    "uri": "opencortex://m/2",
                    "session_id": "locomo-conv-1-s1",
                    "abstract": "Alice stopped eating spicy food.",
                },
                {
                    "uri": "opencortex://m/3",
                    "session_id": "locomo-conv-1-s2",
                    "abstract": "Alice will visit West Lake.",
                },
            ]
        }

        qa_item = bench.build_qa_items()[0]
        results, _latency_ms = await bench.retrieve(oc, qa_item, top_k=5)
        retrieval_meta = bench.pop_last_retrieval_meta()

        self.assertEqual(
            [item["uri"] for item in results],
            [
                "opencortex://m/1",
                "opencortex://m/2",
                "opencortex://m/3",
            ],
        )
        self.assertEqual(
            retrieval_meta["memory_pipeline"]["planner"]["retrieval_depth"],
            "l1",
        )
        self.assertNotIn("detail_level", oc.recall_calls[0])


class TestBenchmarkAdapterRouting(unittest.TestCase):
    def test_conversation_defaults_to_locomo_bench(self):
        self.assertIsInstance(_get_adapter("conversation"), LoCoMoBench)

    def test_longmemeval_routes_to_dedicated_bench(self):
        self.assertIsInstance(
            _get_adapter("conversation", "longmemeval"),
            LongMemEvalBench,
        )


if __name__ == "__main__":
    unittest.main()
