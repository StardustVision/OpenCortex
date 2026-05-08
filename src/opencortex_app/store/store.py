# SPDX-License-Identifier: Apache-2.0
"""Memory and resource store flows."""

from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import structlog

from opencortex_app.core.identity import get_identity_profile
from opencortex_app.storage.namespace import CortexNamespace
from opencortex_app.store.common import (
    extract_category_from_uri,
    merge_unique_strings,
)
from opencortex_app.store.event.events import StoreEvents
from opencortex_app.store.schemas import (
    Context,
    MemoryStoreInput,
    PrimaryRecordInput,
    RawPrimaryRecord,
    ResourceStoreInput,
    StoredRecord,
    StoreTarget,
)
from opencortex_app.store.types import ContextType
from opencortex_app.store.writer.primary_record_writer import PrimaryRecordWriter

logger = structlog.get_logger(__name__)


class MemoryStore:
    """Store memory records through the explicit store flow."""

    def __init__(
        self,
        *,
        namespace: CortexNamespace,
        writer: PrimaryRecordWriter,
        events: StoreEvents,
    ) -> None:
        self.namespace = namespace
        self.writer = writer
        self.events = events

    async def store(self, input_: MemoryStoreInput) -> StoredRecord:
        """Write one raw memory primary record."""
        target = await self.resolve(input_)
        ctx = self.context(input_, target)
        profile = get_identity_profile()
        raw_record = RawPrimaryRecord.from_context(
            ctx=ctx,
            content=input_.content,
            effective_category=ctx.category or extract_category_from_uri(ctx.uri),
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            project_id=profile.project_id,
            session_id="",
            meta=ctx.meta,
        )
        record_input = PrimaryRecordInput(
            ctx=ctx,
            payload=raw_record.model_dump(mode="json"),
            effective_category=raw_record.category,
            meta=dict(ctx.meta),
            context_type=ContextType.MEMORY,
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            content=input_.content,
        )
        stored = await self.writer.write(record_input)
        self.events.memory_stored(record_input, stored)
        stored.meta["dedup_action"] = "created"
        logger.info(
            "memory_store_completed",
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            uri=stored.uri,
            upsert_ms=stored.upsert_ms,
        )
        return stored

    async def resolve(self, input_: MemoryStoreInput) -> StoreTarget:
        """Resolve memory target URI and metadata."""
        uri, parent_uri = await self.namespace.resolve(
            context_type=ContextType.MEMORY,
            category=input_.category,
            abstract="",
        )
        return StoreTarget(
            uri=uri,
            parent_uri=parent_uri,
            meta=dict(input_.meta),
            explicit_entities=merge_unique_strings(input_.meta.get("entities")),
            explicit_topics=merge_unique_strings(input_.meta.get("topics")),
        )

    def context(
        self,
        input_: MemoryStoreInput,
        target: StoreTarget,
    ) -> Context:
        """Assemble raw memory context without semantic derivation."""
        meta = dict(target.meta)
        profile = get_identity_profile()
        meta["project_id"] = profile.project_id
        return Context(
            uri=target.uri,
            parent_uri=target.parent_uri,
            is_leaf=True,
            context_type=ContextType.MEMORY,
            category=input_.category,
            related_uri=[],
            meta=meta,
            session_id="",
            profile=profile,
        )


class ResourceStore:
    """Store resource primary records through the explicit store flow."""

    def __init__(
        self,
        *,
        namespace: CortexNamespace,
        writer: PrimaryRecordWriter,
        events: StoreEvents,
    ) -> None:
        self.namespace = namespace
        self.writer = writer
        self.events = events

    async def store(self, input_: ResourceStoreInput) -> StoredRecord:
        """Write one raw resource primary record."""
        normalized = self.prepare(input_)
        target = await self.resolve(normalized)
        ctx = self.context(normalized, target)
        profile = get_identity_profile()
        raw_record = RawPrimaryRecord.from_context(
            ctx=ctx,
            content=normalized.content,
            effective_category=ctx.category or extract_category_from_uri(ctx.uri),
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            project_id=profile.project_id,
            session_id="",
            meta=ctx.meta,
        )
        record_input = PrimaryRecordInput(
            ctx=ctx,
            payload=raw_record.model_dump(mode="json"),
            effective_category=raw_record.category,
            meta=dict(ctx.meta),
            context_type=ContextType.RESOURCE,
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            content=normalized.content,
        )
        stored = await self.writer.write(record_input)
        self.events.resource_stored(record_input, stored)
        stored.meta["dedup_action"] = "created"
        return stored

    def prepare(self, input_: ResourceStoreInput) -> ResourceStoreInput:
        """Prepare resource metadata."""
        meta = dict(input_.meta)
        source_path = input_.source_path or str(meta.get("file_path", "") or "")
        if source_path:
            source_doc_id = hashlib.sha256(source_path.encode()).hexdigest()[:16]
        else:
            source_doc_id = uuid4().hex[:16]

        source_doc_title = str(meta.get("title", "") or "")
        if not source_doc_title and source_path:
            source_doc_title = os.path.basename(source_path)
        meta.setdefault("source_doc_id", source_doc_id)
        meta.setdefault("source_doc_title", source_doc_title)
        meta.setdefault("source_section_path", "")
        meta.setdefault("chunk_role", "resource")
        return input_.model_copy(update={"meta": meta})

    async def resolve(self, input_: ResourceStoreInput) -> StoreTarget:
        """Resolve resource target URI and metadata."""
        uri, parent_uri = await self.namespace.resolve(
            context_type=ContextType.RESOURCE,
            category=input_.category,
            abstract=input_.source_path or "resource",
        )
        return StoreTarget(
            uri=uri,
            parent_uri=parent_uri,
            meta=dict(input_.meta),
            explicit_entities=merge_unique_strings(input_.meta.get("entities")),
            explicit_topics=merge_unique_strings(input_.meta.get("topics")),
        )

    def context(
        self,
        input_: ResourceStoreInput,
        target: StoreTarget,
    ) -> Context:
        """Assemble raw resource context without semantic derivation."""
        meta = dict(target.meta)
        profile = get_identity_profile()
        meta["project_id"] = profile.project_id
        return Context(
            uri=target.uri,
            parent_uri=target.parent_uri,
            is_leaf=True,
            context_type=ContextType.RESOURCE,
            category=input_.category,
            related_uri=[],
            meta=meta,
            session_id="",
            profile=profile,
        )
