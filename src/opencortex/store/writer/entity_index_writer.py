# SPDX-License-Identifier: Apache-2.0
"""Writer for entity Qdrant retrieval projections."""

from __future__ import annotations

from typing import Any

from opencortex.store.event.events import MemoryEvent
from opencortex.store.writer.event_payload import (
    digest,
    event_record_id,
    event_uri,
    primary_record,
)
from opencortex.store.writer.search_index_writer import upsert_records
from opencortex.utils.facts import temporal_payload_fields
from opencortex.vector.payloads import EntityIndexPayload, VectorPayloadSurface


class EntityIndexWriter:
    """Write entity index records for primary-record entities."""

    def __init__(
        self,
        *,
        vector_store: Any,
        collection_resolver: Any,
        embedder: Any = None,
    ) -> None:
        self.vector_store = vector_store
        self.collection_resolver = collection_resolver
        self.embedder = embedder

    async def write(self, event: MemoryEvent) -> None:
        """Write Qdrant entity-index records from one primary record."""
        record = primary_record(event)
        if (
            not record
            or not bool(record.get("retrieval_ready", False))
            or not bool(record.get("is_leaf", False))
        ):
            return
        records = self.entity_records(event, record)
        if not records:
            return
        await self.embed_records(records)
        await upsert_records(self.vector_store, self.collection_resolver(), records)

    def entity_records(
        self,
        event: MemoryEvent,
        record: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Build entity index payloads for one primary record."""
        records: list[dict[str, Any]] = []
        for entity in self.normalized_entities(record.get("entities") or []):
            uri = f"{event_uri(event)}/entity_indexes/{digest(entity)}"
            meta = dict(record.get("meta") or {})
            meta.update(
                {
                    "index_name": "EntityIndex",
                    "source_uri": event_uri(event),
                    "source_record_id": event_record_id(event),
                    "entity_text": entity,
                }
            )
            records.append(
                EntityIndexPayload(
                    id=uri,
                    uri=uri,
                    parent_uri=event_uri(event),
                    context_type=record.get("context_type", ""),
                    category=record.get("category", ""),
                    abstract=entity,
                    overview=entity,
                    content=entity,
                    retrieval_surface=VectorPayloadSurface.ENTITY_INDEX,
                    source_uri=event_uri(event),
                    source_record_id=event_record_id(event),
                    source_tenant_id=event.tenant_id,
                    source_user_id=event.user_id,
                    tenant_id=event.tenant_id,
                    user_id=event.user_id,
                    project_id=event.project_id,
                    scope=record.get("scope", ""),
                    session_id=getattr(event, "session_id", ""),
                    entity_text=entity,
                    entities=[entity],
                    keywords=record.get("keywords", ""),
                    anchor_hits=record.get("anchor_hits", []),
                    memory_kind=record.get("memory_kind", ""),
                    **temporal_payload_fields(
                        record.get("event_ts"),
                        record.get("event_date"),
                        record.get("utterance_ts"),
                        record.get("date_range_start"),
                        record.get("date_range_end"),
                        record.get("time_refs"),
                        entity,
                    ),
                    meta=meta,
                ).to_record()
            )
        return records

    async def embed_records(self, records: list[dict[str, Any]]) -> None:
        """Attach required vectors to entity index records."""
        if self.embedder is None:
            raise RuntimeError("EntityIndexWriter requires an embedder")
        texts = [str(record.get("overview", "") or "") for record in records]
        if hasattr(self.embedder, "prefer_async") and hasattr(
            self.embedder, "aembed_batch"
        ):
            results = await self.embedder.aembed_batch(texts)
        else:
            import asyncio

            results = await asyncio.to_thread(self.embedder.embed_batch, texts)
        for record, result in zip(records, results, strict=False):
            if getattr(result, "dense_vector", None):
                record["vector"] = result.dense_vector
            else:
                raise ValueError("Entity index embedding returned no dense vector")
            if getattr(result, "sparse_vector", None):
                record["sparse_vector"] = result.sparse_vector

    @staticmethod
    def normalized_entities(entities: list[Any]) -> list[str]:
        """Return stable de-duplicated entity strings."""
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in entities:
            entity = str(raw or "").strip()
            key = entity.casefold()
            if not entity or key in seen:
                continue
            seen.add(key)
            normalized.append(entity)
        return normalized
