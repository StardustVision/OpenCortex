import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.intent import MemoryBootstrapProbe


class _StorageStub:
    def __init__(self, results):
        self._results = results
        self.last_filter = None
        self.calls = []

    async def search(self, **kwargs):
        self.last_filter = kwargs.get("filter")
        self.calls.append(dict(kwargs))
        if callable(self._results):
            return list(self._results(**kwargs))
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
        self.assertEqual(result.evidence.object_top_score, 0.82)
        self.assertEqual(result.evidence.anchor_top_score, 0.82)
        self.assertEqual(result.evidence.object_candidate_count, 2)
        self.assertEqual(result.trace.object_candidates, 2)
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

    async def test_probe_merges_scope_filter_into_storage_search(self):
        storage = _StorageStub([])
        probe = MemoryBootstrapProbe(
            storage=storage,
            embedder=_EmbedderStub(),
            collection_resolver=lambda: "context",
            filter_builder=lambda: {"op": "must", "field": "scope", "conds": ["private"]},
        )

        await probe.probe(
            "launch",
            scope_filter={"op": "must", "field": "session_id", "conds": ["sess-1"]},
        )

        self.assertEqual(
            storage.calls[0]["filter"],
            {
                "op": "and",
                "conds": [
                    {
                        "op": "and",
                        "conds": [
                            {"op": "must", "field": "scope", "conds": ["private"]},
                            {"op": "must", "field": "session_id", "conds": ["sess-1"]},
                        ],
                    },
                    {"op": "must", "field": "is_leaf", "conds": [True]},
                ],
            },
        )
        self.assertTrue(
            any(
                str(call.get("filter", {})).find("anchor_hits") >= 0
                for call in storage.calls[1:]
            )
        )

    async def test_probe_can_return_anchor_only_hits(self):
        def _results(**kwargs):
            filt = kwargs.get("filter") or {}
            conds = filt.get("conds", []) if filt.get("op") == "and" else []
            if any(
                condition.get("field") == "anchor_hits"
                for condition in conds
                if isinstance(condition, dict)
            ):
                return [
                    {
                        "uri": "opencortex://memory/events/anchor-only",
                        "category": "event",
                        "context_type": "memory",
                        "abstract": "下周二去杭州出差，住在西湖边。",
                        "_text_score": 0.67,
                        "anchor_hits": ["杭州", "下周二", "西湖"],
                        "abstract_json": {
                            "memory_kind": "event",
                            "summary": "下周二去杭州出差，住在西湖边。",
                            "anchors": [
                                {"anchor_type": "entity", "value": "杭州", "text": "杭州"},
                                {"anchor_type": "time", "value": "下周二", "text": "下周二"},
                            ],
                            "slots": {
                                "entities": ["杭州"],
                                "time_refs": ["下周二"],
                                "topics": ["西湖"],
                            },
                        },
                    }
                ]
            return []

        probe = MemoryBootstrapProbe(
            storage=_StorageStub(_results),
            embedder=_EmbedderStub(),
            collection_resolver=lambda: "context",
            filter_builder=lambda: {"op": "and", "conds": []},
        )

        result = await probe.probe("下周二在杭州住哪里？")

        self.assertTrue(result.should_recall)
        self.assertEqual(result.evidence.object_candidate_count, 0)
        self.assertEqual(result.evidence.anchor_candidate_count, 1)
        self.assertEqual(result.evidence.object_top_score, None)
        self.assertEqual(result.evidence.anchor_top_score, 0.67)
        self.assertGreaterEqual(result.evidence.anchor_hit_count, 2)
        self.assertEqual(result.candidate_entries[0].uri, "opencortex://memory/events/anchor-only")
        self.assertIn("杭州", result.anchor_hits)
        self.assertGreater(result.trace.anchor_candidates, 0)


if __name__ == "__main__":
    unittest.main()
