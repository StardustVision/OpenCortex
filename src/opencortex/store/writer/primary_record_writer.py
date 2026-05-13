# SPDX-License-Identifier: Apache-2.0
"""Primary record writer."""

from __future__ import annotations

import asyncio
from typing import Any

from opencortex.store.schemas import PrimaryRecordInput, StoredRecord
from opencortex.store.types import ContextType
from opencortex.utils.uri import CortexURI
from opencortex.vector.payloads import (
    DirectoryPayload,
    PrimaryPayload,
    VectorPayloadSurface,
)


class PrimaryRecordWriter:
    """Write prepared primary-record payloads to vector storage."""

    def __init__(
        self,
        *,
        vector_store: Any,
        collection_resolver: Any,
        namespace: Any = None,
    ) -> None:
        self.vector_store = vector_store
        self.collection_resolver = collection_resolver
        self.namespace = namespace

    async def write(self, record_input: PrimaryRecordInput) -> StoredRecord:
        """Upsert one prepared primary record."""
        record = self.prepared_payload(record_input)
        collection = self.collection_resolver()
        await self.ensure_parent_records(collection, record)
        upsert_started = asyncio.get_running_loop().time()
        await self.vector_store.upsert(collection, record)
        upsert_ms = int((asyncio.get_running_loop().time() - upsert_started) * 1000)
        return StoredRecord(
            uri=record_input.ctx.uri,
            context_type=record_input.ctx.context_type,
            category=record["category"],
            abstract=record_input.ctx.abstract,
            overview=record_input.ctx.overview,
            meta=dict(record_input.ctx.meta),
            record=record,
            upsert_ms=upsert_ms,
        )

    @staticmethod
    def prepared_payload(record_input: PrimaryRecordInput) -> dict[str, Any]:
        """Return the payload prepared by the store flow."""
        record = dict(record_input.payload)
        if not record:
            raise ValueError("PrimaryRecordWriter requires a prepared payload")
        if not record.get("id") or not record.get("uri"):
            raise ValueError("PrimaryRecordWriter payload requires id and uri")
        if record.get("retrieval_surface") == str(VectorPayloadSurface.L0_OBJECT):
            vectors = {
                key: record.pop(key)
                for key in ("vector", "sparse_vector")
                if key in record
            }
            return {**PrimaryPayload.model_validate(record).to_record(), **vectors}
        return record

    async def ensure_parent_records(
        self,
        collection: str,
        record: dict[str, Any],
    ) -> None:
        """Ensure Qdrant has payload-only directory records for record ancestors."""
        parent_uri = str(record.get("parent_uri", "") or "")
        if not parent_uri or self.namespace is None:
            return
        for uri in self.namespace.parent_chain(parent_uri):
            await self.vector_store.upsert(collection, self.parent_record(uri, record))

    def parent_record(self, uri: str, source: dict[str, Any]) -> dict[str, Any]:
        """Build one payload-only Qdrant directory record."""
        parent_uri = self.namespace.parent(uri) if self.namespace is not None else ""
        tenant_id = str(
            source.get("tenant_id", "") or source.get("source_tenant_id", "")
        )
        user_id = str(source.get("user_id", "") or source.get("source_user_id", ""))
        project_id = str(source.get("project_id", "") or "")
        return DirectoryPayload(
            id=uri,
            uri=uri,
            parent_uri=parent_uri,
            context_type=str(source.get("context_type") or ContextType.MEMORY),
            category=str(source.get("category", "") or ""),
            scope="private" if CortexURI(uri).is_private else "shared",
            tenant_id=tenant_id,
            user_id=user_id,
            source_tenant_id=tenant_id,
            source_user_id=user_id,
            project_id=project_id,
            session_id="",
            meta={
                "record_kind": "directory",
                "project_id": project_id,
            },
            retrieval_surface=VectorPayloadSurface.DIRECTORY,
            section_index=source.get("section_index"),
        ).to_record()
