# SPDX-License-Identifier: Apache-2.0
"""Session immediate-to-merged primary record flow."""

from __future__ import annotations

from opencortex_app.core.identity import IdentityProfile
from opencortex_app.storage.namespace import CortexNamespace
from opencortex_app.store.event.events import StoreEvents
from opencortex_app.store.schemas import (
    Context,
    PrimaryRecordInput,
    RawPrimaryRecord,
    StoredRecord,
    primary_ttl,
)
from opencortex_app.store.session.buffer import SessionBuffer, SessionKey
from opencortex_app.store.types import ContextType, MemoryCategory, SessionRecordLayer
from opencortex_app.store.writer.primary_record_writer import PrimaryRecordWriter


class SessionMerger:
    """Merge buffered immediate records into merged primary records."""

    def __init__(
        self,
        *,
        buffer: SessionBuffer,
        namespace: CortexNamespace,
        writer: PrimaryRecordWriter,
        events: StoreEvents,
        config: object,
        ttl_from_hours: object,
    ) -> None:
        self.buffer = buffer
        self.namespace = namespace
        self.writer = writer
        self.events = events
        self.config = config
        self.ttl_from_hours = ttl_from_hours

    async def merge_unmerged(
        self,
        key: SessionKey,
        *,
        profile: IdentityProfile,
    ) -> StoredRecord | None:
        """Synchronously merge the current unmerged session buffer."""
        snapshot = self.buffer.snapshot(key)
        if snapshot is None:
            return None

        content = "\n\n".join(snapshot.messages)
        msg_range = [
            snapshot.start_msg_index,
            snapshot.start_msg_index + len(snapshot.messages) - 1,
        ]
        record_input = self.build_merged_record(
            profile=profile,
            content=content,
            msg_range=msg_range,
            source_uris=snapshot.immediate_uris,
        )
        stored = await self.writer.write(record_input)
        self.events.session_merged(
            profile=profile,
            merged_uri=stored.uri,
            source_uris=snapshot.immediate_uris,
            content=content,
            record=dict(stored.record),
        )
        return stored

    def build_merged_record(
        self,
        *,
        profile: IdentityProfile,
        content: str,
        msg_range: list[int],
        source_uris: list[str],
    ) -> PrimaryRecordInput:
        """Build one merged-leaf primary record from buffered messages."""
        session_id = profile.session_id
        uri = self.namespace.session_merged_uri(
            session_id,
            msg_range,
            profile=profile,
        )
        parent_uri = self.namespace.session_events_parent(session_id, profile=profile)
        meta = {
            "project_id": profile.project_id,
            "layer": str(SessionRecordLayer.MERGED),
            "session_id": session_id,
            "msg_range": msg_range,
            "source_uri": parent_uri,
            "recomposition_stage": "online_tail",
            "source_uris": list(source_uris),
        }
        ctx = Context(
            uri=uri,
            parent_uri=parent_uri,
            is_leaf=True,
            context_type=ContextType.MEMORY,
            category=str(MemoryCategory.EVENTS),
            meta=meta,
            session_id=session_id,
            profile=profile,
        )
        raw_record = RawPrimaryRecord.from_context(
            ctx=ctx,
            content=content,
            effective_category=str(MemoryCategory.EVENTS),
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            project_id=profile.project_id,
            session_id=session_id,
            meta=meta,
            ttl_expires_at=primary_ttl(
                config=self.config,
                ttl_from_hours=self.ttl_from_hours,
                context_type=ContextType.MEMORY,
                category=str(MemoryCategory.EVENTS),
                layer=str(SessionRecordLayer.MERGED),
            ),
        )
        return PrimaryRecordInput(
            ctx=ctx,
            payload=raw_record.model_dump(mode="json"),
            effective_category=raw_record.category,
            meta=meta,
            context_type=ContextType.MEMORY,
            session_id=session_id,
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            content=content,
        )
