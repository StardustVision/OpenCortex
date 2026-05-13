# SPDX-License-Identifier: Apache-2.0
"""Tests for the opencortex Qdrant vector store."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from qdrant_client import models

from opencortex.vector.qdrant_store import QdrantVectorStore


class TestQdrantVectorStore(unittest.IsolatedAsyncioTestCase):
    """Verify the standalone vector store boundary."""

    async def asyncSetUp(self) -> None:
        """Create a temporary embedded Qdrant store."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = QdrantVectorStore(path=self.temp_dir.name, vector_size=4)

    async def asyncTearDown(self) -> None:
        """Close and remove the temporary store."""
        await self.store.close()
        self.temp_dir.cleanup()

    async def test_upsert_filter_and_remove_by_uri(self) -> None:
        """Qdrant persists payloads and accepts native Qdrant filters."""
        await self.store.upsert(
            "context",
            {
                "id": "opencortex://tenant/user/memory/1",
                "uri": "opencortex://tenant/user/memory/1",
                "session_id": "session-1",
                "meta": {"layer": "merged"},
                "abstract": "Alice likes Python",
                "vector": [0.1, 0.2, 0.3, 0.4],
            },
        )
        await self.store.upsert(
            "context",
            {
                "id": "opencortex://tenant/user/memory/1/fact_indexes/a",
                "uri": "opencortex://tenant/user/memory/1/fact_indexes/a",
                "session_id": "session-1",
                "meta": {"layer": "merged"},
                "abstract": "Alice likes Python",
            },
        )
        await self.store.upsert(
            "context",
            {
                "id": "opencortex://tenant/user/memory/1/entity_indexes/a",
                "uri": "opencortex://tenant/user/memory/1/entity_indexes/a",
                "source_uri": "opencortex://tenant/user/memory/1",
                "abstract": "Alice",
            },
        )
        await self.store.upsert(
            "context",
            {
                "id": "opencortex://tenant/user/memory/10",
                "uri": "opencortex://tenant/user/memory/10",
                "abstract": "Sibling prefix must survive",
            },
        )
        payload_only = next(
            record
            for record in await self.store.filter("context", None)
            if record["uri"] == "opencortex://tenant/user/memory/1/fact_indexes/a"
        )
        self.assertNotIn("vector", payload_only)

        records = await self.store.filter(
            "context",
            models.Filter(
                must=[
                    models.FieldCondition(
                        key="meta.layer",
                        match=models.MatchValue(value="merged"),
                    )
                ]
            ),
        )

        self.assertEqual(len(records), 2)
        self.assertTrue(
            await self.store.remove_by_uri(
                "context", "opencortex://tenant/user/memory/1"
            )
        )
        self.assertEqual(
            [record["uri"] for record in await self.store.filter("context", None)],
            ["opencortex://tenant/user/memory/10"],
        )

    async def test_upsert_many_writes_multiple_records(self) -> None:
        """Batch upsert persists all vector records."""
        ids = await self.store.upsert_many(
            "context",
            [
                {
                    "id": "opencortex://tenant/user/memory/batch-1",
                    "uri": "opencortex://tenant/user/memory/batch-1",
                    "vector": [0.1, 0.2, 0.3, 0.4],
                },
                {
                    "id": "opencortex://tenant/user/memory/batch-2",
                    "uri": "opencortex://tenant/user/memory/batch-2",
                    "vector": [0.4, 0.3, 0.2, 0.1],
                },
            ],
        )

        self.assertEqual(
            ids,
            [
                "opencortex://tenant/user/memory/batch-1",
                "opencortex://tenant/user/memory/batch-2",
            ],
        )
        records = await self.store.filter("context", None)
        self.assertEqual(len(records), 2)

    async def test_scroll_count_facet_and_filtered_remove_by_uri(self) -> None:
        """Qdrant boundary supports paged console reads and scoped deletes."""
        for index, tenant in enumerate(["a", "a", "b"]):
            await self.store.upsert(
                "context",
                {
                    "id": f"opencortex://{tenant}/user/memory/{index}",
                    "uri": f"opencortex://{tenant}/user/memory/{index}",
                    "tenant_id": tenant,
                    "context_type": "memory",
                    "retrieval_surface": "l0_object",
                    "vector": [0.1, 0.2, 0.3, 0.4],
                },
            )

        tenant_a = models.Filter(
            must=[
                models.FieldCondition(
                    key="tenant_id",
                    match=models.MatchValue(value="a"),
                )
            ]
        )
        first_page = await self.store.scroll("context", tenant_a, limit=1)
        second_page = await self.store.scroll(
            "context",
            tenant_a,
            limit=2,
            offset=first_page.next_offset,
        )

        self.assertEqual(await self.store.count("context", tenant_a), 2)
        self.assertEqual(first_page.records[0]["tenant_id"], "a")
        self.assertEqual(len(second_page.records), 1)
        self.assertEqual(
            await self.store.facet("context", "tenant_id", None),
            {"a": 2, "b": 1},
        )
        self.assertFalse(
            await self.store.remove_by_uri(
                "context",
                "opencortex://b/user/memory/2",
                filters=tenant_a,
            )
        )
        self.assertEqual(await self.store.count("context", None), 3)

    async def test_rejects_wrong_vector_dimension(self) -> None:
        """QdrantVectorStore does not silently pad or truncate vectors."""
        with self.assertRaises(ValueError):
            await self.store.upsert(
                "context",
                {
                    "id": "opencortex://tenant/user/memory/bad-vector",
                    "uri": "opencortex://tenant/user/memory/bad-vector",
                    "vector": [0.1, 0.2],
                },
            )

    async def test_remote_client_uses_url_and_api_key(self) -> None:
        """A configured URL switches the store to remote Qdrant mode."""
        store = QdrantVectorStore(
            url="http://127.0.0.1:6333",
            api_key="secret",
            vector_size=4,
            timeout=60,
        )
        with patch("opencortex.vector.qdrant_store.AsyncQdrantClient") as client:
            result = await store.ensure_client()

        self.assertIs(result, client.return_value)
        client.assert_called_once_with(
            url="http://127.0.0.1:6333",
            api_key="secret",
            timeout=60,
        )

    async def test_payload_indexes_are_declared_for_recall_filters(self) -> None:
        """Recall-critical payload fields are part of the collection schema."""
        self.assertEqual(
            QdrantVectorStore.payload_indexes["retrieval_surface"],
            models.PayloadSchemaType.KEYWORD,
        )
        self.assertEqual(
            QdrantVectorStore.payload_indexes["meta.layer"],
            models.PayloadSchemaType.KEYWORD,
        )
        self.assertEqual(
            QdrantVectorStore.payload_indexes["event_ts"],
            models.PayloadSchemaType.DATETIME,
        )
        self.assertEqual(
            QdrantVectorStore.payload_indexes["section_index"],
            models.PayloadSchemaType.INTEGER,
        )
        self.assertLess(QdrantVectorStore.indexing_threshold, 10000)
