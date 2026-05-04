# SPDX-License-Identifier: Apache-2.0
"""Tests for parent directory record persistence."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, MagicMock

from opencortex.http.request_context import reset_request_identity, set_request_identity
from opencortex.models.embedder.base import EmbedResult
from opencortex.services.memory_directory_record_service import (
    MemoryDirectoryRecordService,
)


class _SpyEmbedder:
    """Capture directory embed inputs and return a configured result."""

    def __init__(self, result: EmbedResult) -> None:
        self.result = result
        self.inputs: List[str] = []

    def embed(self, text: str) -> EmbedResult:
        self.inputs.append(text)
        return self.result


class TestMemoryDirectoryRecordService(unittest.IsolatedAsyncioTestCase):
    """Verify directory record extraction from MemoryWriteService."""

    def _build_service(
        self,
        *,
        filter_results: List[List[dict]],
        embedder: _SpyEmbedder | None = None,
    ) -> tuple[MemoryDirectoryRecordService, SimpleNamespace]:
        storage = SimpleNamespace(
            filter=AsyncMock(side_effect=filter_results),
            upsert=AsyncMock(),
        )
        write_service = SimpleNamespace(
            _storage=storage,
            _embedder=embedder,
            _get_collection=MagicMock(return_value="context"),
            _derive_parent_uri=MagicMock(
                side_effect=lambda uri: uri.rsplit("/", 1)[0] if "/" in uri else None
            ),
        )
        return MemoryDirectoryRecordService(write_service), write_service

    async def test_missing_ancestors_are_created_top_down(self) -> None:
        """Missing directory ancestors are upserted from root to leaf."""
        embedder = _SpyEmbedder(EmbedResult(dense_vector=[0.1, 0.2]))
        service, orch = self._build_service(
            filter_results=[[], [], [], []],
            embedder=embedder,
        )
        tokens = set_request_identity("tenant-1", "user-1")
        try:
            await service.ensure_parent_records(
                "opencortex://tenant-1/user-1/memories/preferences"
            )
        finally:
            reset_request_identity(tokens)

        created = [call.args[1]["uri"] for call in orch._storage.upsert.await_args_list]
        self.assertEqual(
            created,
            [
                "opencortex://tenant-1",
                "opencortex://tenant-1/user-1",
                "opencortex://tenant-1/user-1/memories",
                "opencortex://tenant-1/user-1/memories/preferences",
            ],
        )
        self.assertEqual(
            embedder.inputs,
            ["tenant-1", "user-1", "memories", "preferences"],
        )
        for call in orch._storage.upsert.await_args_list:
            record = call.args[1]
            self.assertEqual(record["source_tenant_id"], "tenant-1")
            self.assertEqual(record["source_user_id"], "user-1")
            self.assertFalse(record["mergeable"])
            self.assertEqual(record["session_id"], "")
            self.assertEqual(record["ttl_expires_at"], "")

    async def test_existing_directory_short_circuits_walk(self) -> None:
        """Traversal stops once an existing ancestor is found."""
        service, orch = self._build_service(
            filter_results=[
                [],
                [{"uri": "opencortex://tenant-1/user-1/memories"}],
            ],
        )
        tokens = set_request_identity("tenant-1", "user-1")
        try:
            await service.ensure_parent_records(
                "opencortex://tenant-1/user-1/memories/preferences"
            )
        finally:
            reset_request_identity(tokens)

        orch._storage.upsert.assert_awaited_once()
        record = orch._storage.upsert.await_args.args[1]
        self.assertEqual(
            record["uri"],
            "opencortex://tenant-1/user-1/memories/preferences",
        )

    async def test_sparse_vector_is_persisted_when_embedder_returns_one(self) -> None:
        """Hybrid directory embeddings keep the sparse vector on the record."""
        sparse_vector = {"indices": [2], "values": [0.7]}
        embedder = _SpyEmbedder(
            EmbedResult(dense_vector=[0.3, 0.4], sparse_vector=sparse_vector)
        )
        service, orch = self._build_service(filter_results=[[], []], embedder=embedder)
        tokens = set_request_identity("tenant-1", "user-1")
        try:
            await service.ensure_parent_records("opencortex://tenant-1/shared")
        finally:
            reset_request_identity(tokens)

        record = orch._storage.upsert.await_args.args[1]
        self.assertEqual(record["vector"], [0.3, 0.4])
        self.assertEqual(record["sparse_vector"], sparse_vector)
        self.assertEqual(record["scope"], "shared")

    async def test_no_embedder_still_creates_directory_record(self) -> None:
        """Directory records do not require an embedder to be persisted."""
        service, orch = self._build_service(filter_results=[[], []], embedder=None)
        tokens = set_request_identity("tenant-1", "user-1")
        try:
            await service.ensure_parent_records("opencortex://tenant-1/shared")
        finally:
            reset_request_identity(tokens)

        record = orch._storage.upsert.await_args.args[1]
        self.assertNotIn("sparse_vector", record)
        self.assertIsNone(record["vector"])
        self.assertEqual(record["scope"], "shared")
        self.assertEqual(record["source_tenant_id"], "tenant-1")
        self.assertEqual(record["source_user_id"], "user-1")

    async def test_invalid_parent_uri_does_not_create_records(self) -> None:
        """Invalid parent URIs preserve the legacy no-op behavior."""
        service, orch = self._build_service(filter_results=[], embedder=None)

        await service.ensure_parent_records("not-a-cortex-uri")

        orch._storage.filter.assert_not_awaited()
        orch._storage.upsert.assert_not_awaited()
