# SPDX-License-Identifier: Apache-2.0
"""Qdrant-backed vector record store for opencortex."""

from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import AsyncQdrantClient, models

PointOffset = str | int | uuid.UUID | None


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


class VectorPage(BaseModel):
    """One Qdrant scroll page."""

    records: list[dict[str, Any]]
    next_offset: str | int | None = None

    model_config = ConfigDict(extra="forbid")


class QdrantVectorStore:
    """Small Qdrant boundary for write-path vector records."""

    dense_vector_name = "dense"
    sparse_vector_name = "sparse"
    indexing_threshold = 1000
    full_scan_threshold = 1000
    payload_indexes = {
        "tenant_id": models.PayloadSchemaType.KEYWORD,
        "user_id": models.PayloadSchemaType.KEYWORD,
        "project_id": models.PayloadSchemaType.KEYWORD,
        "source_tenant_id": models.PayloadSchemaType.KEYWORD,
        "source_user_id": models.PayloadSchemaType.KEYWORD,
        "session_id": models.PayloadSchemaType.KEYWORD,
        "context_type": models.PayloadSchemaType.KEYWORD,
        "category": models.PayloadSchemaType.KEYWORD,
        "retrieval_surface": models.PayloadSchemaType.KEYWORD,
        "scope": models.PayloadSchemaType.KEYWORD,
        "source_uri": models.PayloadSchemaType.KEYWORD,
        "parent_uri": models.PayloadSchemaType.KEYWORD,
        "meta.layer": models.PayloadSchemaType.KEYWORD,
        "meta.source_uri": models.PayloadSchemaType.KEYWORD,
        "meta.tree_uri": models.PayloadSchemaType.KEYWORD,
        "ttl_expires_at": models.PayloadSchemaType.DATETIME,
    }

    def __init__(
        self,
        *,
        path: str = "./data/qdrant",
        vector_size: int = 1024,
        url: str = "",
        api_key: str = "",
        timeout: float = 30.0,
    ) -> None:
        self.path = path
        self.url = url
        self.api_key = api_key
        self.timeout = timeout
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
        offset: PointOffset = None,
    ) -> list[dict[str, Any]]:
        """Return payloads that match a Qdrant filter."""
        page = await self.scroll(
            collection,
            filters,
            limit=limit,
            offset=offset,
        )
        return page.records

    async def scroll(
        self,
        collection: str,
        filters: models.Filter | None,
        *,
        limit: int = 100,
        offset: PointOffset = None,
        order_by: models.OrderBy | None = None,
    ) -> VectorPage:
        """Return one Qdrant scroll page and its next offset."""
        await self.ensure_collection(collection)
        client = await self.ensure_client()
        points, next_offset = await client.scroll(
            collection_name=collection,
            scroll_filter=filters,
            limit=limit,
            offset=offset,
            order_by=order_by,
            with_payload=True,
            with_vectors=False,
        )
        return VectorPage(
            records=[self.from_point(point) for point in points],
            next_offset=normalize_offset(next_offset),
        )

    async def count(
        self,
        collection: str,
        filters: models.Filter | None,
        *,
        exact: bool = True,
    ) -> int:
        """Return the number of records matching a Qdrant filter."""
        await self.ensure_collection(collection)
        client = await self.ensure_client()
        result = await client.count(
            collection_name=collection,
            count_filter=filters,
            exact=exact,
        )
        return int(result.count)

    async def facet(
        self,
        collection: str,
        key: str,
        filters: models.Filter | None,
        *,
        limit: int = 20,
        exact: bool = True,
    ) -> dict[str, int]:
        """Return facet counts for one indexed payload key."""
        await self.ensure_collection(collection)
        client = await self.ensure_client()
        result = await client.facet(
            collection_name=collection,
            key=key,
            facet_filter=filters,
            limit=limit,
            exact=exact,
        )
        return {str(hit.value): int(hit.count) for hit in result.hits}

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

    async def remove_by_uri(
        self,
        collection: str,
        uri: str,
        *,
        filters: models.Filter | None = None,
    ) -> bool:
        """Remove a URI tree and vector projections that point at it."""
        await self.ensure_collection(collection)
        client = await self.ensure_client()
        point_ids: list[str | int] = []
        offset = None
        while True:
            points, offset = await client.scroll(
                collection_name=collection,
                offset=offset,
                scroll_filter=filters,
                limit=512,
                with_payload=True,
                with_vectors=False,
            )
            point_ids.extend(
                point.id for point in points if payload_matches_uri(point, uri)
            )
            if offset is None:
                break
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
            self.client = AsyncQdrantClient(
                url=self.url,
                api_key=self.api_key or None,
                timeout=self.timeout,
            )
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
                optimizers_config=models.OptimizersConfigDiff(
                    indexing_threshold=self.indexing_threshold,
                ),
                hnsw_config=models.HnswConfigDiff(
                    full_scan_threshold=self.full_scan_threshold,
                ),
            )
        await self.reconcile_collection(collection)
        self.collections.add(collection)

    async def reconcile_collection(self, collection: str) -> None:
        """Apply expected optimizer settings and payload indexes."""
        client = await self.ensure_client()
        await client.update_collection(
            collection_name=collection,
            optimizers_config=models.OptimizersConfigDiff(
                indexing_threshold=self.indexing_threshold,
            ),
            hnsw_config=models.HnswConfigDiff(
                full_scan_threshold=self.full_scan_threshold,
            ),
        )
        info = await client.get_collection(collection)
        existing = set((info.payload_schema or {}).keys())
        for field_name, field_schema in self.payload_indexes.items():
            if field_name in existing:
                continue
            await client.create_payload_index(
                collection_name=collection,
                field_name=field_name,
                field_schema=field_schema,
            )

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


def payload_matches_uri(point: Any, uri: str) -> bool:
    """Return whether a point belongs to a URI subtree."""
    prefix = uri if uri.endswith("/") else f"{uri}/"
    payload = dict(point.payload or {})
    for key in ("uri", "source_uri", "parent_uri"):
        value = str(payload.get(key, "") or "")
        if value == uri or value.startswith(prefix):
            return True
    meta = dict(payload.get("meta") or {})
    for key in ("source_uri", "tree_uri"):
        value = str(meta.get(key, "") or "")
        if value == uri or value.startswith(prefix):
            return True
    return False


def normalize_offset(offset: Any) -> str | int | None:
    """Return a JSON-safe Qdrant scroll offset."""
    if offset is None:
        return None
    if isinstance(offset, int):
        return offset
    return str(offset)


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
