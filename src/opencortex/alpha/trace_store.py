"""
Trace Store — persistent storage for traces via Qdrant + CortexFS.

Traces are stored with three-layer CortexFS architecture:
  L0 (abstract): one-line summary -> Qdrant payload (zero I/O search)
  L1 (overview): key steps summary -> CortexFS
  L2 (content):  full conversation -> CortexFS
"""

import orjson
import logging
from typing import Any, Dict, List, Optional

from opencortex.alpha.types import Trace

logger = logging.getLogger(__name__)


class TraceStore:
    def __init__(
        self,
        storage,       # StorageInterface
        embedder,      # EmbedderBase
        cortex_fs,     # CortexFS
        collection_name: str = "traces",
        embedding_dim: int = 1024,
    ):
        self._storage = storage
        self._embedder = embedder
        self._fs = cortex_fs
        self._collection = collection_name
        self._dim = embedding_dim

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
            "abstract": trace.abstract or "",
            "overview": trace.overview or "",
            "vector": vector,
            "created_at": trace.created_at,
        }
        await self._storage.upsert(self._collection, record)

        # Write L0/L1/L2 to CortexFS
        if self._fs and trace.turns:
            l2_content = orjson.dumps(
                [t.to_dict() for t in trace.turns]
            ).decode()
            uri = f"opencortex://{trace.tenant_id}/user/{trace.user_id}/trace/{trace.trace_id}"
            await self._fs.write_context(
                uri,
                content=l2_content,
                abstract=trace.abstract or "",
                overview=trace.overview or "",
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
        filter_expr = {
            "op": "and",
            "conditions": [
                {"field": "session_id", "op": "=", "value": session_id},
                {"field": "tenant_id", "op": "=", "value": tenant_id},
            ],
        }
        return await self._storage.filter(self._collection, filter_expr)

    async def search(
        self, query: str, tenant_id: str, user_id: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Vector search over traces."""
        embed_result = self._embedder.embed(query)
        filter_expr = {
            "op": "and",
            "conditions": [
                {"field": "tenant_id", "op": "=", "value": tenant_id},
            ],
        }
        return await self._storage.search(
            self._collection, embed_result.dense_vector, filter_expr, limit=limit
        )

    async def count_new_traces(self, tenant_id: str) -> int:
        """Count traces not yet processed by Archivist (for trigger)."""
        filter_expr = {
            "field": "tenant_id", "op": "=", "value": tenant_id,
        }
        results = await self._storage.filter(self._collection, filter_expr)
        return len(results)
