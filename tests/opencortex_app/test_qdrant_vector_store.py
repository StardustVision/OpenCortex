# SPDX-License-Identifier: Apache-2.0
"""Tests for the opencortex_app Qdrant vector store."""

from __future__ import annotations

import tempfile
import unittest

from qdrant_client import models

from opencortex_app.vector.qdrant_store import QdrantVectorStore


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
            await self.store.filter("context", None),
            [],
        )

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
