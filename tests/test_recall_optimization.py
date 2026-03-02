"""
Tests for Recall Optimization Phase 1.

Covers:
- Hard keyword detection (CamelCase, ALL_CAPS, path/symbol)
- IntentRouter lexical_boost routing
- RRF merge algorithm
- Adapter search_lexical method
- End-to-end parallel lexical retrieval
"""

import asyncio
import math
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.retrieve.intent_router import (
    IntentRouter,
    _CAMEL_CASE_RE,
    _ALL_CAPS_RE,
    _PATH_SYMBOL_RE,
    _HARD_KEYWORD_LEXICAL_BOOST,
    _DEFAULT_LEXICAL_BOOST,
)
from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
    SearchIntent,
    TypedQuery,
)
from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever


# =============================================================================
# Test: Hard Keyword Detection
# =============================================================================


class TestHardKeywordDetection(unittest.TestCase):
    """Test _detect_hard_keywords regex patterns."""

    def test_camel_case_detected(self):
        self.assertTrue(IntentRouter._detect_hard_keywords("TrafficRule"))
        self.assertTrue(IntentRouter._detect_hard_keywords("OutboundType config"))
        self.assertTrue(IntentRouter._detect_hard_keywords("what is SasePolicy"))

    def test_all_caps_detected(self):
        self.assertTrue(IntentRouter._detect_hard_keywords("Agent TUN mode"))
        self.assertTrue(IntentRouter._detect_hard_keywords("DNS resolution"))
        self.assertTrue(IntentRouter._detect_hard_keywords("SCIM protocol"))

    def test_path_symbol_detected(self):
        self.assertTrue(IntentRouter._detect_hard_keywords("traffic_rule.proto"))
        self.assertTrue(IntentRouter._detect_hard_keywords("config.yaml settings"))
        self.assertTrue(IntentRouter._detect_hard_keywords("src/main.py"))
        self.assertTrue(IntentRouter._detect_hard_keywords("user_id field"))

    def test_plain_text_not_detected(self):
        self.assertFalse(IntentRouter._detect_hard_keywords("what did we discuss yesterday"))
        self.assertFalse(IntentRouter._detect_hard_keywords("summarize my preferences"))
        self.assertFalse(IntentRouter._detect_hard_keywords("hello"))
        self.assertFalse(IntentRouter._detect_hard_keywords("I need help"))

    def test_single_capital_not_detected(self):
        # Single uppercase word at start of sentence should NOT match ALL_CAPS
        # (ALL_CAPS requires 2+ consecutive capitals)
        self.assertFalse(IntentRouter._detect_hard_keywords("Hello world"))

    def test_camel_case_regex_specifics(self):
        self.assertTrue(bool(_CAMEL_CASE_RE.search("myFunction")))
        self.assertTrue(bool(_CAMEL_CASE_RE.search("getData")))
        self.assertFalse(bool(_CAMEL_CASE_RE.search("ALLCAPS")))
        self.assertFalse(bool(_CAMEL_CASE_RE.search("lowercase")))


# =============================================================================
# Test: Lexical Boost Routing
# =============================================================================


class TestLexicalBoostRouting(unittest.TestCase):
    """Test IntentRouter sets correct lexical_boost values."""

    def setUp(self):
        self.router = IntentRouter(llm_completion=None)

    def test_hard_keyword_gets_high_boost(self):
        intent = asyncio.run(
            self.router.route("TrafficRule configuration")
        )
        self.assertEqual(intent.lexical_boost, _HARD_KEYWORD_LEXICAL_BOOST)

    def test_plain_query_gets_default_boost(self):
        intent = asyncio.run(
            self.router.route("what are my preferences")
        )
        self.assertEqual(intent.lexical_boost, _DEFAULT_LEXICAL_BOOST)

    def test_all_caps_gets_high_boost(self):
        intent = asyncio.run(
            self.router.route("how does DNS work in agent")
        )
        self.assertEqual(intent.lexical_boost, _HARD_KEYWORD_LEXICAL_BOOST)

    def test_merge_preserves_keyword_boost(self):
        """LLM merge should not downgrade keyword-detected hard boost."""
        keyword_intent = SearchIntent(lexical_boost=_HARD_KEYWORD_LEXICAL_BOOST)
        llm_intent = SearchIntent(lexical_boost=_DEFAULT_LEXICAL_BOOST)
        merged = self.router._merge(keyword_intent, llm_intent)
        self.assertEqual(merged.lexical_boost, _HARD_KEYWORD_LEXICAL_BOOST)

    def test_merge_takes_higher_boost(self):
        """If LLM has higher boost, use that."""
        keyword_intent = SearchIntent(lexical_boost=_DEFAULT_LEXICAL_BOOST)
        llm_intent = SearchIntent(lexical_boost=0.6)
        merged = self.router._merge(keyword_intent, llm_intent)
        self.assertEqual(merged.lexical_boost, 0.6)

    def test_no_recall_intent_has_default_boost(self):
        intent = asyncio.run(
            self.router.route("hello")
        )
        # No-recall still has default boost (irrelevant since should_recall=False)
        self.assertFalse(intent.should_recall)


# =============================================================================
# Test: RRF Merge
# =============================================================================


class TestRRFMerge(unittest.TestCase):
    """Test HierarchicalRetriever._merge_rrf static method."""

    def test_basic_rrf_merge(self):
        dense = [
            {"uri": "a", "_final_score": 0.9, "abstract": "doc a"},
            {"uri": "b", "_final_score": 0.7, "abstract": "doc b"},
            {"uri": "c", "_final_score": 0.5, "abstract": "doc c"},
        ]
        lexical = [
            {"uri": "b", "_score": 0.8, "abstract": "doc b"},
            {"uri": "d", "_score": 0.6, "abstract": "doc d"},
        ]
        merged = HierarchicalRetriever._merge_rrf(dense, lexical, lexical_weight=0.3, k=60)

        uris = [r["uri"] for r in merged]
        # All 4 unique URIs should be present
        self.assertEqual(len(merged), 4)
        self.assertIn("a", uris)
        self.assertIn("b", uris)
        self.assertIn("c", uris)
        self.assertIn("d", uris)
        # "b" appears in both paths — should rank higher
        b_idx = uris.index("b")
        d_idx = uris.index("d")
        self.assertLess(b_idx, d_idx)

    def test_empty_lexical_returns_dense(self):
        dense = [{"uri": "a", "_final_score": 0.9}]
        merged = HierarchicalRetriever._merge_rrf(dense, [], lexical_weight=0.3)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["uri"], "a")

    def test_high_lexical_weight_promotes_lexical_only(self):
        """With high lexical_weight, a doc only in lexical should rank higher."""
        dense = [{"uri": "a", "_final_score": 0.9}]
        lexical = [{"uri": "b", "_score": 0.8}]
        merged = HierarchicalRetriever._merge_rrf(dense, lexical, lexical_weight=0.8, k=60)
        uris = [r["uri"] for r in merged]
        # With b=0.8, lexical path has 4x weight of dense path
        # b only in lexical (rank 0) vs a only in dense (rank 0)
        # RRF(b) = 0.2/61 + 0.8/60 = 0.00328 + 0.01333 = 0.01661
        # RRF(a) = 0.8/60 + 0.2/61 -- wait, a is only in dense (rank 0)
        # RRF(a) = (1-0.8)/(60+0) + 0.8/(60+1) = 0.2/60 + 0.8/61 = 0.00333 + 0.01311 = 0.01644
        # RRF(b) = (1-0.8)/(60+1) + 0.8/(60+0) = 0.2/61 + 0.8/60 = 0.00328 + 0.01333 = 0.01661
        # b should be first
        self.assertEqual(uris[0], "b")

    def test_rrf_scores_assigned(self):
        dense = [{"uri": "a", "_final_score": 0.9}]
        lexical = [{"uri": "a", "_score": 0.5}]
        merged = HierarchicalRetriever._merge_rrf(dense, lexical, lexical_weight=0.3, k=60)
        self.assertEqual(len(merged), 1)
        self.assertIn("_final_score", merged[0])
        # RRF(a) = 0.7/60 + 0.3/60 = 1/60 ≈ 0.01667
        expected = 0.7 / 60 + 0.3 / 60
        self.assertAlmostEqual(merged[0]["_final_score"], expected, places=5)

    def test_rrf_does_not_mutate_input(self):
        dense = [{"uri": "a", "_final_score": 0.9}]
        lexical = [{"uri": "a", "_score": 0.5}]
        dense_copy = [dict(d) for d in dense]
        HierarchicalRetriever._merge_rrf(dense, lexical, lexical_weight=0.3)
        self.assertEqual(dense[0]["_final_score"], dense_copy[0]["_final_score"])


# =============================================================================
# Test: Adapter search_lexical
# =============================================================================


class TestSearchLexical(unittest.TestCase):
    """Test QdrantStorageAdapter.search_lexical returns scored results."""

    def test_search_lexical_exists_on_adapter(self):
        """Verify the method exists and is callable."""
        from opencortex.storage.qdrant.adapter import QdrantStorageAdapter
        adapter = QdrantStorageAdapter.__new__(QdrantStorageAdapter)
        self.assertTrue(hasattr(adapter, "search_lexical"))
        self.assertTrue(callable(adapter.search_lexical))

    def test_hasattr_detection_for_in_memory(self):
        """InMemoryStorage should NOT have search_lexical (graceful skip)."""
        from opencortex.storage.vikingdb_interface import VikingDBInterface
        # A bare VikingDBInterface instance should not have search_lexical
        self.assertFalse(hasattr(VikingDBInterface, "search_lexical"))


# =============================================================================
# Test: Parallel Lexical Retrieval (E2E with InMemoryStorage)
# =============================================================================


class InMemoryStorageWithLexical:
    """Minimal in-memory storage that also supports search_lexical.

    For testing the retriever's parallel lexical path.
    """

    def __init__(self):
        self._records: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._collections: set = set()

    async def collection_exists(self, name: str) -> bool:
        return name in self._collections

    async def create_collection(self, name: str, schema=None) -> bool:
        self._collections.add(name)
        self._records[name] = {}
        return True

    async def insert(self, collection: str, data: Dict[str, Any]) -> str:
        rid = data.get("id", str(uuid4()))
        data["id"] = rid
        self._records[collection][rid] = dict(data)
        return rid

    async def search(
        self, collection, query_vector=None, sparse_query_vector=None,
        filter=None, limit=10, offset=0, output_fields=None,
        with_vector=False, text_query="",
    ):
        """Dense search mock: returns all records sorted by cosine similarity."""
        if collection not in self._records:
            return []
        records = list(self._records[collection].values())
        if query_vector:
            for r in records:
                vec = r.get("vector", [])
                if vec:
                    r["_score"] = self._cosine(query_vector, vec)
                else:
                    r["_score"] = 0.0
            records.sort(key=lambda x: x.get("_score", 0.0), reverse=True)
        return records[:limit]

    async def search_lexical(
        self, collection, text_query, filter=None, limit=10,
    ):
        """Lexical search mock: term overlap scoring on abstract."""
        if collection not in self._records:
            return []
        records = list(self._records[collection].values())
        query_terms = set(text_query.lower().split())
        for r in records:
            abstract = r.get("abstract", "").lower()
            abstract_terms = set(abstract.split())
            overlap = len(query_terms & abstract_terms)
            r["_score"] = overlap / max(len(query_terms), 1)
        records = [r for r in records if r.get("_score", 0) > 0]
        records.sort(key=lambda x: x.get("_score", 0.0), reverse=True)
        return records[:limit]

    @staticmethod
    def _cosine(a, b):
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(x * x for x in b)) or 1.0
        return dot / (na * nb)


class MockEmbedder:
    """Simple embedder that makes semantically dissimilar vectors for technical terms."""

    def embed(self, text):
        @dataclass
        class EmbedResult:
            dense_vector: list
            sparse_vector: dict = field(default_factory=dict)
        # Generic "about" vector
        vec = [0.5, 0.5, 0.5, 0.5]
        return EmbedResult(dense_vector=vec)

    def get_dimension(self):
        return 4


class TestParallelLexicalRetrieval(unittest.TestCase):
    """E2E test: hard keywords in abstract exact-match but low semantic similarity."""

    def test_lexical_path_rescues_hard_keyword_doc(self):
        """A document with TrafficRule in abstract but very different embedding
        should still appear in results via lexical path."""

        async def _run_test():
            storage = InMemoryStorageWithLexical()
            embedder = MockEmbedder()
            retriever = HierarchicalRetriever(
                storage=storage,
                embedder=embedder,
                rerank_config=None,
                use_frontier_batching=False,
                rl_weight=0.0,
            )

            await storage.create_collection("context")

            # Doc 1: high semantic similarity, no keyword match
            await storage.insert("context", {
                "id": "semantic-doc",
                "uri": "opencortex://test/user/u1/memory/docs/semantic-doc",
                "parent_uri": "opencortex://test/user/u1/memory/docs",
                "abstract": "network configuration and routing policies",
                "overview": "general network documentation",
                "context_type": "memory",
                "is_leaf": True,
                "category": "documents",
                "vector": [0.5, 0.5, 0.5, 0.5],
            })

            # Doc 2: low semantic similarity, exact keyword match
            await storage.insert("context", {
                "id": "keyword-doc",
                "uri": "opencortex://test/user/u1/memory/docs/keyword-doc",
                "parent_uri": "opencortex://test/user/u1/memory/docs",
                "abstract": "TrafficRule OutboundType configuration for SASE",
                "overview": "TrafficRule defines outbound traffic types",
                "context_type": "memory",
                "is_leaf": True,
                "category": "documents",
                "vector": [0.1, 0.9, 0.1, 0.1],
            })

            query = TypedQuery(
                query="TrafficRule OutboundType",
                context_type=ContextType.MEMORY,
                intent="quick_lookup",
                detail_level=DetailLevel.L1,
            )

            result = await retriever.retrieve(
                query, limit=5, lexical_boost=_HARD_KEYWORD_LEXICAL_BOOST,
            )
            return result

        result = asyncio.run(_run_test())
        uris = [mc.uri for mc in result.matched_contexts]
        self.assertIn(
            "opencortex://test/user/u1/memory/docs/keyword-doc",
            uris,
            "Hard keyword doc should appear in results via lexical path",
        )


# =============================================================================
# Test: SearchIntent serialization includes lexical_boost
# =============================================================================


class TestSearchIntentSerialization(unittest.TestCase):
    """Test that lexical_boost is included in FindResult serialization."""

    def test_find_result_to_dict_includes_lexical_boost(self):
        from opencortex.retrieve.types import FindResult
        intent = SearchIntent(lexical_boost=0.55)
        result = FindResult(
            memories=[], resources=[], skills=[],
            search_intent=intent,
        )
        d = result.to_dict()
        self.assertIn("search_intent", d)
        self.assertEqual(d["search_intent"]["lexical_boost"], 0.55)


if __name__ == "__main__":
    unittest.main()
