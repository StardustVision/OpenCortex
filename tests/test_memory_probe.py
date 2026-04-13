import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.intent import MemoryBootstrapProbe


class _StorageStub:
    def __init__(self, results):
        self._results = results

    async def search(self, **kwargs):
        return list(self._results)


class _EmbedderStub:
    model_name = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    is_available = True

    def embed_query(self, text):
        class _Result:
            dense_vector = [0.1, 0.2]

        return _Result()


class TestMemoryProbe(unittest.IsolatedAsyncioTestCase):
    async def test_probe_returns_l0_hits_and_evidence(self):
        probe = MemoryBootstrapProbe(
            storage=_StorageStub(
                [
                    {
                        "uri": "opencortex://memory/events/1",
                        "category": "event",
                        "context_type": "memory",
                        "abstract": "Reviewed launch checklist.",
                        "_score": 0.82,
                        "metadata": {"topics": ["launch", "checklist"]},
                    },
                    {
                        "uri": "opencortex://memory/events/2",
                        "category": "event",
                        "context_type": "memory",
                        "abstract": "Discussed launch rollback.",
                        "_score": 0.71,
                    },
                ]
            ),
            embedder=_EmbedderStub(),
            collection_resolver=lambda: "context",
            filter_builder=lambda: {"op": "and", "conds": []},
        )

        result = await probe.probe("What happened before launch?")

        self.assertTrue(result.should_recall)
        self.assertEqual(len(result.candidate_entries), 2)
        self.assertEqual(result.evidence.candidate_count, 2)
        self.assertEqual(result.evidence.top_score, 0.82)
        self.assertEqual(result.evidence.score_gap, 0.11)
        self.assertIn("launch", result.anchor_hits)

    async def test_empty_query_bypasses_probe(self):
        probe = MemoryBootstrapProbe(
            storage=_StorageStub([]),
            embedder=_EmbedderStub(),
            collection_resolver=lambda: "context",
            filter_builder=lambda: {"op": "and", "conds": []},
        )

        result = await probe.probe("   ")

        self.assertFalse(result.should_recall)
        self.assertEqual(result.trace.degrade_reason, "empty_query")


if __name__ == "__main__":
    unittest.main()
