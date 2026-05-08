# SPDX-License-Identifier: Apache-2.0
"""Qdrant-backed vector record store for opencortex_app."""

from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import AsyncQdrantClient, models


class VectorRecord(BaseModel):
    """Record accepted by the Qdrant vector store."""

    id: str | int
    vector: list[float] | None = None
    sparse_vector: dict[str, float] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "VectorRecord":
        """Split a flat store payload into identity, vectors, and payload."""
        data = dict(payload)
        record_id = data.pop("id")
        dense_vector = data.pop("vector", None)
        sparse_vector = data.pop("sparse_vector", None)
        data["id"] = record_id
        return cls(
            id=record_id,
            vector=dense_vector,
            sparse_vector=sparse_vector if isinstance(sparse_vector, dict) else None,
            payload=data,
        )


class QdrantVectorStore:
    """Small Qdrant boundary for write-path vector records."""

    dense_vector_name = "dense"
    sparse_vector_name = "sparse"

    def __init__(
        self,
        *,
        path: str = "./data/qdrant",
        vector_size: int = 1024,
        url: str = "",
    ) -> None:
        self.path = path
        self.url = url
        self.vector_size = vector_size
        self.client: AsyncQdrantClient | None = None
        self.collections: set[str] = set()

    async def upsert(self, collection: str, record: dict[str, Any]) -> str:
        """Insert or replace one vector record."""
        await self.ensure_collection(collection)
        vector_record = VectorRecord.from_payload(record)
        client = await self.ensure_client()
        await client.upsert(
            collection_name=collection,
            points=[self.to_point(vector_record)],
        )
        return str(vector_record.id)

    async def filter(
        self,
        collection: str,
        filters: models.Filter | None,
        *,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        """Return payloads that match a Qdrant filter."""
        await self.ensure_collection(collection)
        client = await self.ensure_client()
        points, _ = await client.scroll(
            collection_name=collection,
            scroll_filter=filters,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [self.from_point(point) for point in points]

    async def search(
        self,
        collection: str,
        *,
        query_vector: list[float] | None,
        filters: models.Filter | None = None,
        limit: int = 10,
        score_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return scored payloads from dense vector search or filtered scroll."""
        await self.ensure_collection(collection)
        if query_vector is None:
            records = await self.filter(collection, filters, limit=limit)
            return [{**record, "_score": 0.0} for record in records]
        if len(query_vector) != self.vector_size:
            raise ValueError(
                "Vector dimension mismatch: "
                f"expected {self.vector_size}, got {len(query_vector)}"
            )
        client = await self.ensure_client()
        response = await client.query_points(
            collection_name=collection,
            query=query_vector,
            using=self.dense_vector_name,
            query_filter=filters,
            limit=limit,
            with_payload=True,
            with_vectors=False,
            score_threshold=score_threshold,
        )
        return [self.from_scored_point(point) for point in response.points]

    async def remove_by_uri(self, collection: str, uri: str) -> bool:
        """Remove the record at uri and derived records beneath that URI."""
        await self.ensure_collection(collection)
        client = await self.ensure_client()
        points, _ = await client.scroll(
            collection_name=collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="uri",
                        match=models.MatchText(text=uri),
                    )
                ]
            ),
            limit=1024,
            with_payload=True,
            with_vectors=False,
        )
        prefix = uri if uri.endswith("/") else f"{uri}/"
        point_ids = [
            point.id
            for point in points
            if payload_uri(point) == uri or payload_uri(point).startswith(prefix)
        ]
        if not point_ids:
            return False
        await client.delete(
            collection_name=collection,
            points_selector=models.PointIdsList(points=point_ids),
        )
        return True

    async def close(self) -> None:
        """Close the underlying Qdrant client."""
        if self.client is None:
            return
        await self.client.close()
        self.client = None
        self.collections.clear()

    async def ensure_client(self) -> AsyncQdrantClient:
        """Return a lazily initialized Qdrant client."""
        if self.client is not None:
            return self.client
        if self.url:
            self.client = AsyncQdrantClient(url=self.url)
        else:
            os.makedirs(self.path, exist_ok=True)
            self.client = AsyncQdrantClient(path=self.path)
        return self.client

    async def ensure_collection(self, collection: str) -> None:
        """Create the collection if it does not already exist."""
        if collection in self.collections:
            return
        client = await self.ensure_client()
        if not await client.collection_exists(collection):
            await client.create_collection(
                collection_name=collection,
                vectors_config={
                    self.dense_vector_name: models.VectorParams(
                        size=self.vector_size,
                        distance=models.Distance.COSINE,
                    )
                },
                sparse_vectors_config={
                    self.sparse_vector_name: models.SparseVectorParams(),
                },
            )
        self.collections.add(collection)

    def to_point(self, record: VectorRecord) -> models.PointStruct:
        """Convert a vector record to a Qdrant point."""
        vectors: dict[str, Any] = {}
        if record.vector is not None:
            if len(record.vector) != self.vector_size:
                raise ValueError(
                    "Vector dimension mismatch: "
                    f"expected {self.vector_size}, got {len(record.vector)}"
                )
            vectors[self.dense_vector_name] = record.vector
        if record.sparse_vector:
            vectors[self.sparse_vector_name] = to_sparse_vector(record.sparse_vector)
        return models.PointStruct(
            id=to_point_id(record.id),
            vector=vectors,
            payload=dict(record.payload),
        )

    @staticmethod
    def from_point(point: Any) -> dict[str, Any]:
        """Convert a Qdrant point back to the flat record payload."""
        payload = dict(point.payload or {})
        payload.setdefault("id", str(point.id))
        return payload

    @staticmethod
    def from_scored_point(point: Any) -> dict[str, Any]:
        """Convert a scored Qdrant point back to the flat record payload."""
        payload = QdrantVectorStore.from_point(point)
        payload["_score"] = float(getattr(point, "score", 0.0) or 0.0)
        return payload


def payload_uri(point: Any) -> str:
    """Return a point payload URI."""
    return str((point.payload or {}).get("uri", "") or "")


def to_point_id(raw_id: str | int) -> str | int:
    """Return a Qdrant-compatible point ID for a store record ID."""
    if isinstance(raw_id, int):
        return raw_id
    try:
        return str(uuid.UUID(str(raw_id)))
    except (AttributeError, ValueError):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, str(raw_id)))


def to_sparse_vector(sparse_vector: dict[str, float]) -> models.SparseVector:
    """Convert token-weight sparse vectors to Qdrant's sparse format."""
    indices = []
    values = []
    for key, value in sparse_vector.items():
        index = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % (2**31)
        indices.append(index)
        values.append(float(value))
    return models.SparseVector(indices=indices, values=values)
