import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from benchmarks.adapters.conversation import LongMemEvalBench
from benchmarks.adapters.locomo import LoCoMoBench
from benchmarks.unified_eval import (
    _benchmark_flavor,
    _get_adapter,
    _resolve_benchmark_options,
    _retrieval_cutoffs,
)


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
        self.benchmark_ingest_calls = []
        self.recall_result = {"memory": []}
        self.list_results = []
        self.benchmark_ingest_result = {"records": []}
        self.search_payload_calls = []
        self.search_payload_result = {"results": []}

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

    async def benchmark_conversation_ingest(
        self,
        session_id,
        segments,
        include_session_summary=True,
        ingest_shape="merged_recompose",
    ):
        self.benchmark_ingest_calls.append(
            {
                "session_id": session_id,
                "segments": list(segments),
                "include_session_summary": include_session_summary,
                "ingest_shape": ingest_shape,
            }
        )
        return dict(self.benchmark_ingest_result)

    async def context_recall(self, **kwargs):
        self.recall_calls.append(dict(kwargs))
        return self.recall_result

    async def search_payload(self, **kwargs):
        self.search_payload_calls.append(dict(kwargs))
        return self.search_payload_result


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
                        "msg_range": [0, 1],
                        "abstract_json": {"slots": {"time_refs": ["1 May, 2023"]}},
                        "event_date": "2023-05-01T09:00:00Z",
                    },
                    {
                        "uri": "opencortex://m/session2",
                        "session_id": "locomo-conv-1",
                        "msg_range": [2, 2],
                        "abstract_json": {"slots": {"time_refs": ["3 May, 2023"]}},
                        "event_date": "2023-05-03T10:00:00Z",
                    },
                ],
                "total": 2,
            },
        ]

        result = await bench.ingest(oc, max_qa=1)

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
                        "msg_range": [0, 2],
                        "abstract_json": {"slots": {"time_refs": ["1 May, 2023"]}},
                        "event_date": "2023-05-01T09:00:00Z",
                    },
                    {
                        "uri": "opencortex://m/session1-tight",
                        "session_id": "locomo-conv-1",
                        "msg_range": [0, 1],
                        "abstract_json": {"slots": {"time_refs": ["1 May, 2023"]}},
                        "event_date": "2023-05-01T09:00:00Z",
                    },
                    {
                        "uri": "opencortex://m/session2-tight",
                        "session_id": "locomo-conv-1",
                        "msg_range": [2, 2],
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

    async def test_ingest_store_uses_offline_benchmark_endpoint(self):
        bench = LoCoMoBench()
        bench.load_dataset(self.dataset_path)
        bench._ingest_method = "store"
        oc = _OCStub()
        oc.benchmark_ingest_result = {
            "records": [
                {
                    "uri": "opencortex://m/session1",
                    "session_id": "locomo-conv-1",
                    "msg_range": [0, 1],
                    "abstract_json": {"slots": {"time_refs": ["1 May, 2023"]}},
                    "event_date": "2023-05-01T09:00:00Z",
                },
                {
                    "uri": "opencortex://m/session2",
                    "session_id": "locomo-conv-1",
                    "msg_range": [2, 2],
                    "abstract_json": {"slots": {"time_refs": ["3 May, 2023"]}},
                    "event_date": "2023-05-03T10:00:00Z",
                },
            ]
        }

        result = await bench.ingest(oc, max_qa=1)

        self.assertEqual(result.ingested_items, 1)
        self.assertEqual(len(oc.benchmark_ingest_calls), 1)
        self.assertEqual(oc.benchmark_ingest_calls[0]["session_id"], "locomo-conv-1")
        self.assertEqual(len(oc.benchmark_ingest_calls[0]["segments"]), 2)
        # U11: adapter must opt out of session_summary on the store path
        # so benchmark scoring is not paying for an unused LLM call per
        # conversation.
        self.assertFalse(oc.benchmark_ingest_calls[0]["include_session_summary"])
        self.assertFalse(oc.commit_calls)
        self.assertFalse(oc.end_calls)
        self.assertFalse(oc.memory_list_calls)
        items = bench.build_qa_items()
        self.assertEqual(items[0].expected_uris, ["opencortex://m/session1"])
        self.assertEqual(items[1].expected_uris, ["opencortex://m/session2"])

    async def test_build_qa_items_prefers_question_matching_leaf_within_session(self):
        fixture = [
            {
                "sample_id": "conv-match",
                "conversation": {
                    "speaker_a": "Alice",
                    "speaker_b": "Bob",
                    "session_1_date_time": "9:00 am on 1 May, 2023",
                    "session_1": [
                        {
                            "speaker": "Alice",
                            "text": "The LGBTQ support group helped me.",
                        },
                        {"speaker": "Bob", "text": "Glad it helped."},
                    ],
                },
                "qa": [
                    {
                        "question": "When did Alice go to the LGBTQ support group?",
                        "answer": "1 May 2023",
                        "category": "2",
                        "evidence": ["D1:1"],
                    }
                ],
            }
        ]
        dataset_path = os.path.join(self.temp_dir.name, "locomo-match.json")
        with open(dataset_path, "w", encoding="utf-8") as file_obj:
            json.dump(fixture, file_obj)
        bench = LoCoMoBench()
        bench.load_dataset(dataset_path)
        oc = _OCStub()
        oc.list_results = [
            {"results": [], "total": 0},
            {
                "results": [
                    {
                        "uri": "opencortex://m/session1-generic",
                        "session_id": "locomo-conv-match",
                        "msg_range": [0, 1],
                        "abstract": "Alice and Bob agree to relax before the weekend.",
                        "overview": "They talk about work, family, and doing more research later.",
                        "abstract_json": {
                            "anchors": [
                                {"anchor_type": "entity", "value": "Alice"},
                                {"anchor_type": "time", "value": "1 May, 2023"},
                            ]
                        },
                        "event_date": "2023-05-01T09:00:00Z",
                    },
                    {
                        "uri": "opencortex://m/session1-specific",
                        "session_id": "locomo-conv-match",
                        "msg_range": [0, 1],
                        "abstract": "Alice says the LGBTQ support group made her feel accepted.",
                        "overview": "She describes the support group and what she learned there.",
                        "abstract_json": {
                            "anchors": [
                                {"anchor_type": "entity", "value": "Alice"},
                                {
                                    "anchor_type": "entity",
                                    "value": "LGBTQ support group",
                                },
                                {"anchor_type": "time", "value": "1 May, 2023"},
                            ]
                        },
                        "event_date": "2023-05-01T09:00:00Z",
                    },
                ],
                "total": 2,
            },
        ]

        await bench.ingest(oc)
        items = bench.build_qa_items()

        self.assertEqual(items[0].expected_uris, ["opencortex://m/session1-specific"])

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
            ],
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
        self.assertTrue(oc.recall_calls[0]["session_scope"])
        self.assertEqual(oc.recall_calls[0]["session_id"], "locomo-conv-1")
        self.assertEqual(
            retrieval_meta["retrieval_contract"],
            {
                "method": "recall",
                "endpoint": "context_recall",
                "session_scope": True,
            },
        )

    async def test_retrieve_search_uses_scoped_memory_search(self):
        bench = LoCoMoBench()
        bench.load_dataset(self.dataset_path)
        bench._retrieve_method = "search"
        oc = _OCStub()
        oc.search_payload_result = {
            "results": [
                {"uri": "opencortex://m/1", "content": "Alice moved to Hangzhou."}
            ]
        }

        qa_item = bench.build_qa_items()[0]
        results, _latency_ms = await bench.retrieve(oc, qa_item, top_k=200)
        retrieval_meta = bench.pop_last_retrieval_meta()

        self.assertEqual([item["uri"] for item in results], ["opencortex://m/1"])
        self.assertFalse(oc.recall_calls)
        self.assertEqual(oc.search_payload_calls[0]["limit"], 200)
        self.assertEqual(oc.search_payload_calls[0]["context_type"], "memory")
        self.assertEqual(oc.search_payload_calls[0]["detail_level"], "l2")
        self.assertEqual(
            oc.search_payload_calls[0]["metadata_filter"],
            {"op": "must", "field": "session_id", "conds": ["locomo-conv-1"]},
        )
        self.assertEqual(
            retrieval_meta["retrieval_contract"],
            {
                "method": "search",
                "endpoint": "memory_search",
                "session_scope": True,
            },
        )


_LONGMEMEVAL_FIXTURE = [
    {
        "question": "Where did the user move in the first session?",
        "answer": "Hangzhou",
        "question_type": "single-session-user",
        "haystack_session_ids": ["s1", "s2"],
        "answer_session_ids": ["s1"],
        "haystack_dates": ["2023-05-01", "2023-05-03"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I moved to Hangzhou."},
                {"role": "assistant", "content": "Noted."},
            ],
            [
                {"role": "user", "content": "I will visit West Lake next week."},
            ],
        ],
    },
    {
        "question": "What did the assistant note?",
        "answer": "The move",
        "question_type": "single-session-assistant",
        "haystack_session_ids": ["s3"],
        "answer_session_ids": ["s3"],
        "haystack_dates": ["2023-05-04"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I like tea."},
                {"role": "assistant", "content": "I noted the move."},
            ],
        ],
    },
    {
        "question": "Which city appears again?",
        "answer": "Hangzhou",
        "question_type": "single-session-user",
        "haystack_session_ids": ["s4"],
        "answer_session_ids": ["s4"],
        "haystack_dates": ["2023-05-05"],
        "haystack_sessions": [[{"role": "user", "content": "Hangzhou again."}]],
    },
]


class TestLongMemEvalBench(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dataset_path = os.path.join(self.temp_dir.name, "longmemeval.json")
        with open(self.dataset_path, "w", encoding="utf-8") as file_obj:
            json.dump(_LONGMEMEVAL_FIXTURE, file_obj)

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_ingest_maps_inner_sessions_to_merged_uris(self):
        bench = LongMemEvalBench()
        bench.load_dataset(self.dataset_path)
        oc = _OCStub()
        oc.list_results = [
            {"results": [], "total": 0},
            {
                "results": [
                    {
                        "uri": "opencortex://m/lme-session1",
                        "session_id": "lme-item-0",
                        "msg_range": [0, 1],
                        "abstract_json": {"slots": {"time_refs": ["2023-05-01"]}},
                        "event_date": "2023-05-01T00:00:00Z",
                    },
                    {
                        "uri": "opencortex://m/lme-session2",
                        "session_id": "lme-item-0",
                        "msg_range": [2, 2],
                        "abstract_json": {"slots": {"time_refs": ["2023-05-03"]}},
                        "event_date": "2023-05-03T00:00:00Z",
                    },
                ],
                "total": 2,
            },
        ]

        result = await bench.ingest(oc, max_qa=1, ingest_method="context_lifecycle")

        self.assertEqual(result.total_items, 1)
        self.assertEqual(result.ingested_items, 1)
        self.assertEqual(
            [call["session_id"] for call in oc.commit_calls],
            ["lme-item-0", "lme-item-0"],
        )
        self.assertEqual(oc.end_calls, ["lme-item-0"])
        items = bench.build_qa_items()
        self.assertEqual(
            items[0].expected_uris,
            ["opencortex://m/lme-session1"],
        )
        self.assertEqual(items[0].meta["item_index"], 0)

    async def test_ingest_store_uses_offline_benchmark_endpoint(self):
        bench = LongMemEvalBench()
        bench.load_dataset(self.dataset_path)
        bench._ingest_method = "store"
        oc = _OCStub()
        oc.benchmark_ingest_result = {
            "records": [
                {
                    "uri": "opencortex://m/lme-session1",
                    "session_id": "lme-item-0",
                    "msg_range": [0, 1],
                    "abstract_json": {"slots": {"time_refs": ["2023-05-01"]}},
                    "event_date": "2023-05-01T00:00:00Z",
                },
                {
                    "uri": "opencortex://m/lme-session2",
                    "session_id": "lme-item-0",
                    "msg_range": [2, 2],
                    "abstract_json": {"slots": {"time_refs": ["2023-05-03"]}},
                    "event_date": "2023-05-03T00:00:00Z",
                },
            ]
        }

        result = await bench.ingest(oc, max_qa=1)

        self.assertEqual(result.ingested_items, 1)
        self.assertEqual(len(oc.benchmark_ingest_calls), 1)
        self.assertEqual(oc.benchmark_ingest_calls[0]["session_id"], "lme-item-0")
        self.assertEqual(len(oc.benchmark_ingest_calls[0]["segments"]), 2)
        # U11: LongMemEval store path also opts out of session_summary.
        self.assertFalse(oc.benchmark_ingest_calls[0]["include_session_summary"])
        self.assertFalse(oc.commit_calls)
        self.assertFalse(oc.end_calls)
        self.assertFalse(oc.memory_list_calls)
        items = bench.build_qa_items()
        self.assertEqual(
            items[0].expected_uris,
            ["opencortex://m/lme-session1"],
        )

    async def test_ingest_mainstream_writes_pair_evidence_without_summary(self):
        bench = LongMemEvalBench()
        bench.load_dataset(self.dataset_path)
        oc = _OCStub()
        oc.benchmark_ingest_result = {
            "records": [
                {
                    "uri": "opencortex://m/lme-evidence-s1",
                    "session_id": "lme-item-0",
                    "msg_range": [0, 1],
                    "meta": {"lme_session_id": "s1"},
                },
                {
                    "uri": "opencortex://m/lme-evidence-s2",
                    "session_id": "lme-item-0",
                    "msg_range": [2, 2],
                    "meta": {"lme_session_id": "s2"},
                },
            ]
        }

        result = await bench.ingest(
            oc,
            max_qa=1,
            ingest_method="longmemeval-mainstream",
        )

        self.assertEqual(result.ingested_items, 1)
        self.assertEqual(result.meta["benchmark_flavor"], "mainstream")
        self.assertEqual(len(oc.benchmark_ingest_calls), 1)
        call = oc.benchmark_ingest_calls[0]
        self.assertEqual(call["session_id"], "lme-item-0")
        self.assertEqual(call["ingest_shape"], "direct_evidence")
        self.assertFalse(call["include_session_summary"])
        self.assertEqual(len(call["segments"]), 2)
        self.assertEqual(len(call["segments"][0]), 2)
        first_meta = call["segments"][0][0]["meta"]
        self.assertEqual(first_meta["event_date"], "2023-05-01")
        self.assertEqual(first_meta["lme_session_id"], "s1")
        self.assertFalse(oc.commit_calls)
        self.assertFalse(oc.end_calls)
        items = bench.build_qa_items()
        self.assertEqual(items[0].expected_uris, ["opencortex://m/lme-evidence-s1"])

    async def test_recall_eval_uses_direct_evidence_ingest_and_context_recall(self):
        bench = LongMemEvalBench()
        bench.load_dataset(self.dataset_path)
        bench._retrieve_method = "recall"
        oc = _OCStub()
        oc.benchmark_ingest_result = {
            "records": [
                {
                    "uri": "opencortex://m/lme-evidence-s1",
                    "session_id": "lme-item-0",
                    "msg_range": [0, 1],
                    "meta": {"lme_session_id": "s1"},
                }
            ]
        }
        oc.recall_result = {
            "memory": [
                {
                    "uri": "opencortex://m/lme-evidence-s1",
                    "abstract": "I moved to Hangzhou.",
                }
            ]
        }

        ingest_result = await bench.ingest(
            oc,
            max_qa=1,
            ingest_method="recall-eval",
        )
        qa_item = bench.build_qa_items(max_qa=1)[0]
        results, _latency_ms = await bench.retrieve(oc, qa_item, top_k=7)
        retrieval_meta = bench.pop_last_retrieval_meta()

        self.assertEqual(ingest_result.meta["benchmark_flavor"], "recall-eval")
        self.assertEqual(ingest_result.meta["ingest_shape"], "direct_evidence")
        self.assertEqual(len(oc.benchmark_ingest_calls), 1)
        self.assertEqual(oc.benchmark_ingest_calls[0]["session_id"], "lme-item-0")
        self.assertEqual(oc.benchmark_ingest_calls[0]["ingest_shape"], "direct_evidence")
        self.assertFalse(oc.benchmark_ingest_calls[0]["include_session_summary"])
        self.assertFalse(oc.commit_calls)
        self.assertEqual([item["uri"] for item in results], ["opencortex://m/lme-evidence-s1"])
        self.assertEqual(oc.recall_calls[0]["session_id"], "lme-item-0")
        self.assertEqual(oc.recall_calls[0]["limit"], 7)
        self.assertTrue(oc.recall_calls[0]["session_scope"])
        self.assertEqual(
            retrieval_meta["retrieval_contract"],
            {
                "method": "recall",
                "endpoint": "context_recall",
                "session_scope": True,
            },
        )

    async def test_per_type_sampling_is_shared_by_ingest_and_qa(self):
        bench = LongMemEvalBench()
        bench.load_dataset(self.dataset_path)
        oc = _OCStub()

        result = await bench.ingest(
            oc,
            per_type=1,
            ingest_method="longmemeval-mainstream",
        )
        items = bench.build_qa_items(per_type=1)

        self.assertEqual(result.total_items, 2)
        self.assertEqual(len(items), 2)
        self.assertEqual(
            [item.meta["question_type"] for item in items],
            ["single-session-user", "single-session-assistant"],
        )

    async def test_retrieve_search_is_scoped_to_longmemeval_item(self):
        bench = LongMemEvalBench()
        bench.load_dataset(self.dataset_path)
        bench._retrieve_method = "search"
        oc = _OCStub()
        oc.search_payload_result = {
            "results": [
                {"uri": "opencortex://m/lme-evidence-s1", "content": "Hangzhou"}
            ]
        }

        qa_item = bench.build_qa_items(max_qa=1)[0]
        results, _latency_ms = await bench.retrieve(oc, qa_item, top_k=200)
        retrieval_meta = bench.pop_last_retrieval_meta()

        self.assertEqual(results[0]["uri"], "opencortex://m/lme-evidence-s1")
        self.assertEqual(oc.search_payload_calls[0]["limit"], 200)
        self.assertEqual(oc.search_payload_calls[0]["context_type"], "memory")
        self.assertEqual(oc.search_payload_calls[0]["detail_level"], "l2")
        self.assertEqual(
            oc.search_payload_calls[0]["metadata_filter"],
            {"op": "must", "field": "session_id", "conds": ["lme-item-0"]},
        )
        self.assertEqual(
            retrieval_meta["retrieval_contract"],
            {
                "method": "search",
                "endpoint": "memory_search",
                "session_scope": True,
            },
        )

    async def test_retrieve_uses_item_scoped_session_recall(self):
        bench = LongMemEvalBench()
        bench.load_dataset(self.dataset_path)
        bench._retrieve_method = "recall"
        oc = _OCStub()
        oc.recall_result = {
            "memory_pipeline": {
                "probe": {"should_recall": True},
                "planner": {"retrieval_depth": "l0"},
                "runtime": {"trace": {}, "degrade": {"applied": False}},
            },
            "memory": [
                {
                    "uri": "opencortex://m/lme-session1",
                    "abstract": "I moved to Hangzhou.",
                }
            ],
        }

        qa_item = bench.build_qa_items()[0]
        qa_item.meta["item_index"] = 0
        results, _latency_ms = await bench.retrieve(oc, qa_item, top_k=3)
        retrieval_meta = bench.pop_last_retrieval_meta()

        self.assertEqual(
            [item["uri"] for item in results], ["opencortex://m/lme-session1"]
        )
        self.assertEqual(oc.recall_calls[0]["session_id"], "lme-item-0")
        self.assertTrue(oc.recall_calls[0]["session_scope"])
        self.assertEqual(
            retrieval_meta["retrieval_contract"],
            {
                "method": "recall",
                "endpoint": "context_recall",
                "session_scope": True,
            },
        )


class TestLongMemEvalRunnerOptions(unittest.TestCase):
    def test_longmemeval_auto_uses_mainstream_high_k_cutoffs(self):
        class Args:
            dataset = "longmemeval"
            benchmark_flavor = "auto"
            retrieval_cutoffs = ""

        flavor = _benchmark_flavor(Args())

        self.assertEqual(flavor, "mainstream-search")
        self.assertEqual(_retrieval_cutoffs(Args(), flavor), [10, 20, 50, 200])

    def test_locomo_auto_uses_mainstream_high_k_cutoffs(self):
        class Args:
            dataset = "locomo"
            benchmark_flavor = "auto"
            retrieval_cutoffs = ""

        flavor = _benchmark_flavor(Args())

        self.assertEqual(flavor, "mainstream-search")
        self.assertEqual(_retrieval_cutoffs(Args(), flavor), [10, 20, 50, 200])

    def test_mainstream_alias_normalizes_to_mainstream_search(self):
        class Args:
            dataset = "longmemeval"
            benchmark_flavor = "mainstream"

        self.assertEqual(_benchmark_flavor(Args()), "mainstream-search")

    def test_custom_retrieval_cutoffs_are_parsed(self):
        class Args:
            dataset = "longmemeval"
            benchmark_flavor = "mainstream"
            retrieval_cutoffs = "5, 20, 20, 200"

        self.assertEqual(_retrieval_cutoffs(Args(), "mainstream-search"), [5, 20, 200])

    def test_recall_eval_forces_recall_and_evidence_metadata(self):
        class Args:
            dataset = "longmemeval"
            benchmark_flavor = "recall-eval"
            ingest_method = "store"
            retrieve_method = "search"
            retrieval_cutoffs = ""
            top_k = 10

        options = _resolve_benchmark_options(Args())

        self.assertEqual(options.benchmark_layer, "production_recall")
        self.assertEqual(options.benchmark_flavor, "recall-eval")
        self.assertEqual(options.ingest_method, "longmemeval-mainstream")
        self.assertEqual(options.ingest_shape, "direct_evidence")
        self.assertEqual(options.retrieve_method, "recall")
        self.assertEqual(options.retrieval_cutoffs, [10, 20, 50, 200])
        self.assertEqual(options.retrieval_metric_top_k, 200)
        self.assertEqual(options.effective_top_k, 200)

    def test_deprecated_mcp_ingest_method_aliases_to_context_lifecycle(self):
        class Args:
            dataset = "locomo"
            benchmark_flavor = "internal"
            ingest_method = "mcp"
            retrieve_method = "search"
            retrieval_cutoffs = ""
            top_k = 10

        options = _resolve_benchmark_options(Args())

        self.assertEqual(options.ingest_method, "context_lifecycle")
        self.assertEqual(options.ingest_shape, "context_lifecycle")

    def test_pressure_defaults_to_recall_unless_search_is_explicit(self):
        class DefaultArgs:
            dataset = "beam"
            benchmark_flavor = "pressure"
            ingest_method = "store"
            retrieve_method = "search"
            retrieval_cutoffs = ""
            top_k = 10

        class ExplicitSearchArgs(DefaultArgs):
            retrieve_method_set = True

        options = _resolve_benchmark_options(DefaultArgs())
        self.assertEqual(options.benchmark_layer, "pressure")
        self.assertEqual(options.benchmark_flavor, "pressure")
        self.assertEqual(options.retrieve_method, "recall")
        self.assertEqual(
            _resolve_benchmark_options(ExplicitSearchArgs()).retrieve_method,
            "search",
        )


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
