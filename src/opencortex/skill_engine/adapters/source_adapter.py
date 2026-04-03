"""Source adapter — reads from OpenCortex memory store (read-only)."""

import asyncio
import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from opencortex.utils.similarity import cosine_similarity

logger = logging.getLogger(__name__)

CONTEXT_COLLECTION = "context"
DEFAULT_SIMILARITY_THRESHOLD = 0.75
DEFAULT_SCAN_LIMIT = 500


@dataclass
class MemoryCluster:
    cluster_id: str
    theme: str
    memory_ids: List[str]
    centroid_embedding: List[float]
    avg_score: float


@dataclass
class MemoryRecord:
    memory_id: str
    abstract: str
    overview: str
    content: str
    context_type: str
    category: str
    meta: Dict[str, Any] = field(default_factory=dict)


class SourceAdapter(Protocol):
    """Protocol for reading memories. Implementation bridges to Qdrant."""

    async def scan_memories(
        self, tenant_id: str, user_id: str,
        context_types: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        min_count: int = 3,
    ) -> List[MemoryCluster]: ...

    async def get_cluster_memories(
        self, cluster: MemoryCluster,
    ) -> List[MemoryRecord]: ...


class QdrantSourceAdapter:
    """Concrete SourceAdapter that reads from OpenCortex's context Qdrant collection."""

    def __init__(self, storage, embedder, collection_name: str = CONTEXT_COLLECTION):
        self._storage = storage
        self._embedder = embedder
        self._collection = collection_name

    async def scan_memories(
        self, tenant_id: str, user_id: str,
        context_types: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        min_count: int = 3,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        project_id: str = "public",
    ) -> List[MemoryCluster]:
        """Scan memories, cluster by embedding similarity, return clusters."""
        # Build tenant/user/scope filter (same pattern as orchestrator.py:1602-1665)
        conds = [
            {"op": "must", "field": "source_tenant_id", "conds": [tenant_id, ""]},
            {"op": "must", "field": "is_leaf", "conds": [True]},
        ]
        # Project isolation (same as orchestrator search)
        if project_id and project_id != "public":
            conds.append({"op": "must", "field": "project_id", "conds": [project_id, "public"]})
        # Scope: shared + user's private
        scope_filter = {"op": "or", "conds": [
            {"op": "must", "field": "scope", "conds": ["shared", ""]},
            {"op": "and", "conds": [
                {"op": "must", "field": "scope", "conds": ["private"]},
                {"op": "must", "field": "source_user_id", "conds": [user_id]},
            ]},
        ]}
        conds.append(scope_filter)

        if context_types:
            conds.append({"op": "must", "field": "context_type", "conds": context_types})
        if categories:
            conds.append({"op": "must", "field": "category", "conds": categories})

        # Exclude staging
        conds.append({"op": "must_not", "field": "context_type", "conds": ["staging"]})

        filter_expr = {"op": "and", "conds": conds}

        # Fetch memories (scroll returns payloads without vectors)
        records = await self._storage.filter(
            self._collection, filter_expr, limit=DEFAULT_SCAN_LIMIT,
        )

        if not records:
            return []

        # Group by context_type + category for clustering
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in records:
            key = f"{r.get('context_type', 'memory')}:{r.get('category', 'events')}"
            groups[key].append(r)

        # Cluster within each group by embedding similarity
        all_clusters = []
        for group_key, group_records in groups.items():
            clusters = await self._cluster_group(
                group_records, similarity_threshold,
            )
            for cluster_records in clusters:
                if len(cluster_records) < min_count:
                    continue
                memory_ids = [r.get("id", r.get("uri", "")) for r in cluster_records]
                # Compute centroid from first record's embedding
                centroid = await self._get_embedding(cluster_records[0])
                cluster_id = hashlib.sha256(
                    "|".join(sorted(memory_ids)).encode()
                ).hexdigest()[:12]
                all_clusters.append(MemoryCluster(
                    cluster_id=f"cl-{cluster_id}",
                    theme=group_key,
                    memory_ids=memory_ids,
                    centroid_embedding=centroid,
                    avg_score=sum(r.get("reward_score", 0) for r in cluster_records) / len(cluster_records),
                ))

        return all_clusters

    async def get_cluster_memories(
        self, cluster: MemoryCluster,
    ) -> List[MemoryRecord]:
        """Fetch full memory content for a cluster."""
        records = await self._storage.get(self._collection, cluster.memory_ids)
        return [
            MemoryRecord(
                memory_id=r.get("id", r.get("uri", "")),
                abstract=r.get("abstract", ""),
                overview=r.get("overview", ""),
                content=r.get("content", r.get("overview", "")),
                context_type=r.get("context_type", "memory"),
                category=r.get("category", ""),
                meta={k: v for k, v in r.items()
                      if k not in ("abstract", "overview", "content", "vector")},
            )
            for r in records
        ]

    async def _cluster_group(
        self, records: List[Dict[str, Any]],
        threshold: float,
    ) -> List[List[Dict[str, Any]]]:
        """Greedy cosine-similarity clustering (adapted from archivist.py)."""
        if len(records) <= 1:
            return [records] if records else []

        # Get embeddings for all records
        embeddings = []
        for r in records:
            emb = await self._get_embedding(r)
            embeddings.append(emb)

        assigned = [False] * len(records)
        clusters = []

        for i in range(len(records)):
            if assigned[i]:
                continue
            if not embeddings[i]:
                continue
            cluster = [records[i]]
            assigned[i] = True

            for j in range(i + 1, len(records)):
                if assigned[j]:
                    continue
                if not embeddings[j]:
                    continue
                sim = cosine_similarity(embeddings[i], embeddings[j])
                if sim >= threshold:
                    cluster.append(records[j])
                    assigned[j] = True

            clusters.append(cluster)

        # Unassigned records as singletons
        for i in range(len(records)):
            if not assigned[i]:
                clusters.append([records[i]])

        return clusters

    async def _get_embedding(self, record: Dict[str, Any]) -> List[float]:
        """Get embedding for a record. Embeds the abstract text."""
        text = record.get("abstract", "") or record.get("name", "") or ""
        if not text:
            return []
        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._embedder.embed, text),
                timeout=2.0,
            )
            return result.dense_vector
        except Exception:
            return []
