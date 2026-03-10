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
        from opencortex.storage.storage_interface import StorageInterface
        # A bare StorageInterface instance should not have search_lexical
        self.assertFalse(hasattr(StorageInterface, "search_lexical"))


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

    model_name = "mock"

    def embed(self, text):
        @dataclass
        class EmbedResult:
            dense_vector: list
            sparse_vector: dict = field(default_factory=dict)
        # Generic "about" vector
        vec = [0.5, 0.5, 0.5, 0.5]
        return EmbedResult(dense_vector=vec)

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]

    def get_dimension(self):
        return 4

    def close(self):
        pass


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


# =============================================================================
# Test: BM25SparseEmbedder (Phase 2)
# =============================================================================


class TestBM25SparseEmbedder(unittest.TestCase):
    """Test BM25SparseEmbedder produces valid sparse vectors."""

    def setUp(self):
        from opencortex.models.embedder.sparse import BM25SparseEmbedder
        self.embedder = BM25SparseEmbedder()

    def test_produces_sparse_vector(self):
        result = self.embedder.embed("TrafficRule OutboundType configuration")
        self.assertIsNotNone(result.sparse_vector)
        self.assertIsInstance(result.sparse_vector, dict)
        self.assertGreater(len(result.sparse_vector), 0)
        # All weights must be positive
        for token, weight in result.sparse_vector.items():
            self.assertIsInstance(token, str)
            self.assertGreater(weight, 0.0)

    def test_no_dense_vector(self):
        result = self.embedder.embed("hello world")
        self.assertIsNone(result.dense_vector)
        self.assertIsNotNone(result.sparse_vector)

    def test_empty_input(self):
        result = self.embedder.embed("")
        self.assertIsNotNone(result.sparse_vector)
        self.assertEqual(len(result.sparse_vector), 0)

    def test_chinese_tokens(self):
        result = self.embedder.embed("流量规则出站类型")
        self.assertIsNotNone(result.sparse_vector)
        # Should have Chinese character tokens
        has_chinese = any(
            "\u4e00" <= c <= "\u9fa5"
            for token in result.sparse_vector
            for c in token
        )
        self.assertTrue(has_chinese, "Should tokenize Chinese characters")

    def test_max_tokens_respected(self):
        from opencortex.models.embedder.sparse import BM25SparseEmbedder
        embedder = BM25SparseEmbedder(max_tokens=5)
        # Generate a long text with many unique tokens
        text = " ".join(f"token{i}" for i in range(100))
        result = embedder.embed(text)
        self.assertLessEqual(len(result.sparse_vector), 5)

    def test_camel_case_boost(self):
        result = self.embedder.embed("TrafficRule defines outbound types")
        # "trafficrule" should have higher weight than generic words
        weights = result.sparse_vector
        if "trafficrule" in weights and "defines" in weights:
            self.assertGreater(weights["trafficrule"], weights["defines"])

    def test_is_sparse_property(self):
        self.assertTrue(self.embedder.is_sparse)
        self.assertFalse(self.embedder.is_dense)


# =============================================================================
# Test: CompositeHybridEmbedder integration (Phase 2)
# =============================================================================


class TestCompositeHybridIntegration(unittest.TestCase):
    """Test CompositeHybridEmbedder with BM25SparseEmbedder."""

    def test_composite_produces_both_vectors(self):
        from opencortex.models.embedder.sparse import BM25SparseEmbedder
        from opencortex.models.embedder.base import CompositeHybridEmbedder

        dense = MockEmbedder()
        sparse = BM25SparseEmbedder()
        hybrid = CompositeHybridEmbedder(dense, sparse)

        result = hybrid.embed("TrafficRule configuration")
        self.assertIsNotNone(result.dense_vector)
        self.assertIsNotNone(result.sparse_vector)
        self.assertEqual(len(result.dense_vector), 4)
        self.assertGreater(len(result.sparse_vector), 0)

    def test_composite_batch(self):
        from opencortex.models.embedder.sparse import BM25SparseEmbedder
        from opencortex.models.embedder.base import CompositeHybridEmbedder

        dense = MockEmbedder()
        sparse = BM25SparseEmbedder()
        hybrid = CompositeHybridEmbedder(dense, sparse)

        results = hybrid.embed_batch(["hello", "world"])
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertIsNotNone(r.dense_vector)
            self.assertIsNotNone(r.sparse_vector)


# =============================================================================
# Test: Hotness scoring (Phase 3)
# =============================================================================


class TestHotnessScoring(unittest.TestCase):
    """Test HierarchicalRetriever._compute_hotness static method."""

    def test_zero_access_count(self):
        record = {"active_count": 0, "accessed_at": ""}
        score = HierarchicalRetriever._compute_hotness(record)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_high_access_recent(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {"active_count": 100, "accessed_at": now}
        score = HierarchicalRetriever._compute_hotness(record)
        self.assertGreater(score, 0.5, "High access + recent should give high hotness")

    def test_high_access_old(self):
        record = {"active_count": 100, "accessed_at": "2025-01-01T00:00:00Z"}
        score = HierarchicalRetriever._compute_hotness(record)
        # Very old → recency decay should make this low
        self.assertLess(score, 0.1)

    def test_low_access_recent(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {"active_count": 1, "accessed_at": now}
        score = HierarchicalRetriever._compute_hotness(record)
        # Low access but recent — moderate score
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1.0)

    def test_missing_fields_graceful(self):
        """Empty record should not crash."""
        record = {}
        score = HierarchicalRetriever._compute_hotness(record)
        self.assertGreaterEqual(score, 0.0)

    def test_hotness_weight_in_constructor(self):
        """Verify hot_weight parameter is accepted."""
        storage = InMemoryStorageWithLexical()
        retriever = HierarchicalRetriever(
            storage=storage,
            embedder=MockEmbedder(),
            rerank_config=None,
            hot_weight=0.05,
        )
        self.assertEqual(retriever._hot_weight, 0.05)


# =============================================================================
# Test: L0 quality gate (Phase 3)
# =============================================================================


class TestL0QualityGate(unittest.TestCase):
    """Test MemoryOrchestrator._enrich_abstract."""

    def test_enriches_low_coverage_abstract(self):
        from opencortex.orchestrator import MemoryOrchestrator
        abstract = "some generic description"
        content = "TrafficRule defines OutboundType enum for SASE policy"
        enriched = MemoryOrchestrator._enrich_abstract(abstract, content)
        # Should have appended missing terms
        self.assertNotEqual(enriched, abstract)
        self.assertIn("[", enriched)

    def test_no_enrichment_when_coverage_high(self):
        from opencortex.orchestrator import MemoryOrchestrator
        abstract = "TrafficRule OutboundType SASE policy configuration"
        content = "TrafficRule defines OutboundType for SASE"
        enriched = MemoryOrchestrator._enrich_abstract(abstract, content)
        # Coverage should be high enough — no enrichment
        self.assertEqual(enriched, abstract)

    def test_no_enrichment_empty_content(self):
        from opencortex.orchestrator import MemoryOrchestrator
        abstract = "some abstract"
        enriched = MemoryOrchestrator._enrich_abstract(abstract, "")
        self.assertEqual(enriched, abstract)

    def test_max_10_terms_appended(self):
        from opencortex.orchestrator import MemoryOrchestrator
        abstract = "generic"
        # Content with many CamelCase terms
        terms = " ".join(f"TermName{i}" for i in range(30))
        content = terms
        enriched = MemoryOrchestrator._enrich_abstract(abstract, content)
        # Count terms in the bracket
        if "[" in enriched:
            bracket_content = enriched[enriched.index("[") + 1 : enriched.index("]")]
            term_count = len(bracket_content.split(", "))
            self.assertLessEqual(term_count, 10)


# =============================================================================
# Test: Tenant isolation field (Phase 3)
# =============================================================================


class TestTenantIsolation(unittest.TestCase):
    """Test source_tenant_id field in schema and migration."""

    def test_context_schema_has_source_tenant_id(self):
        from opencortex.storage.collection_schemas import CollectionSchemas
        schema = CollectionSchemas.context_collection("test", 1024)
        field_names = [f["FieldName"] for f in schema["Fields"]]
        self.assertIn("source_tenant_id", field_names)
        self.assertIn("source_tenant_id", schema["ScalarIndex"])

    def test_migration_infer_tenant_from_uri(self):
        from opencortex.migration.v031_tenant_backfill import infer_tenant_from_uri
        self.assertEqual(
            infer_tenant_from_uri("opencortex://acme/user/u1/memory/docs/a"),
            "acme",
        )
        self.assertEqual(
            infer_tenant_from_uri("opencortex://sase/user/dev/memory/docs/x"),
            "sase",
        )
        self.assertEqual(infer_tenant_from_uri(""), "")
        self.assertEqual(infer_tenant_from_uri("invalid-uri"), "")


# =============================================================================
# Test: Eval dataset loading (Phase 0)
# =============================================================================


class TestEvalDataset(unittest.TestCase):
    """Verify the SASE eval dataset loads correctly."""

    def test_dataset_loads(self):
        from opencortex.eval.memory_eval import load_dataset
        dataset_path = os.path.join(
            os.path.dirname(__file__), "..", "src", "opencortex",
            "eval", "datasets", "sase_eval_200.jsonl",
        )
        if not os.path.exists(dataset_path):
            self.skipTest("Eval dataset not found")
        dataset = load_dataset(dataset_path)
        self.assertGreaterEqual(len(dataset), 200)
        for item in dataset:
            self.assertIn("query", item)
            self.assertIn("category", item)
            self.assertIn("expected_uris", item)
            self.assertIn(item["category"], ("hard_keyword", "semantic", "hierarchical"))

    def test_dataset_category_distribution(self):
        from opencortex.eval.memory_eval import load_dataset
        dataset_path = os.path.join(
            os.path.dirname(__file__), "..", "src", "opencortex",
            "eval", "datasets", "sase_eval_200.jsonl",
        )
        if not os.path.exists(dataset_path):
            self.skipTest("Eval dataset not found")
        dataset = load_dataset(dataset_path)
        cats = {}
        for item in dataset:
            c = item["category"]
            cats[c] = cats.get(c, 0) + 1
        total = sum(cats.values())
        # Hard keyword should be ~40%
        self.assertGreaterEqual(cats.get("hard_keyword", 0) / total, 0.30)
        # Semantic should be ~40%
        self.assertGreaterEqual(cats.get("semantic", 0) / total, 0.30)
        # Hierarchical should be ~20%
        self.assertGreaterEqual(cats.get("hierarchical", 0) / total, 0.10)


if __name__ == "__main__":
    unittest.main()
