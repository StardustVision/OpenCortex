import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from benchmarks.adapters.beam import BeamBench
from benchmarks.unified_eval import _benchmark_flavor, _get_adapter


_BEAM_FIXTURE = [
    {
        "id": "beam-100k-1",
        "bucket": "100k",
        "tier": "100k",
        "question": "Where did Alice hide the launch notes?",
        "answer": "in the blue notebook",
        "haystack_sessions": [
            [
                {"role": "user", "content": "Alice hid the launch notes."},
                {"role": "assistant", "content": "They are in the blue notebook."},
            ]
        ],
    },
    {
        "id": "beam-1m-1",
        "bucket": "1m",
        "tier": "1m",
        "question": "Which city hosts the archive?",
        "answer": "Hangzhou",
        "messages": [
            {"speaker": "user", "text": "The archive is hosted in Hangzhou."},
        ],
    },
]


class _OCStub:
    def __init__(self):
        self.benchmark_ingest_calls = []
        self.recall_calls = []
        self.search_payload_calls = []
        self.benchmark_ingest_result = {
            "records": [{"uri": "opencortex://m/beam-evidence-0"}]
        }
        self.recall_result = {"memory": [{"uri": "opencortex://m/beam-evidence-0"}]}
        self.search_payload_result = {
            "results": [{"uri": "opencortex://m/beam-evidence-0"}]
        }

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


class TestBeamBench(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dataset_path = os.path.join(self.temp_dir.name, "beam.json")
        with open(self.dataset_path, "w", encoding="utf-8") as file_obj:
            json.dump({"items": _BEAM_FIXTURE}, file_obj)

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_load_filter_and_direct_evidence_ingest(self):
        bench = BeamBench()
        bench.load_dataset(self.dataset_path, beam_tier="100k")
        oc = _OCStub()

        result = await bench.ingest(oc, beam_tier="100k")
        items = bench.build_qa_items(beam_tier="100k")

        self.assertEqual(result.total_items, 1)
        self.assertEqual(result.ingested_items, 1)
        self.assertEqual(result.meta["benchmark_flavor"], "pressure")
        self.assertEqual(result.meta["ingest_shape"], "direct_evidence")
        self.assertEqual(result.meta["beam_tier"], "100k")
        self.assertEqual(len(oc.benchmark_ingest_calls), 1)
        call = oc.benchmark_ingest_calls[0]
        self.assertEqual(call["session_id"], "beam-item-0")
        self.assertEqual(call["ingest_shape"], "direct_evidence")
        self.assertFalse(call["include_session_summary"])
        message_meta = call["segments"][0][0]["meta"]
        self.assertEqual(message_meta["beam_bucket"], "100k")
        self.assertEqual(message_meta["beam_tier"], "100k")
        self.assertEqual(items[0].question, "Where did Alice hide the launch notes?")
        self.assertEqual(items[0].answer, "in the blue notebook")
        self.assertEqual(items[0].expected_uris, ["opencortex://m/beam-evidence-0"])
        self.assertEqual(items[0].meta["beam_bucket"], "100k")

    def test_build_qa_items_without_ingest_uses_tier_filter(self):
        bench = BeamBench()
        bench.load_dataset(self.dataset_path)

        items = bench.build_qa_items(beam_tier="1m")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].meta["item_index"], 1)
        self.assertEqual(items[0].category, "1m")
        self.assertIn("archive is hosted in Hangzhou", bench.get_baseline_context(items[0]))

    async def test_retrieve_defaults_to_scoped_recall(self):
        bench = BeamBench()
        bench.load_dataset(self.dataset_path, beam_tier="100k")
        oc = _OCStub()
        qa_item = bench.build_qa_items(beam_tier="100k")[0]

        results, _latency_ms = await bench.retrieve(oc, qa_item, top_k=3)
        retrieval_meta = bench.pop_last_retrieval_meta()

        self.assertEqual(results[0]["uri"], "opencortex://m/beam-evidence-0")
        self.assertEqual(oc.recall_calls[0]["session_id"], "beam-item-0")
        self.assertTrue(oc.recall_calls[0]["session_scope"])
        self.assertEqual(
            retrieval_meta["retrieval_contract"],
            {
                "method": "recall",
                "endpoint": "context_recall",
                "session_scope": True,
            },
        )

    async def test_search_retrieval_is_item_scoped(self):
        bench = BeamBench()
        bench.load_dataset(self.dataset_path, beam_tier="100k")
        bench._retrieve_method = "search"
        oc = _OCStub()
        qa_item = bench.build_qa_items(beam_tier="100k")[0]

        results, _latency_ms = await bench.retrieve(oc, qa_item, top_k=5)

        self.assertEqual(results[0]["uri"], "opencortex://m/beam-evidence-0")
        self.assertEqual(
            oc.search_payload_calls[0]["metadata_filter"],
            {"op": "must", "field": "session_id", "conds": ["beam-item-0"]},
        )


class TestBeamBenchRunnerOptions(unittest.TestCase):
    def test_beam_routes_to_dedicated_adapter(self):
        self.assertIsInstance(_get_adapter("conversation", "beam"), BeamBench)

    def test_beam_auto_flavor_is_pressure(self):
        class Args:
            dataset = "beam"
            benchmark_flavor = "auto"

        self.assertEqual(_benchmark_flavor(Args()), "pressure")


if __name__ == "__main__":
    unittest.main()
