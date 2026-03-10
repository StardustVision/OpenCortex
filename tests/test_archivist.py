import json
import unittest
from opencortex.alpha.archivist import cluster_traces, Archivist, _cosine_similarity
from opencortex.alpha.types import KnowledgeType, KnowledgeScope, KnowledgeStatus
from unittest.mock import MagicMock


class TestClustering(unittest.TestCase):

    def test_cluster_by_source_task_type(self):
        """Traces with same source+task_type cluster together."""
        traces = [
            {"trace_id": "t1", "source": "claude", "task_type": "debug", "abstract": "fix A"},
            {"trace_id": "t2", "source": "claude", "task_type": "debug", "abstract": "fix B"},
            {"trace_id": "t3", "source": "claude", "task_type": "coding", "abstract": "add C"},
        ]
        clusters = cluster_traces(traces)
        self.assertEqual(len(clusters), 2)

    def test_cluster_without_embedder(self):
        """Without embedder, each group is one cluster."""
        traces = [
            {"trace_id": "t1", "source": "claude", "task_type": "debug"},
            {"trace_id": "t2", "source": "claude", "task_type": "debug"},
        ]
        clusters = cluster_traces(traces, embedder=None)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]), 2)

    def test_empty_traces(self):
        clusters = cluster_traces([])
        self.assertEqual(len(clusters), 0)

    def test_cosine_similarity(self):
        a = [1.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0]
        self.assertAlmostEqual(_cosine_similarity(a, b), 1.0)

        c = [0.0, 1.0, 0.0]
        self.assertAlmostEqual(_cosine_similarity(a, c), 0.0)

    def test_cluster_with_mock_embedder(self):
        """With embedder, similar abstracts cluster together."""
        embedder = MagicMock()
        # Same vectors for similar traces, different for dissimilar
        def embed_fn(text):
            result = MagicMock()
            if "import" in text:
                result.dense = [1.0, 0.0, 0.0]
            else:
                result.dense = [0.0, 1.0, 0.0]
            return result
        embedder.embed = embed_fn

        traces = [
            {"trace_id": "t1", "source": "claude", "task_type": "debug", "abstract": "fix import error"},
            {"trace_id": "t2", "source": "claude", "task_type": "debug", "abstract": "fix import bug"},
            {"trace_id": "t3", "source": "claude", "task_type": "debug", "abstract": "fix typo"},
        ]
        clusters = cluster_traces(traces, embedder=embedder, similarity_threshold=0.8)
        # t1 and t2 should cluster (same vector), t3 separate
        self.assertEqual(len(clusters), 2)


class TestArchivist(unittest.IsolatedAsyncioTestCase):

    async def test_extract_from_cluster(self):
        """Extract knowledge from a cluster of traces."""
        async def mock_llm(prompt):
            return json.dumps([{
                "type": "belief",
                "statement": "Always check spelling before pip install",
                "objective": "Prevent import errors",
                "trigger_keywords": ["import", "pip"],
            }])

        archivist = Archivist(llm_fn=mock_llm)
        cluster = [
            {"trace_id": "t1", "abstract": "Fixed import error", "outcome": "success", "task_type": "debug"},
            {"trace_id": "t2", "abstract": "Fixed another import", "outcome": "success", "task_type": "debug"},
        ]
        items = await archivist.extract_from_cluster(
            cluster, "team", "hugo", KnowledgeScope.USER,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].knowledge_type, KnowledgeType.BELIEF)
        self.assertEqual(items[0].status, KnowledgeStatus.CANDIDATE)
        self.assertEqual(len(items[0].source_trace_ids), 2)

    async def test_extract_multiple_types(self):
        """LLM returns multiple knowledge types."""
        async def mock_llm(prompt):
            return json.dumps([
                {"type": "sop", "objective": "Fix imports", "action_steps": ["Check", "Install"]},
                {"type": "negative_rule", "statement": "Never skip checks", "severity": "high"},
            ])

        archivist = Archivist(llm_fn=mock_llm)
        cluster = [
            {"trace_id": "t1", "abstract": "task 1"},
            {"trace_id": "t2", "abstract": "task 2"},
        ]
        items = await archivist.extract_from_cluster(
            cluster, "team", "hugo",
        )
        self.assertEqual(len(items), 2)
        types = {i.knowledge_type for i in items}
        self.assertIn(KnowledgeType.SOP, types)
        self.assertIn(KnowledgeType.NEGATIVE_RULE, types)

    async def test_run_skips_singletons(self):
        """Full run skips clusters with only 1 trace."""
        async def mock_llm(prompt):
            return json.dumps([{"type": "belief", "statement": "test"}])

        archivist = Archivist(llm_fn=mock_llm)
        traces = [
            {"trace_id": "t1", "source": "claude", "task_type": "debug", "abstract": "A"},
            {"trace_id": "t2", "source": "claude", "task_type": "coding", "abstract": "B"},
        ]
        # Each in different group -> singletons -> skipped
        items = await archivist.run(traces, "team", "hugo")
        self.assertEqual(len(items), 0)

    async def test_run_extracts_from_cluster(self):
        """Full run extracts from clusters with 2+ traces."""
        async def mock_llm(prompt):
            return json.dumps([{"type": "belief", "statement": "test"}])

        archivist = Archivist(llm_fn=mock_llm)
        traces = [
            {"trace_id": "t1", "source": "claude", "task_type": "debug", "abstract": "A"},
            {"trace_id": "t2", "source": "claude", "task_type": "debug", "abstract": "B"},
        ]
        items = await archivist.run(traces, "team", "hugo")
        self.assertGreater(len(items), 0)

    async def test_llm_error_returns_empty(self):
        """LLM error returns empty list."""
        async def mock_llm(prompt):
            return "invalid json"

        archivist = Archivist(llm_fn=mock_llm)
        items = await archivist.extract_from_cluster(
            [{"trace_id": "t1"}, {"trace_id": "t2"}],
            "team", "hugo",
        )
        self.assertEqual(len(items), 0)

    def test_should_trigger(self):
        """Trigger mode auto vs manual."""
        a = Archivist(llm_fn=None, trigger_threshold=5, trigger_mode="auto")
        self.assertTrue(a.should_trigger(5))
        self.assertFalse(a.should_trigger(4))

        m = Archivist(llm_fn=None, trigger_mode="manual")
        self.assertFalse(m.should_trigger(100))

    def test_status(self):
        a = Archivist(llm_fn=None, trigger_threshold=10, trigger_mode="auto")
        s = a.status
        self.assertFalse(s["running"])
        self.assertEqual(s["trigger_threshold"], 10)


if __name__ == "__main__":
    unittest.main()
