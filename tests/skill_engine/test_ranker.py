import unittest
from unittest.mock import AsyncMock, MagicMock
from opencortex.skill_engine.types import SkillRecord, SkillCategory, SkillStatus
from opencortex.skill_engine.ranker import (
    SkillRanker, _tokenize, _bm25_score,
)
from opencortex.utils.similarity import cosine_similarity


class TestTokenize(unittest.TestCase):

    def test_english_tokens(self):
        tokens = _tokenize("Deploy the application")
        self.assertIn("deploy", tokens)
        self.assertIn("the", tokens)
        self.assertIn("application", tokens)

    def test_chinese_unigrams(self):
        tokens = _tokenize("部署应用")
        self.assertIn("部", tokens)
        self.assertIn("署", tokens)
        self.assertIn("应", tokens)
        self.assertIn("用", tokens)

    def test_mixed_language(self):
        tokens = _tokenize("deploy 部署")
        self.assertIn("deploy", tokens)
        self.assertIn("部", tokens)

    def test_empty_string(self):
        self.assertEqual(_tokenize(""), [])


class TestBM25(unittest.TestCase):

    def test_matching_query(self):
        query = _tokenize("deploy")
        docs = [
            _tokenize("deploy the application to production"),
            _tokenize("fix the database connection bug"),
        ]
        scores = _bm25_score(query, docs)
        self.assertGreater(scores[0], scores[1])

    def test_no_match(self):
        query = _tokenize("zzzzz")
        docs = [_tokenize("deploy application")]
        scores = _bm25_score(query, docs)
        self.assertEqual(scores[0], 0.0)

    def test_empty_docs(self):
        self.assertEqual(_bm25_score(_tokenize("query"), []), [])


class TestCosineSimularity(unittest.TestCase):

    def test_identical(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [1, 0]), 1.0)

    def test_orthogonal(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_empty(self):
        self.assertEqual(cosine_similarity([], []), 0.0)


class TestSkillRanker(unittest.IsolatedAsyncioTestCase):

    def _make_skill(self, name, description, content="# Content", abstract=""):
        return SkillRecord(
            skill_id=f"sk-{name}", name=name,
            description=description, content=content,
            category=SkillCategory.WORKFLOW,
            status=SkillStatus.ACTIVE,
            tenant_id="t", user_id="u",
            abstract=abstract or description,
        )

    async def test_rank_empty(self):
        ranker = SkillRanker()
        result = await ranker.rank("query", [])
        self.assertEqual(result, [])

    async def test_rank_single(self):
        ranker = SkillRanker()
        skill = self._make_skill("deploy", "Deploy workflow")
        result = await ranker.rank("deploy", [skill])
        self.assertEqual(len(result), 1)

    async def test_rank_prefers_matching_terms(self):
        """BM25 should rank skill with matching terms higher."""
        ranker = SkillRanker()  # No embedding adapter -> BM25 only
        deploy = self._make_skill("deploy-flow", "Standard deployment workflow",
                                   content="# Deploy\n1. Build\n2. Test\n3. Deploy to staging")
        debug = self._make_skill("debug-flow", "Debugging network issues",
                                  content="# Debug\n1. Check logs\n2. Trace packets")
        result = await ranker.rank("deploy staging", [debug, deploy])
        self.assertEqual(result[0].name, "deploy-flow")

    async def test_rank_respects_top_k(self):
        ranker = SkillRanker()
        skills = [self._make_skill(f"skill-{i}", f"Description {i}") for i in range(10)]
        result = await ranker.rank("description", skills, top_k=3)
        self.assertEqual(len(result), 3)


if __name__ == "__main__":
    unittest.main()
