# SPDX-License-Identifier: Apache-2.0
"""Primary record writer."""

from __future__ import annotations

import asyncio
from typing import Any

from opencortex.http.request_context import get_effective_project_id
from opencortex.retrieve.types import ContextType
from opencortex.store.schemas import PrimaryRecordInput, StoredRecord
from opencortex.store.types import MemoryCategory, SessionRecordLayer
from opencortex.utils.uri import CortexURI


class PrimaryRecordWriter:
    """Write fixed primary-record inputs to vector storage."""

    def __init__(
        self,
        *,
        config: Any,
        storage: Any,
        collection_resolver: Any,
        ttl_from_hours: Any,
    ) -> None:
        self.config = config
        self.storage = storage
        self.collection_resolver = collection_resolver
        self.ttl_from_hours = ttl_from_hours

    async def write(self, record_input: PrimaryRecordInput) -> StoredRecord:
        """Build and upsert the primary record."""
        record = self.build_record(record_input)
        upsert_started = asyncio.get_running_loop().time()
        await self.storage.upsert(self.collection_resolver(), record)
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

    def build_record(self, record_input: PrimaryRecordInput) -> dict[str, Any]:
        """Build the vector-store payload for a primary record."""
        ctx = record_input.ctx
        record = ctx.to_dict()
        if ctx.vector:
            record["vector"] = ctx.vector
        if record_input.sparse_vector:
            record["sparse_vector"] = record_input.sparse_vector

        record["scope"] = "private" if CortexURI(ctx.uri).is_private else "shared"
        record["category"] = record_input.effective_category
        record["source_user_id"] = record_input.user_id
        record["session_id"] = record_input.session_id or ""
        record["ttl_expires_at"] = self.ttl_for_store_record(record_input)
        record["project_id"] = (
            record_input.meta.get("project_id") or get_effective_project_id()
        )
        record["source_tenant_id"] = record_input.tenant_id
        record["keywords"] = record_input.keywords
        record["entities"] = record_input.entities
        record.update(record_input.object_payload)
        record["abstract_json"] = record_input.abstract_json
        self.populate_source_fields(record, record_input.meta)
        return record

    def ttl_for_store_record(self, record_input: PrimaryRecordInput) -> str:
        """Return TTL for short-lived primary record kinds."""
        if record_input.context_type == ContextType.STAGING:
            return self.ttl_from_hours(self.config.immediate_event_ttl_hours)
        if (
            record_input.context_type == ContextType.MEMORY
            and record_input.effective_category == str(MemoryCategory.EVENTS)
            and record_input.meta.get("layer") == str(SessionRecordLayer.IMMEDIATE)
        ):
            return self.ttl_from_hours(self.config.immediate_event_ttl_hours)
        if (
            record_input.context_type == ContextType.MEMORY
            and record_input.effective_category == str(MemoryCategory.EVENTS)
            and record_input.meta.get("layer") == str(SessionRecordLayer.MERGED)
        ):
            return self.ttl_from_hours(self.config.merged_event_ttl_hours)
        return ""

    @staticmethod
    def populate_source_fields(
        record: dict[str, Any],
        meta: dict[str, Any],
    ) -> None:
        """Copy enrichment fields to top-level payload fields."""
        record["source_doc_id"] = meta.get("source_doc_id", "")
        record["source_doc_title"] = meta.get("source_doc_title", "")
        record["source_section_path"] = meta.get("source_section_path", "")
        record["chunk_role"] = meta.get("chunk_role", "")
        record["speaker"] = meta.get("speaker", "")
        record["event_date"] = meta.get("event_date")
