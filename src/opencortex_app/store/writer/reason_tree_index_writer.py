# SPDX-License-Identifier: Apache-2.0
"""Reason-tree retrieval projection writer."""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel

from opencortex_app.store.writer.event_payload import (
    digest,
    event_record_id,
    event_uri,
    primary_record,
)

logger = structlog.get_logger(__name__)


class ReasonTreeIndexRecord(BaseModel):
    """CFS tree projection used by reasoned recall."""

    id: str
    uri: str
    parent_uri: str
    source_uri: str
    source_record_id: str
    parent_source_uri: str = ""
    tree_uri: str = ""
    path: str = ""
    path_segments: list[str] = []
    level: int
    reason_role: str = "leaf"
    context_window: str = "self"
    source_uris: list[str] = []
    merged_uris: list[str] = []
    context_type: str = ""
    category: str = ""
    abstract: str = ""
    overview: str = ""
    content: str = ""
    is_leaf: bool = False
    retrieval_surface: str = "reason_tree_index"
    retrieval_ready: bool = True
    source_tenant_id: str = ""
    source_user_id: str = ""
    project_id: str = ""
    scope: str = ""
    session_id: str = ""
    entities: list[str] = []
    keywords: str = ""
    anchor_hits: list[str] = []
    memory_kind: str = ""
    cone_seed: bool = True
    cone_neighbors: list[str] = []
    meta: dict[str, Any] = {}


class ReasonTreeIndexWriter:
    """Write CFS tree retrieval projections into the vector store."""

    def __init__(
        self,
        *,
        vector_store: Any,
        collection_resolver: Any,
        namespace: Any,
        embedder: Any = None,
    ) -> None:
        self.vector_store = vector_store
        self.collection_resolver = collection_resolver
        self.namespace = namespace
        self.embedder = embedder

    async def write(self, event: Any) -> None:
        """Write one reason-tree index record for a ready primary record."""
        record = primary_record(event)
        if not record or not bool(record.get("retrieval_ready", False)):
            return
        index_record = self.index_record(event, record)
        payload = index_record.model_dump()
        self.embed_record(payload)
        await self.vector_store.upsert(self.collection_resolver(), payload)

    def index_record(
        self,
        event: Any,
        record: dict[str, Any],
    ) -> ReasonTreeIndexRecord:
        """Build a reason-tree projection payload."""
        source_uri = event_uri(event)
        uri = f"{source_uri}/reason_tree_indexes/{digest(source_uri)}"
        parent_uri = str(record.get("parent_uri", "") or "")
        source_uris = list((record.get("meta") or {}).get("source_uris") or [])
        merged_uris = list((record.get("meta") or {}).get("merged_uris") or [])
        path_segments = self.namespace.segments(source_uri)
        tree_uri = self.tree_uri(record, source_uri, parent_uri)
        reason_role = self.reason_role(record, source_uri)
        context_window = self.context_window(
            reason_role,
            bool(record.get("is_leaf", False)),
        )
        cone_neighbors = self.cone_neighbors(
            parent_uri=parent_uri,
            source_uris=source_uris,
            merged_uris=merged_uris,
        )
        meta = dict(record.get("meta") or {})
        meta.update(
            {
                "index_name": "ReasonTreeIndex",
                "source_uri": source_uri,
                "source_record_id": event_record_id(event),
                "tree_uri": tree_uri,
                "reason_role": reason_role,
                "context_window": context_window,
            }
        )
        return ReasonTreeIndexRecord(
            id=uri,
            uri=uri,
            parent_uri=parent_uri,
            source_uri=source_uri,
            source_record_id=event_record_id(event),
            parent_source_uri=parent_uri,
            tree_uri=tree_uri,
            path="/".join(path_segments),
            path_segments=path_segments,
            level=max(len(path_segments), 1),
            reason_role=reason_role,
            context_window=context_window,
            source_uris=source_uris,
            merged_uris=merged_uris,
            context_type=str(record.get("context_type", "") or ""),
            category=str(record.get("category", "") or ""),
            abstract=str(record.get("abstract", "") or ""),
            overview=str(record.get("overview", "") or ""),
            is_leaf=bool(record.get("is_leaf", False)),
            source_tenant_id=str(record.get("source_tenant_id", event.tenant_id) or ""),
            source_user_id=str(record.get("source_user_id", event.user_id) or ""),
            project_id=str(record.get("project_id", event.project_id) or ""),
            scope=str(record.get("scope", "") or ""),
            session_id=str(
                record.get("session_id", getattr(event, "session_id", "")) or ""
            ),
            entities=list(record.get("entities") or []),
            keywords=str(record.get("keywords", "") or ""),
            anchor_hits=list(record.get("anchor_hits") or []),
            memory_kind=str(record.get("memory_kind", "") or ""),
            cone_neighbors=cone_neighbors,
            meta=meta,
        )

    def tree_uri(self, record: dict[str, Any], source_uri: str, parent_uri: str) -> str:
        """Return the root URI for reason-tree expansion."""
        meta = dict(record.get("meta") or {})
        if meta.get("source_uri"):
            return str(meta["source_uri"])
        if meta.get("layer"):
            return parent_uri
        if meta.get("source_doc_id"):
            return parent_uri
        if record.get("session_id"):
            return parent_uri
        parent_chain = self.namespace.parent_chain(parent_uri) if parent_uri else []
        return parent_chain[0] if parent_chain else source_uri

    @staticmethod
    def reason_role(record: dict[str, Any], source_uri: str) -> str:
        """Return the reason-tree role for one primary record."""
        layer = str((record.get("meta") or {}).get("layer", "") or "")
        if layer == "immediate":
            return "session_immediate"
        if layer == "merged":
            return "session_segment"
        if layer == "session_final":
            return "session_final"
        if bool(record.get("is_leaf", False)):
            return "leaf"
        if source_uri.endswith("/final"):
            return "session_final"
        return "section"

    @staticmethod
    def context_window(reason_role: str, is_leaf: bool) -> str:
        """Return the recommended hydration window for recall expansion."""
        if reason_role == "session_final":
            return "children"
        if reason_role == "session_segment":
            return "parent_siblings"
        if is_leaf:
            return "self_parent"
        return "children"

    @staticmethod
    def cone_neighbors(
        *,
        parent_uri: str,
        source_uris: list[str],
        merged_uris: list[str],
    ) -> list[str]:
        """Return URI neighbors useful for recall-time cone diffusion."""
        neighbors: list[str] = []
        for uri in [parent_uri, *source_uris, *merged_uris]:
            text = str(uri or "").strip()
            if text and text not in neighbors:
                neighbors.append(text)
        return neighbors

    def embed_record(self, record: dict[str, Any]) -> None:
        """Attach required vector to a reason-tree index record."""
        if self.embedder is None:
            raise RuntimeError("ReasonTreeIndexWriter requires an embedder")
        text = str(record.get("overview") or record.get("abstract") or "")
        result = self.embedder.embed(text)
        if getattr(result, "dense_vector", None):
            record["vector"] = result.dense_vector
        else:
            raise ValueError("Reason tree embedding returned no dense vector")
        if getattr(result, "sparse_vector", None):
            record["sparse_vector"] = result.sparse_vector
