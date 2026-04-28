# SPDX-License-Identifier: Apache-2.0
"""Store record assembly and persistence for memory writes."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opencortex.core.context import Context
from opencortex.http.request_context import get_effective_project_id
from opencortex.services.memory_signals import MemoryStoredSignal
from opencortex.utils.uri import CortexURI

if TYPE_CHECKING:
    from opencortex.services.memory_write_service import MemoryWriteService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredRecordResult:
    """Result of persisting a memory store record."""

    record: Dict[str, Any]
    upsert_ms: int


class MemoryStoreRecordService:
    """Owns post-Context record assembly and persistence."""

    def __init__(self, write_service: "MemoryWriteService") -> None:
        """Bind the persistence service to a write service facade."""
        self._write_service = write_service

    @property
    def _orch(self) -> Any:
        return self._write_service._orch

    async def persist_context_record(
        self,
        *,
        ctx: Context,
        content: str,
        abstract_json: Dict[str, Any],
        object_payload: Dict[str, Any],
        effective_category: str,
        keywords: str,
        entities: List[str],
        meta: Dict[str, Any],
        context_type: Optional[str],
        session_id: Optional[str],
        tenant_id: str,
        user_id: str,
        sparse_vector: Optional[Any],
        is_leaf: bool,
    ) -> StoredRecordResult:
        """Assemble and persist a normal store record."""
        orch = self._orch
        record = ctx.to_dict()
        if ctx.vector:
            record["vector"] = ctx.vector
        if sparse_vector:
            record["sparse_vector"] = sparse_vector

        uri = ctx.uri
        inferred_scope = "private" if CortexURI(uri).is_private else "shared"
        project_id = get_effective_project_id()
        record["scope"] = inferred_scope
        record["category"] = effective_category
        record["source_user_id"] = user_id
        record["session_id"] = session_id or ""
        record["ttl_expires_at"] = self._ttl_for_record(
            context_type=context_type,
            effective_category=effective_category,
            meta=meta,
        )
        record["project_id"] = project_id
        record["source_tenant_id"] = tenant_id
        record["keywords"] = keywords
        record["entities"] = entities
        record.update(object_payload)
        record["abstract_json"] = abstract_json
        self._populate_flattened_source_fields(record, meta)

        upsert_started = asyncio.get_running_loop().time()
        await orch._storage.upsert(orch._get_collection(), record)
        upsert_ms = int((asyncio.get_running_loop().time() - upsert_started) * 1000)
        await orch._sync_anchor_projection_records(
            source_record=record,
            abstract_json=abstract_json,
        )

        self._publish_memory_stored(
            record=record,
            uri=uri,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=str(project_id),
            context_type=str(context_type or ctx.context_type or "memory"),
            effective_category=effective_category,
        )
        self._sync_entity_index(record=record, entities=entities)
        self._schedule_cortexfs_write(
            uri=uri,
            content=content,
            abstract=ctx.abstract,
            abstract_json=abstract_json,
            overview=ctx.overview,
            is_leaf=is_leaf,
        )
        return StoredRecordResult(record=record, upsert_ms=upsert_ms)

    def _ttl_for_record(
        self,
        *,
        context_type: Optional[str],
        effective_category: str,
        meta: Dict[str, Any],
    ) -> str:
        """Return the TTL string for short-lived record kinds."""
        orch = self._orch
        if context_type == "staging":
            return orch._ttl_from_hours(orch._config.immediate_event_ttl_hours)
        if (
            (context_type or "memory") == "memory"
            and effective_category == "events"
            and meta.get("layer") == "merged"
        ):
            return orch._ttl_from_hours(orch._config.merged_event_ttl_hours)
        return ""

    @staticmethod
    def _populate_flattened_source_fields(
        record: Dict[str, Any],
        meta: Dict[str, Any],
    ) -> None:
        """Copy document/conversation enrichment fields to top level."""
        record["source_doc_id"] = meta.get("source_doc_id", "")
        record["source_doc_title"] = meta.get("source_doc_title", "")
        record["source_section_path"] = meta.get("source_section_path", "")
        record["chunk_role"] = meta.get("chunk_role", "")
        record["speaker"] = meta.get("speaker", "")
        record["event_date"] = meta.get("event_date")

    def _publish_memory_stored(
        self,
        *,
        record: Dict[str, Any],
        uri: str,
        tenant_id: str,
        user_id: str,
        project_id: str,
        context_type: str,
        effective_category: str,
    ) -> None:
        """Publish the post-store lifecycle signal when a bus exists."""
        signal_bus = getattr(self._orch, "_memory_signal_bus", None)
        if signal_bus is None:
            return
        signal_bus.publish_nowait(
            MemoryStoredSignal(
                uri=uri,
                record_id=str(record["id"]),
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=project_id,
                context_type=context_type,
                category=effective_category,
                record=dict(record),
            )
        )

    def _sync_entity_index(
        self,
        *,
        record: Dict[str, Any],
        entities: List[str],
    ) -> None:
        """Sync the entity index for entity-bearing records."""
        entity_index = getattr(self._orch, "_entity_index", None)
        if entity_index and entities:
            entity_index.add(self._orch._get_collection(), str(record["id"]), entities)

    def _schedule_cortexfs_write(
        self,
        *,
        uri: str,
        content: str,
        abstract: str,
        abstract_json: Dict[str, Any],
        overview: str,
        is_leaf: bool,
    ) -> None:
        """Schedule CortexFS write without blocking Qdrant persistence."""

        def _on_fs_done(task: asyncio.Task[Any]) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc:
                logger.warning(
                    "[MemoryService] CortexFS write failed for %s: %s",
                    uri,
                    exc,
                )

        fs_task = asyncio.create_task(
            self._orch._fs.write_context(
                uri=uri,
                content=content,
                abstract=abstract,
                abstract_json=abstract_json,
                overview=overview,
                is_leaf=is_leaf,
            )
        )
        fs_task.add_done_callback(_on_fs_done)
