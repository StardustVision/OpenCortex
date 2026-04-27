# SPDX-License-Identifier: Apache-2.0
"""
Qdrant storage adapter for OpenCortex.

Implements StorageInterface using Qdrant's AsyncQdrantClient in embedded
(local path) mode — zero external process required.

Architecture:
    Orchestrator → StorageInterface → QdrantStorageAdapter → AsyncQdrantClient
"""

import hashlib
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from qdrant_client import AsyncQdrantClient, models

from opencortex.storage.qdrant.filter_translator import translate_filter
from opencortex.storage.storage_interface import (
    CollectionNotFoundError,
    StorageInterface,
)

logger = logging.getLogger(__name__)


def _tokenize_for_scoring(text: str) -> set:
    """Zero-dependency tokenizer for Chinese+English mixed text scoring."""
    text = (text or "").lower()
    # English words, paths, error codes (e.g. error-404, config.yaml)
    words = set(re.findall(r"[a-z0-9][a-z0-9_\-\.]*[a-z0-9]|[a-z0-9]", text))
    # Chinese characters (single char as unigram)
    chinese_chars = set(re.findall(r"[\u4e00-\u9fa5]", text))
    return words | chinese_chars


def _compute_text_score(query: str, abstract: str, overview: str, keywords: str = "") -> float:
    """Term-overlap scoring for lexical search results.

    Abstract matches weighted 2x, keywords 1.5x, overview 1x.
    """
    query_terms = _tokenize_for_scoring(query)
    if not query_terms:
        return 0.0
    abstract_terms = _tokenize_for_scoring(abstract)
    overview_terms = _tokenize_for_scoring(overview)
    keywords_terms = _tokenize_for_scoring(keywords)
    abstract_hits = len(query_terms & abstract_terms)
    overview_hits = len(query_terms & overview_terms)
    keywords_hits = len(query_terms & keywords_terms)
    return min(1.0, (abstract_hits * 2 + keywords_hits * 1.5 + overview_hits) / (len(query_terms) * 2))


class QdrantStorageAdapter(StorageInterface):
    """StorageInterface implementation backed by Qdrant (embedded local mode).

    Uses AsyncQdrantClient with a local path for zero-dependency vector storage.

    Args:
        path: Directory for Qdrant's embedded storage. Created if needed.
        embedding_dim: Default dense vector dimension (default: 1024).
    """

    # Named vector spaces
    _DENSE_NAME = "dense"
    _SPARSE_NAME = "sparse"
    _ORDERED_FILTER_FALLBACK_SCAN_LIMIT = 2048

    def __init__(self, path: str = "./.qdrant", embedding_dim: int = 1024, url: str = ""):
        self._path = path
        self._url = url
        self._dim = embedding_dim
        self._client: Optional[AsyncQdrantClient] = None
        self._sparse_collections: set = set()

    async def _ensure_client(self) -> AsyncQdrantClient:
        """Lazily initialize the Qdrant client."""
        if self._client is None:
            if self._url:
                self._client = AsyncQdrantClient(url=self._url)
                logger.info("[QdrantAdapter] Client initialized at %s", self._url)
            else:
                os.makedirs(self._path, exist_ok=True)
                self._client = AsyncQdrantClient(path=self._path)
                logger.info("[QdrantAdapter] Client initialized at %s", self._path)
        return self._client

    async def ensure_text_indexes(self) -> None:
        """Ensure full-text indexes exist on abstract/overview fields.

        Safe to call on existing collections — Qdrant create_payload_index
        is idempotent (skips if index already exists).
        """
        client = await self._ensure_client()
        collections = await client.get_collections()
        existing = {c.name for c in collections.collections}

        for coll_name in existing:
            for field in ("abstract", "overview", "keywords"):
                try:
                    await client.create_payload_index(
                        collection_name=coll_name,
                        field_name=field,
                        field_schema=models.TextIndexParams(
                            type=models.TextIndexType.TEXT,
                            tokenizer=models.TokenizerType.MULTILINGUAL,
                            min_token_len=2,
                            max_token_len=20,
                        ),
                    )
                except Exception as exc:
                    logger.debug(
                        "[QdrantAdapter] Text index %s.%s: %s",
                        coll_name, field, exc,
                    )
        logger.info("[QdrantAdapter] Text indexes ensured on %d collections", len(existing))

    # =========================================================================
    # Collection Management
    # =========================================================================

    async def create_collection(self, name: str, schema: Dict[str, Any]) -> bool:
        client = await self._ensure_client()
        # Check schema fields for sparse_vector
        has_sparse = any(
            f.get("FieldType") == "sparse_vector"
            for f in schema.get("Fields", [])
        )

        if await client.collection_exists(name):
            # Register sparse capability even for existing collections
            # (critical: otherwise hybrid search falls back to pure dense
            # after a server restart)
            if has_sparse:
                self._sparse_collections.add(name)
            # PERF-02 (REVIEW closure tracker): always run the scalar
            # index ensure loop, even for existing collections. Qdrant's
            # ``create_payload_index`` is idempotent, so this is safe to
            # run on every startup. The migration story for new schema-
            # declared indexes (e.g. ``meta.source_uri``) becomes "redeploy"
            # rather than "recreate the collection" or "manual operator
            # action".
            await self._ensure_scalar_indexes(name, schema)
            return False

        vector_dim = schema.get("Dimension", self._dim)
        # Also infer dim from vector field if present
        for f in schema.get("Fields", []):
            if f.get("FieldType") == "vector" and "Dim" in f:
                vector_dim = f["Dim"]

        vectors_config = {
            self._DENSE_NAME: models.VectorParams(
                size=vector_dim,
                distance=models.Distance.COSINE,
            ),
        }
        sparse_config = None
        if has_sparse:
            sparse_config = {
                self._SPARSE_NAME: models.SparseVectorParams(),
            }
            self._sparse_collections.add(name)

        await client.create_collection(
            collection_name=name,
            vectors_config=vectors_config,
            sparse_vectors_config=sparse_config,
        )

        # Create payload indices for scalar-indexed fields
        await self._ensure_scalar_indexes(name, schema)

        logger.info("[QdrantAdapter] Collection created: %s (dim=%d, sparse=%s)",
                     name, vector_dim, has_sparse)
        return True

    async def _ensure_scalar_indexes(
        self, name: str, schema: Dict[str, Any]
    ) -> None:
        """Idempotently create payload indices for every ScalarIndex field.

        Called from both the new-collection path and the existing-collection
        path of ``create_collection`` (PERF-02). Qdrant's
        ``create_payload_index`` is no-op when the index already exists;
        running this on every startup is safe and gives schema-declared
        new indexes a deploy-driven migration path without operator
        intervention.

        Failures per field are logged at info (REVIEW closure tracker
        CORR-PERF02-005 — operators need visibility into degraded filter
        paths even when the process keeps running) and don't abort: a
        missing index degrades the relevant filter to a full-scan but
        doesn't break correctness, and the next startup will retry.
        """
        client = await self._ensure_client()
        for field_name in schema.get("ScalarIndex", []):
            schema_type = self._infer_payload_type(schema, field_name)
            try:
                await client.create_payload_index(
                    collection_name=name,
                    field_name=field_name,
                    field_schema=schema_type,
                )
            except Exception as exc:
                logger.info(
                    "[QdrantAdapter] Index ensure for %s.%s failed: %s "
                    "(filter on this field will degrade to full-scan; "
                    "next startup will retry)",
                    name, field_name, exc,
                )

    async def drop_collection(self, name: str) -> bool:
        client = await self._ensure_client()
        if not await client.collection_exists(name):
            return False
        await client.delete_collection(name)
        self._sparse_collections.discard(name)
        return True

    async def collection_exists(self, name: str) -> bool:
        client = await self._ensure_client()
        return await client.collection_exists(name)

    async def list_collections(self) -> List[str]:
        client = await self._ensure_client()
        result = await client.get_collections()
        return [c.name for c in result.collections]

    async def get_collection_info(self, name: str) -> Optional[Dict[str, Any]]:
        client = await self._ensure_client()
        if not await client.collection_exists(name):
            return None
        info = await client.get_collection(name)
        return {
            "name": name,
            "vector_dim": self._dim,
            "count": info.points_count or 0,
            "status": str(info.status),
        }

    # =========================================================================
    # CRUD Operations — Single Record
    # =========================================================================

    async def insert(self, collection: str, data: Dict[str, Any]) -> str:
        await self._assert_collection(collection)
        point = self._to_point(dict(data))
        client = await self._ensure_client()
        await client.upsert(
            collection_name=collection,
            points=[point],
        )
        return str(point.id)

    async def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:
        await self._assert_collection(collection)
        client = await self._ensure_client()
        point_id = self._to_point_id(id)

        # Check existence
        existing = await client.retrieve(collection, [point_id])
        if not existing:
            return False

        data = dict(data)
        # If vector is being updated, use update_vectors
        vector = data.pop("vector", None)
        sparse_vector = data.pop("sparse_vector", None)
        # Remove id from payload update
        data.pop("id", None)

        if data:
            await client.set_payload(
                collection_name=collection,
                payload=data,
                points=[point_id],
            )

        if vector is not None:
            vectors = {self._DENSE_NAME: vector}
            if sparse_vector and collection in self._sparse_collections:
                vectors[self._SPARSE_NAME] = self._to_sparse_vector(sparse_vector)
            await client.update_vectors(
                collection_name=collection,
                points=[
                    models.PointVectors(
                        id=point_id,
                        vector=vectors,
                    )
                ],
            )

        return True

    async def upsert(self, collection: str, data: Dict[str, Any]) -> str:
        await self._assert_collection(collection)
        point = self._to_point(dict(data))
        client = await self._ensure_client()
        await client.upsert(
            collection_name=collection,
            points=[point],
        )
        return str(point.id)

    async def delete(self, collection: str, ids: List[str]) -> int:
        await self._assert_collection(collection)
        if not ids:
            return 0
        client = await self._ensure_client()
        point_ids = [self._to_point_id(i) for i in ids]
        await client.delete(
            collection_name=collection,
            points_selector=models.PointIdsList(points=point_ids),
        )
        return len(ids)

    async def get(self, collection: str, ids: List[str]) -> List[Dict[str, Any]]:
        await self._assert_collection(collection)
        if not ids:
            return []
        client = await self._ensure_client()
        point_ids = [self._to_point_id(i) for i in ids]
        points = await client.retrieve(
            collection_name=collection,
            ids=point_ids,
            with_payload=True,
            with_vectors=True,
        )
        return [self._from_point(p) for p in points]

    async def exists(self, collection: str, id: str) -> bool:
        await self._assert_collection(collection)
        client = await self._ensure_client()
        point_id = self._to_point_id(id)
        points = await client.retrieve(collection, [point_id])
        return len(points) > 0

    # =========================================================================
    # CRUD Operations — Batch
    # =========================================================================

    async def batch_insert(self, collection: str, data: List[Dict[str, Any]]) -> List[str]:
        await self._assert_collection(collection)
        if not data:
            return []
        points = [self._to_point(dict(d)) for d in data]
        client = await self._ensure_client()
        await client.upsert(collection_name=collection, points=points)
        return [str(p.id) for p in points]

    async def batch_upsert(self, collection: str, data: List[Dict[str, Any]]) -> List[str]:
        return await self.batch_insert(collection, data)

    async def batch_delete(self, collection: str, filter: Dict[str, Any]) -> int:
        await self._assert_collection(collection)
        client = await self._ensure_client()
        qdrant_filter = translate_filter(filter) if filter else None
        if not qdrant_filter:
            return 0

        # Count before delete
        count_before = (await client.count(
            collection_name=collection,
            count_filter=qdrant_filter,
        )).count

        await client.delete(
            collection_name=collection,
            points_selector=models.FilterSelector(filter=qdrant_filter),
        )
        return count_before

    async def remove_by_uri(self, collection: str, uri: str) -> int:
        """Delete the record at *uri* and every derived record beneath it.

        "Beneath" = URI equals ``uri`` exactly OR URI starts with ``uri + "/"``.
        Sibling URIs that merely share a tokenised substring with *uri* (e.g.
        ``{uri}AB``) must NOT be removed.

        Implementation: MatchText is used as a server-side *candidate* filter
        (cheap, tokenised), but every match is re-checked with literal
        ``startswith`` before deletion.  This is necessary because:

        * On embedded (local) Qdrant payload indexes are no-ops (the client
          logs ``Payload indexes have no effect``) so MatchText degenerates
          to tokenised substring matching — it would match ``{uri}AB``.
        * On a real Qdrant server with a TEXT index, MatchText requires all
          query tokens to be present but still does not enforce word order or
          exact string prefix, so collisions are possible for
          tokeniser-adjacent URIs.

        The client-side ``startswith`` guard removes both failure modes.
        """
        await self._assert_collection(collection)
        client = await self._ensure_client()

        # Narrow candidates server-side with MatchText (tokenised).
        candidate_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="uri",
                    match=models.MatchText(text=uri),
                )
            ]
        )

        # Fetch candidate points in pages and keep only true prefix matches.
        descendant_prefix = uri if uri.endswith("/") else uri + "/"
        stale_ids: List[Any] = []
        next_offset = None
        while True:
            page, next_offset = await client.scroll(
                collection_name=collection,
                scroll_filter=candidate_filter,
                limit=256,
                offset=next_offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in page:
                record_uri = (point.payload or {}).get("uri", "")
                if not isinstance(record_uri, str):
                    continue
                if record_uri == uri or record_uri.startswith(descendant_prefix):
                    stale_ids.append(point.id)
            if next_offset is None:
                break

        if not stale_ids:
            return 0

        await client.delete(
            collection_name=collection,
            points_selector=models.PointIdsList(points=stale_ids),
        )
        return len(stale_ids)

    # =========================================================================
    # Search Operations
    # =========================================================================

    async def search(
        self,
        collection: str,
        query_vector: Optional[List[float]] = None,
        sparse_query_vector: Optional[Dict[str, float]] = None,
        filter: Optional[Dict[str, Any]] = None,
        limit: int = 10,
        offset: int = 0,
        output_fields: Optional[List[str]] = None,
        with_vector: bool = False,
        text_query: str = "",
    ) -> List[Dict[str, Any]]:
        await self._assert_collection(collection)
        client = await self._ensure_client()
        qdrant_filter = translate_filter(filter) if filter else None

        has_sparse = (
            sparse_query_vector
            and collection in self._sparse_collections
        )

        if query_vector and has_sparse:
            # Hybrid search with RRF fusion
            sparse_vec = self._to_sparse_vector(sparse_query_vector)
            results = await client.query_points(
                collection_name=collection,
                prefetch=[
                    models.Prefetch(
                        query=query_vector,
                        using=self._DENSE_NAME,
                        limit=limit * 2 + offset,
                    ),
                    models.Prefetch(
                        query=sparse_vec,
                        using=self._SPARSE_NAME,
                        limit=limit * 2 + offset,
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                query_filter=qdrant_filter,
                limit=limit + offset,
                with_payload=True,
                with_vectors=with_vector,
            )
            points = results.points[offset:]
        elif query_vector:
            # Pure dense search
            results = await client.query_points(
                collection_name=collection,
                query=query_vector,
                using=self._DENSE_NAME,
                query_filter=qdrant_filter,
                limit=limit + offset,
                with_payload=True,
                with_vectors=with_vector,
            )
            points = results.points[offset:]
        elif has_sparse:
            # Pure sparse search
            sparse_vec = self._to_sparse_vector(sparse_query_vector)
            results = await client.query_points(
                collection_name=collection,
                query=sparse_vec,
                using=self._SPARSE_NAME,
                query_filter=qdrant_filter,
                limit=limit + offset,
                with_payload=True,
                with_vectors=with_vector,
            )
            points = results.points[offset:]
        else:
            if text_query:
                # Lexical fallback: MatchText on abstract OR overview OR keywords
                text_conditions = [
                    models.FieldCondition(
                        key="abstract",
                        match=models.MatchText(text=text_query),
                    ),
                    models.FieldCondition(
                        key="overview",
                        match=models.MatchText(text=text_query),
                    ),
                    models.FieldCondition(
                        key="keywords",
                        match=models.MatchText(text=text_query),
                    ),
                ]
                combined_filter = models.Filter(
                    must=[qdrant_filter] if qdrant_filter else [],
                    should=text_conditions,
                )
                oversample = (limit + offset) * 3
                points_list, _ = await client.scroll(
                    collection_name=collection,
                    scroll_filter=combined_filter,
                    limit=oversample,
                    with_payload=True,
                    with_vectors=with_vector,
                )
                # Score and rank by text overlap
                for p in points_list:
                    payload = p.payload or {}
                    payload["_text_score"] = _compute_text_score(
                        text_query,
                        payload.get("abstract", ""),
                        payload.get("overview", ""),
                        payload.get("keywords", ""),
                    )
                    p.payload = payload
                points_list.sort(
                    key=lambda p: (p.payload or {}).get("_text_score", 0),
                    reverse=True,
                )
                points = points_list[offset : offset + limit]
            else:
                # Pure scalar filter — use scroll
                points_list, _ = await client.scroll(
                    collection_name=collection,
                    scroll_filter=qdrant_filter,
                    limit=limit + offset,
                    with_payload=True,
                    with_vectors=with_vector,
                )
                points = points_list[offset:]

        return [self._from_scored_point(p) for p in points]

    async def search_lexical(
        self,
        collection: str,
        text_query: str,
        filter: Optional[Dict[str, Any]] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Standalone lexical search via MatchText on abstract/overview.

        Unlike the lexical fallback inside search() (which only triggers when
        query_vector is None), this method always executes and is designed to
        run in parallel with dense search for RRF fusion.

        Returns dicts with '_score' set from term-overlap scoring.
        """
        await self._assert_collection(collection)
        client = await self._ensure_client()
        qdrant_filter = translate_filter(filter) if filter else None

        text_conditions = [
            models.FieldCondition(
                key="abstract",
                match=models.MatchText(text=text_query),
            ),
            models.FieldCondition(
                key="overview",
                match=models.MatchText(text=text_query),
            ),
            models.FieldCondition(
                key="keywords",
                match=models.MatchText(text=text_query),
            ),
        ]
        combined_filter = models.Filter(
            must=[qdrant_filter] if qdrant_filter else [],
            should=text_conditions,
        )
        oversample = limit * 3
        points_list, _ = await client.scroll(
            collection_name=collection,
            scroll_filter=combined_filter,
            limit=oversample,
            with_payload=True,
            with_vectors=False,
        )

        results = []
        for p in points_list:
            record = self._from_point(p)
            record["_score"] = _compute_text_score(
                text_query,
                record.get("abstract", ""),
                record.get("overview", ""),
                record.get("keywords", ""),
            )
            results.append(record)

        results.sort(key=lambda r: r.get("_score", 0.0), reverse=True)
        return results[:limit]

    async def filter(
        self,
        collection: str,
        filter: Dict[str, Any],
        limit: int = 10,
        offset: int = 0,
        output_fields: Optional[List[str]] = None,
        order_by: Optional[str] = None,
        order_desc: bool = False,
    ) -> List[Dict[str, Any]]:
        await self._assert_collection(collection)
        client = await self._ensure_client()
        qdrant_filter = translate_filter(filter) if filter else None

        if order_by:
            points = await self._ordered_filter_points(
                client=client,
                collection=collection,
                qdrant_filter=qdrant_filter,
                filter_dsl=filter,
                order_by=order_by,
                order_desc=order_desc,
                limit=limit,
                offset=offset,
            )
        else:
            points, _ = await client.scroll(
                collection_name=collection,
                scroll_filter=qdrant_filter,
                limit=limit + offset,
                with_payload=True,
            )
            points = points[offset:]

        return [self._from_point(p) for p in points]

    async def _ordered_filter_points(
        self,
        *,
        client: AsyncQdrantClient,
        collection: str,
        qdrant_filter: Optional[models.Filter],
        filter_dsl: Optional[Dict[str, Any]],
        order_by: str,
        order_desc: bool,
        limit: int,
        offset: int,
    ) -> List[Any]:
        """Return ordered filter results without draining the whole collection."""
        requested = max(limit + offset, 0)
        if requested <= 0:
            return []

        direction = models.Direction.DESC if order_desc else models.Direction.ASC
        try:
            ordered_points, _ = await client.scroll(
                collection_name=collection,
                scroll_filter=qdrant_filter,
                limit=requested,
                order_by=models.OrderBy(key=order_by, direction=direction),
                with_payload=True,
            )
        except Exception as exc:
            logger.info(
                "[QdrantAdapter] Native order_by failed for %s.%s: %s; "
                "falling back to bounded scan",
                collection,
                order_by,
                exc,
            )
            return await self._bounded_ordered_filter_points(
                client=client,
                collection=collection,
                qdrant_filter=qdrant_filter,
                order_by=order_by,
                order_desc=order_desc,
                limit=limit,
                offset=offset,
            )

        # Qdrant order_by returns records that have the ordered field. Preserve
        # previous adapter behavior for missing fields by treating them as "".
        if order_desc:
            if len(ordered_points) >= requested:
                return ordered_points[offset:requested]
            missing = await self._missing_order_field_points(
                client=client,
                collection=collection,
                filter_dsl=filter_dsl,
                order_by=order_by,
                limit=requested - len(ordered_points),
            )
            return (ordered_points + missing)[offset:requested]

        missing = await self._missing_order_field_points(
            client=client,
            collection=collection,
            filter_dsl=filter_dsl,
            order_by=order_by,
            limit=requested,
        )
        if len(missing) >= requested:
            return missing[offset:requested]
        return (missing + ordered_points)[offset:requested]

    async def _missing_order_field_points(
        self,
        *,
        client: AsyncQdrantClient,
        collection: str,
        filter_dsl: Optional[Dict[str, Any]],
        order_by: str,
        limit: int,
    ) -> List[Any]:
        """Fetch records missing the order field, bounded by caller page needs."""
        if limit <= 0:
            return []
        missing_filter_dsl = self._and_filter(
            filter_dsl,
            {"op": "is_null", "field": order_by},
        )
        points, _ = await client.scroll(
            collection_name=collection,
            scroll_filter=translate_filter(missing_filter_dsl),
            limit=limit,
            with_payload=True,
        )
        return points

    async def _bounded_ordered_filter_points(
        self,
        *,
        client: AsyncQdrantClient,
        collection: str,
        qdrant_filter: Optional[models.Filter],
        order_by: str,
        order_desc: bool,
        limit: int,
        offset: int,
    ) -> List[Any]:
        """Compatibility fallback for clients without native ordered scroll."""
        requested = max(limit + offset, 0)
        scan_limit = max(
            requested,
            min(
                self._ORDERED_FILTER_FALLBACK_SCAN_LIMIT,
                max(requested * 4, 128),
            ),
        )
        points, _ = await client.scroll(
            collection_name=collection,
            scroll_filter=qdrant_filter,
            limit=scan_limit,
            with_payload=True,
        )
        points.sort(
            key=lambda point: (point.payload or {}).get(order_by, ""),
            reverse=order_desc,
        )
        if len(points) >= self._ORDERED_FILTER_FALLBACK_SCAN_LIMIT:
            logger.info(
                "[QdrantAdapter] Ordered filter fallback reached scan limit=%d "
                "for %s.%s; results may be truncated",
                self._ORDERED_FILTER_FALLBACK_SCAN_LIMIT,
                collection,
                order_by,
            )
        return points[offset:requested]

    @staticmethod
    def _and_filter(
        left: Optional[Dict[str, Any]],
        right: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Join two filter DSL clauses with AND, ignoring an empty left side."""
        if not left:
            return right
        return {"op": "and", "conds": [left, right]}

    async def scroll(
        self,
        collection: str,
        filter: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
        output_fields: Optional[List[str]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        await self._assert_collection(collection)
        client = await self._ensure_client()
        qdrant_filter = translate_filter(filter) if filter else None

        offset_id = self._to_point_id(cursor) if cursor else None

        points, next_offset = await client.scroll(
            collection_name=collection,
            scroll_filter=qdrant_filter,
            limit=limit,
            offset=offset_id,
            with_payload=True,
        )

        records = [self._from_point(p) for p in points]
        if next_offset is None:
            next_cursor = None
        elif hasattr(next_offset, "id"):
            next_cursor = str(next_offset.id)
        else:
            next_cursor = str(next_offset)
        return records, next_cursor

    # =========================================================================
    # Aggregation
    # =========================================================================

    async def count(self, collection: str, filter: Optional[Dict[str, Any]] = None) -> int:
        await self._assert_collection(collection)
        client = await self._ensure_client()
        qdrant_filter = translate_filter(filter) if filter else None
        result = await client.count(
            collection_name=collection,
            count_filter=qdrant_filter,
        )
        return result.count

    # =========================================================================
    # Index Operations
    # =========================================================================

    async def create_index(
        self,
        collection: str,
        field: str,
        index_type: str,
        **kwargs,
    ) -> bool:
        await self._assert_collection(collection)
        client = await self._ensure_client()
        schema_type = self._index_type_to_schema(index_type)
        try:
            await client.create_payload_index(
                collection_name=collection,
                field_name=field,
                field_schema=schema_type,
            )
            return True
        except Exception as exc:
            logger.warning("[QdrantAdapter] create_index failed: %s", exc)
            return False

    async def drop_index(self, collection: str, field: str) -> bool:
        await self._assert_collection(collection)
        client = await self._ensure_client()
        try:
            await client.delete_payload_index(
                collection_name=collection,
                field_name=field,
            )
            return True
        except Exception as exc:
            logger.warning("[QdrantAdapter] drop_index failed: %s", exc)
            return False

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def clear(self, collection: str) -> bool:
        await self._assert_collection(collection)
        client = await self._ensure_client()
        # Get collection info to recreate with same config
        info = await client.get_collection(collection)
        await client.delete_collection(collection)
        await client.create_collection(
            collection_name=collection,
            vectors_config=info.config.params.vectors,
            sparse_vectors_config=info.config.params.sparse_vectors,
        )
        return True

    async def optimize(self, collection: str) -> bool:
        # Qdrant handles optimization automatically
        return True

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            logger.info("[QdrantAdapter] Client closed")

    async def health_check(self) -> bool:
        try:
            await self._ensure_client()
            return True
        except Exception:
            return False

    async def get_stats(self) -> Dict[str, Any]:
        client = await self._ensure_client()
        collections = await client.get_collections()
        total_records = 0
        for col in collections.collections:
            info = await client.get_collection(col.name)
            total_records += info.points_count or 0
        return {
            "collections": len(collections.collections),
            "total_records": total_records,
            "storage_size": 0,
            "backend": "qdrant",
        }

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    async def _assert_collection(self, name: str) -> None:
        """Raise CollectionNotFoundError if collection doesn't exist."""
        client = await self._ensure_client()
        if not await client.collection_exists(name):
            raise CollectionNotFoundError(f"Collection '{name}' does not exist")

    def _to_point(self, data: Dict[str, Any]) -> models.PointStruct:
        """Convert a StorageInterface data dict to a Qdrant PointStruct."""
        # Extract and normalize ID
        raw_id = data.pop("id", None)
        if raw_id is None:
            from opencortex.utils.id_generator import generate_id
            raw_id = generate_id()
        point_id = self._to_point_id(raw_id)

        # Extract vectors
        dense_vector = data.pop("vector", None)
        sparse_dict = data.pop("sparse_vector", None)

        vectors: Dict[str, Any] = {}
        if dense_vector:
            vectors[self._DENSE_NAME] = dense_vector
        if sparse_dict and isinstance(sparse_dict, dict):
            vectors[self._SPARSE_NAME] = self._to_sparse_vector(sparse_dict)

        # Everything else is payload
        # Store original string ID in payload for retrieval
        data["id"] = raw_id

        return models.PointStruct(
            id=point_id,
            vector=vectors if vectors else {self._DENSE_NAME: [0.0] * self._dim},
            payload=data,
        )

    def _from_point(self, point) -> Dict[str, Any]:
        """Convert a Qdrant point (Record or ScoredPoint) to a flat dict."""
        payload = dict(point.payload) if point.payload else {}
        # Restore id from payload (original string ID)
        if "id" not in payload:
            payload["id"] = str(point.id)
        # Extract vectors if present
        if hasattr(point, "vector") and point.vector:
            if isinstance(point.vector, dict):
                if self._DENSE_NAME in point.vector:
                    payload["vector"] = point.vector[self._DENSE_NAME]
            elif isinstance(point.vector, list):
                payload["vector"] = point.vector
        return payload

    def _from_scored_point(self, point) -> Dict[str, Any]:
        """Convert a ScoredPoint to a flat dict with _score."""
        result = self._from_point(point)
        if hasattr(point, "score") and point.score is not None:
            result["_score"] = point.score
        return result

    @staticmethod
    def _to_point_id(raw_id) -> int | str:
        """Convert raw ID to a Qdrant-compatible point ID.

        Accepts:
          - int (Snowflake) -> use directly
          - valid UUID string -> use as UUID
          - other string -> derive UUID5
        """
        if isinstance(raw_id, int):
            return raw_id
        try:
            return str(uuid.UUID(str(raw_id)))
        except (ValueError, AttributeError):
            return str(uuid.uuid5(uuid.NAMESPACE_URL, str(raw_id)))

    @staticmethod
    def _to_sparse_vector(sparse_dict: Dict[str, float]) -> models.SparseVector:
        """Convert a {term: weight} dict to a Qdrant SparseVector."""
        indices = []
        values = []
        for key, val in sparse_dict.items():
            # Deterministic hash — immune to PYTHONHASHSEED randomisation
            idx = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % (2**31)
            indices.append(idx)
            values.append(float(val))
        return models.SparseVector(indices=indices, values=values)

    @staticmethod
    def _infer_payload_type(
        schema: Dict[str, Any], field_name: str,
    ) -> models.PayloadSchemaType:
        """Infer Qdrant payload schema type from field definition."""
        for f in schema.get("Fields", []):
            if f.get("FieldName") == field_name:
                ft = f.get("FieldType", "")
                if ft in ("string", "path"):
                    return models.PayloadSchemaType.TEXT
                elif ft in ("int64", "integer"):
                    return models.PayloadSchemaType.INTEGER
                elif ft in ("float", "double"):
                    return models.PayloadSchemaType.FLOAT
                elif ft == "bool":
                    return models.PayloadSchemaType.BOOL
                elif ft == "date_time":
                    return models.PayloadSchemaType.DATETIME
        return models.PayloadSchemaType.KEYWORD

    # =========================================================================
    # Reinforcement Learning
    # =========================================================================

    async def update_reward(self, collection: str, id: str, reward: float) -> None:
        """Accumulate a reward signal on a record's payload."""
        client = await self._ensure_client()
        point_id = self._to_point_id(id)
        existing = await client.retrieve(collection, [point_id])
        if not existing:
            return
        payload = existing[0].payload or {}
        new_reward = payload.get("reward_score", 0.0) + reward
        pos = payload.get("positive_feedback_count", 0)
        neg = payload.get("negative_feedback_count", 0)
        if reward > 0:
            pos += 1
        elif reward < 0:
            neg += 1
        await client.set_payload(
            collection_name=collection,
            payload={
                "reward_score": new_reward,
                "positive_feedback_count": pos,
                "negative_feedback_count": neg,
            },
            points=[point_id],
        )

    async def get_profile(self, collection: str, id: str):
        """Return a Profile dataclass for the given record, or None."""
        from opencortex.storage.qdrant.reward_types import Profile

        client = await self._ensure_client()
        point_id = self._to_point_id(id)
        existing = await client.retrieve(collection, [point_id])
        if not existing:
            return None
        p = existing[0].payload or {}
        return Profile(
            id=id,
            reward_score=p.get("reward_score", 0.0),
            retrieval_count=p.get("active_count", 0),
            positive_feedback_count=p.get("positive_feedback_count", 0),
            negative_feedback_count=p.get("negative_feedback_count", 0),
            effective_score=p.get("reward_score", 0.0),
            is_protected=p.get("protected", False),
            accessed_at=p.get("accessed_at", ""),
        )

    async def set_protected(self, collection: str, id: str, protected: bool) -> None:
        """Mark a record as protected (slower decay)."""
        client = await self._ensure_client()
        point_id = self._to_point_id(id)
        await client.set_payload(
            collection_name=collection,
            payload={"protected": protected},
            points=[point_id],
        )

    async def apply_decay(
        self,
        decay_rate: float = 0.95,
        protected_rate: float = 0.99,
        threshold: float = 0.01,
    ):
        """Apply time-decay to reward_score across all collections.

        Batches set_payload calls per scroll page to avoid N individual
        update round-trips (each of which would also do a redundant retrieve).
        """
        import math
        from datetime import datetime, timezone
        from opencortex.storage.qdrant.reward_types import DecayResult

        now = datetime.now(timezone.utc)
        result = DecayResult()
        client = await self._ensure_client()
        collections = await client.get_collections()

        for coll in collections.collections:
            coll_name = coll.name
            cursor = None
            while True:
                points, cursor = await self.scroll(coll_name, limit=100, cursor=cursor)
                # Collect updates for this batch
                batch_updates: list = []  # (point_id, new_reward)
                for record in points:
                    result.records_processed += 1
                    reward = record.get("reward_score", 0.0)
                    if reward == 0.0:
                        continue
                    is_protected = record.get("protected", False)
                    rate = protected_rate if is_protected else decay_rate
                    # Access-driven protection: recent access → slower decay
                    accessed_at_str = record.get("accessed_at")
                    if accessed_at_str:
                        try:
                            accessed_dt = datetime.fromisoformat(
                                accessed_at_str.replace("Z", "+00:00")
                            )
                            days_since = max(0, (now - accessed_dt).days)
                            access_bonus = 0.04 * math.exp(-days_since / 30)
                            rate = min(1.0, rate + access_bonus)
                        except (ValueError, TypeError):
                            pass
                    new_reward = reward * rate
                    if abs(new_reward) < threshold:
                        new_reward = 0.0
                        result.records_below_threshold += 1
                    result.records_decayed += 1
                    record_id = record.get("id", "")
                    batch_updates.append((self._to_point_id(record_id), new_reward))

                # Flush batch — one set_payload per distinct reward value
                if batch_updates:
                    # Group by new_reward to minimise API calls
                    from collections import defaultdict
                    by_value: dict = defaultdict(list)
                    for pid, val in batch_updates:
                        by_value[val].append(pid)
                    for val, pids in by_value.items():
                        await client.set_payload(
                            collection_name=coll_name,
                            payload={"reward_score": val},
                            points=pids,
                        )

                if cursor is None:
                    break
        return result

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    @staticmethod
    def _index_type_to_schema(index_type: str) -> models.PayloadSchemaType:
        """Convert index type string to Qdrant schema type."""
        mapping = {
            "keyword": models.PayloadSchemaType.KEYWORD,
            "text": models.PayloadSchemaType.TEXT,
            "integer": models.PayloadSchemaType.INTEGER,
            "float": models.PayloadSchemaType.FLOAT,
            "bool": models.PayloadSchemaType.BOOL,
        }
        return mapping.get(index_type, models.PayloadSchemaType.KEYWORD)
