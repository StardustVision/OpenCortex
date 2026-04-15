import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.intent import MemoryBootstrapProbe
from opencortex.intent.types import ScopeLevel


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

        first_filter = str(storage.calls[0]["filter"])
        self.assertIn("scope", first_filter)
        self.assertIn("session_id", first_filter)
        self.assertIn("is_leaf", first_filter)
        self.assertIn("retrieval_surface", first_filter)
        self.assertIn("l0_object", first_filter)
        self.assertTrue(
            any(
                str(call.get("filter", {})).find("anchor_hits") >= 0
                for call in storage.calls[1:]
            )
        )
        self.assertTrue(
            any(
                str(call.get("filter", {})).find("anchor_surface") >= 0
                for call in storage.calls[1:]
            )
        )

    async def test_probe_can_return_anchor_only_hits(self):
        source_uri = "opencortex://memory/events/source-1"

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
                        "uri": f"{source_uri}/anchors/anchor-only",
                        "parent_uri": source_uri,
                        "category": "event",
                        "context_type": "memory",
                        "abstract": "",
                        "overview": "杭州",
                        "_text_score": 0.67,
                        "retrieval_surface": "anchor_projection",
                        "anchor_hits": ["杭州", "下周二", "西湖"],
                        "projection_target_uri": source_uri,
                        "projection_target_abstract": "下周二去杭州出差，住在西湖边。",
                        "projection_target_overview": "杭州出差安排",
                        "meta": {"anchor_type": "entity", "anchor_text": "杭州"},
                        "entities": ["杭州"],
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
        self.assertEqual(result.candidate_entries[0].uri, source_uri)
        self.assertEqual(
            result.candidate_entries[0].abstract,
            "下周二去杭州出差，住在西湖边。",
        )
        self.assertIn("杭州", result.anchor_hits)
        self.assertGreater(result.trace.anchor_candidates, 0)

    async def test_starting_points_empty_for_global_query(self):
        probe = MemoryBootstrapProbe(
            storage=_StorageStub([]),
            embedder=_EmbedderStub(),
            collection_resolver=lambda: "context",
            filter_builder=lambda: {"op": "and", "conds": []},
        )

        result = await probe.probe("What happened before launch?")

        self.assertTrue(result.should_recall)
        self.assertEqual(result.starting_points, [])
        self.assertEqual(result.scope_level, ScopeLevel.GLOBAL)
        self.assertIn("launch", result.query_entities)

    async def test_starting_points_session_scoped(self):
        storage = _StorageStub(
            [
                {
                    "uri": "opencortex://t/u/memories/events/s1",
                    "session_id": "s1",
                    "parent_uri": "opencortex://t/u/memories/events",
                    "is_leaf": False,
                    "abstract": "Session root",
                    "_score": 0.9,
                    "structured_slots": {
                        "entities": ["Alice"],
                        "time_refs": ["2024-01-01"],
                    },
                },
                {
                    "uri": "opencortex://t/u/memories/events/s1/m1",
                    "session_id": "s1",
                    "parent_uri": "opencortex://t/u/memories/events/s1",
                    "is_leaf": True,
                    "abstract": "Immediate message",
                    "_score": 0.8,
                    "meta": {"layer": "immediate"},
                },
            ]
        )
        probe = MemoryBootstrapProbe(
            storage=storage,
            embedder=_EmbedderStub(),
            collection_resolver=lambda: "context",
            filter_builder=lambda: {"op": "and", "conds": []},
        )

        result = await probe.probe("What did Alice say?")

        self.assertEqual(len(result.starting_points), 1)
        self.assertEqual(result.starting_points[0].uri, "opencortex://t/u/memories/events/s1")
        self.assertEqual(result.starting_points[0].session_id, "s1")
        self.assertEqual(result.scope_level, ScopeLevel.SESSION_ONLY)
        self.assertIn("Alice", result.starting_point_anchors)

    async def test_starting_points_document_scoped(self):
        storage = _StorageStub(
            [
                {
                    "uri": "opencortex://t/u/resources/documents/doc-1",
                    "source_doc_id": "doc-1",
                    "parent_uri": "",
                    "is_leaf": False,
                    "abstract": "Document root",
                    "_score": 0.85,
                    "structured_slots": {
                        "entities": ["Project X"],
                    },
                },
                {
                    "uri": "opencortex://t/u/resources/documents/doc-1/chunk-1",
                    "source_doc_id": "doc-1",
                    "parent_uri": "opencortex://t/u/resources/documents/doc-1",
                    "is_leaf": True,
                    "abstract": "Chunk",
                    "_score": 0.75,
                },
            ]
        )
        probe = MemoryBootstrapProbe(
            storage=storage,
            embedder=_EmbedderStub(),
            collection_resolver=lambda: "context",
            filter_builder=lambda: {"op": "and", "conds": []},
        )

        result = await probe.probe("Tell me about Project X")

        self.assertEqual(len(result.starting_points), 1)
        self.assertEqual(result.starting_points[0].uri, "opencortex://t/u/resources/documents/doc-1")
        self.assertEqual(result.starting_points[0].source_doc_id, "doc-1")
        self.assertEqual(result.scope_level, ScopeLevel.DOCUMENT_ONLY)

    async def test_starting_points_most_specific_scope_wins(self):
        storage = _StorageStub(
            [
                {
                    "uri": "opencortex://t/u/memories/events/s1",
                    "session_id": "s1",
                    "parent_uri": "opencortex://t/u/memories/events",
                    "is_leaf": False,
                    "abstract": "Session root",
                    "_score": 0.9,
                },
                {
                    "uri": "opencortex://t/u/memories/events/s1/nested/container",
                    "session_id": "s1",
                    "parent_uri": "opencortex://t/u/memories/events/s1/nested",
                    "is_leaf": False,
                    "abstract": "Nested container",
                    "_score": 0.85,
                },
            ]
        )
        probe = MemoryBootstrapProbe(
            storage=storage,
            embedder=_EmbedderStub(),
            collection_resolver=lambda: "context",
            filter_builder=lambda: {"op": "and", "conds": []},
        )

        result = await probe.probe("Query")

        self.assertEqual(len(result.starting_points), 2)
        # With current storage hierarchy, any session_id-bearing record
        # maps to SESSION_ONLY because parent_uri traversal is not reliable.
        self.assertEqual(result.scope_level, ScopeLevel.SESSION_ONLY)


if __name__ == "__main__":
    unittest.main()
