import unittest
from unittest.mock import AsyncMock, MagicMock
from opencortex.alpha.knowledge_store import KnowledgeStore
from opencortex.alpha.types import (
    Knowledge, KnowledgeType, KnowledgeStatus, KnowledgeScope,
)


class TestKnowledgeStore(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.storage = AsyncMock()
        self.storage.collection_exists = AsyncMock(return_value=True)
        self.embedder = MagicMock()
        self.embedder.embed = MagicMock(return_value=MagicMock(dense_vector=[0.1]*4))
        self.cortex_fs = AsyncMock()
        self.store = KnowledgeStore(
            storage=self.storage,
            embedder=self.embedder,
            cortex_fs=self.cortex_fs,
            collection_name="knowledge",
            embedding_dim=4,
        )

    async def test_save_belief(self):
        """Save a belief and retrieve it."""
        k = Knowledge(
            knowledge_id="k1",
            knowledge_type=KnowledgeType.BELIEF,
            tenant_id="team", user_id="hugo",
            scope=KnowledgeScope.USER,
            statement="Always check spelling",
            abstract="Check spelling before pip install",
        )
        await self.store.save(k)
        self.storage.upsert.assert_called_once()
        call_args = self.storage.upsert.call_args
        self.assertEqual(call_args[0][0], "knowledge")

    async def test_search_by_type(self):
        """Search filtered by knowledge_type."""
        self.storage.search = AsyncMock(return_value=[])
        result = await self.store.search(
            "import error", "team", "hugo",
            types=["belief", "sop"],
        )
        self.storage.search.assert_called_once()
        # Check the filter includes type constraint
        call_args = self.storage.search.call_args
        filter_expr = call_args[0][2]
        type_cond = [c for c in filter_expr["conditions"]
                     if c.get("field") == "knowledge_type"]
        self.assertEqual(len(type_cond), 1)

    async def test_approve_moves_to_active(self):
        """approve() transitions to active."""
        self.storage.get = AsyncMock(return_value=[{
            "knowledge_id": "k1",
            "status": "verified",
            "updated_at": "2026-01-01",
        }])
        result = await self.store.approve("k1")
        self.assertTrue(result)
        call_args = self.storage.upsert.call_args
        record = call_args[0][1]
        self.assertEqual(record["status"], "active")

    async def test_reject_deprecates(self):
        """reject() transitions to deprecated."""
        self.storage.get = AsyncMock(return_value=[{
            "knowledge_id": "k1",
            "status": "candidate",
            "updated_at": "2026-01-01",
        }])
        result = await self.store.reject("k1")
        self.assertTrue(result)
        call_args = self.storage.upsert.call_args
        record = call_args[0][1]
        self.assertEqual(record["status"], "deprecated")

    async def test_approve_nonexistent_returns_false(self):
        """approve() returns False for missing ID."""
        self.storage.get = AsyncMock(return_value=[])
        result = await self.store.approve("nonexistent")
        self.assertFalse(result)

    async def test_list_candidates(self):
        """list_candidates returns candidate + verified items."""
        self.storage.filter = AsyncMock(return_value=[
            {"knowledge_id": "k1", "status": "candidate"},
        ])
        result = await self.store.list_candidates("team")
        self.storage.filter.assert_called_once()
        self.assertEqual(len(result), 1)

    async def test_promote(self):
        """promote() changes scope."""
        self.storage.get = AsyncMock(return_value=[{
            "knowledge_id": "k1",
            "scope": "user",
            "updated_at": "2026-01-01",
        }])
        result = await self.store.promote("k1", "tenant")
        self.assertTrue(result)
        call_args = self.storage.upsert.call_args
        record = call_args[0][1]
        self.assertEqual(record["scope"], "tenant")


if __name__ == "__main__":
    unittest.main()
