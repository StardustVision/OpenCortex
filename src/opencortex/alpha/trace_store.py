"""
Trace Store — persistent storage for traces via Qdrant + CortexFS.

Traces are stored with three-layer CortexFS architecture:
  L0 (abstract): one-line summary -> Qdrant payload (zero I/O search)
  L1 (overview): key steps summary -> CortexFS
  L2 (content):  full conversation -> CortexFS
"""

import orjson
import logging
import inspect
from typing import Any, Awaitable, Callable, Dict, List, Optional

from opencortex.alpha.types import Trace
from opencortex.http.request_context import get_effective_project_id

logger = logging.getLogger(__name__)


class TraceStore:
    def __init__(
        self,
        storage,       # StorageInterface
        embedder,      # EmbedderBase
        cortex_fs,     # CortexFS
        collection_name: str = "traces",
        embedding_dim: int = 1024,
        on_trace_saved: Optional[Callable[[Trace], Awaitable[None] | None]] = None,
    ):
        self._storage = storage
        self._embedder = embedder
        self._fs = cortex_fs
        self._collection = collection_name
        self._dim = embedding_dim
        self._on_trace_saved = on_trace_saved

    async def init(self) -> "TraceStore":
        """Ensure collection exists."""
        from opencortex.storage.collection_schemas import init_trace_collection
        await init_trace_collection(self._storage, self._collection, self._dim)
        return self

    async def save(self, trace: Trace) -> str:
        """Save trace to Qdrant + CortexFS."""
        embed_text = trace.abstract or trace.trace_id
        embed_result = self._embedder.embed(embed_text)
        vector = embed_result.dense_vector
        project_id = getattr(trace, "project_id", "") or get_effective_project_id()
        setattr(trace, "project_id", project_id)

        record = {
            "id": trace.trace_id,
            "trace_id": trace.trace_id,
            "session_id": trace.session_id,
            "tenant_id": trace.tenant_id,
            "user_id": trace.user_id,
            "source": trace.source,
            "source_version": trace.source_version or "",
            "task_type": trace.task_type or "",
            "outcome": trace.outcome.value if trace.outcome else "",
            "error_code": trace.error_code or "",
            "training_ready": trace.training_ready,
            "archivist_processed": False,
            "abstract": trace.abstract or "",
            "overview": trace.overview or "",
            "project_id": project_id,
            "vector": vector,
            "created_at": trace.created_at,
        }
        await self._storage.upsert(self._collection, record)

        # Write L0/L1/L2 to CortexFS
        if self._fs and trace.turns:
            l2_content = orjson.dumps(
                [t.to_dict() for t in trace.turns]
            ).decode()
            uri = f"opencortex://{trace.tenant_id}/{trace.user_id}/trace/{trace.trace_id}"
            await self._fs.write_context(
                uri,
                content=l2_content,
                abstract=trace.abstract or "",
                overview=trace.overview or "",
            )

        if self._on_trace_saved:
            try:
                callback_result = self._on_trace_saved(trace)
                if inspect.isawaitable(callback_result):
                    await callback_result
            except Exception as exc:
                logger.warning(
                    "[TraceStore] on_trace_saved callback failed for trace_id=%s: %s",
                    trace.trace_id,
                    exc,
                )

        return trace.trace_id

    async def get(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """Get trace metadata from Qdrant."""
        results = await self._storage.get(self._collection, [trace_id])
        return results[0] if results else None

    async def list_by_session(
        self, session_id: str, tenant_id: str, user_id: str,
    ) -> List[Dict[str, Any]]:
        """List traces for a session."""
        filter_expr = {"op": "and", "conds": [
            {"op": "must", "field": "session_id", "conds": [session_id]},
            {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
        ]}
        return await self._storage.filter(self._collection, filter_expr)

    async def search(
        self, query: str, tenant_id: str, user_id: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Vector search over traces."""
        embed_result = self._embedder.embed_query(query)
        filter_expr = {"op": "and", "conds": [
            {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
        ]}
        return await self._storage.search(
            collection=self._collection,
            query_vector=embed_result.dense_vector,
            filter=filter_expr,
            limit=limit,
        )

    async def count_new_traces(self, tenant_id: str) -> int:
        """Count traces not yet processed by Archivist (for trigger)."""
        filter_expr = {"op": "and", "conds": [
            {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
            {"op": "must", "field": "archivist_processed", "conds": [False]},
        ]}
        results = await self._storage.filter(self._collection, filter_expr)
        return len(results)

    async def list_unprocessed(
        self, tenant_id: str, limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """List traces not yet processed by Archivist."""
        filter_expr = {"op": "and", "conds": [
            {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
            {"op": "must", "field": "archivist_processed", "conds": [False]},
        ]}
        return await self._storage.filter(
            self._collection, filter_expr, limit=limit,
        )

    async def mark_processed(self, trace_ids: List[str]) -> int:
        """Mark traces as processed by Archivist."""
        count = 0
        for tid in trace_ids:
            existing = await self.get(tid)
            if existing:
                existing["archivist_processed"] = True
                await self._storage.upsert(self._collection, existing)
                count += 1
        return count
