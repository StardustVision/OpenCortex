# SPDX-License-Identifier: Apache-2.0
"""Session and trace lifecycle service for OpenCortex.

This module owns Observer session recording, immediate-message persistence,
trace callbacks, benchmark ingest delegation, and session-end side effects.
The orchestrator keeps thin compatibility wrappers for existing callers.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from uuid import uuid4

from opencortex.cognition.state_types import OwnerType
from opencortex.context.benchmark_ingest_service import (
    BenchmarkConversationIngestService,
)
from opencortex.context.session_records import SessionRecordsRepository
from opencortex.http.request_context import (
    get_effective_identity,
    get_effective_project_id,
)
from opencortex.services.derivation_service import _merge_unique_strings
from opencortex.services.memory_filters import FilterExpr
from opencortex.utils.uri import CortexURI

if TYPE_CHECKING:
    from opencortex.cortex_memory import CortexMemory

logger = logging.getLogger(__name__)

_IMMEDIATE_EMBED_TIMEOUT_SECONDS = 8.0


class SessionLifecycleService:
    """Own session/trace lifecycle behavior using orchestrator subsystems."""

    def __init__(self, orchestrator: "CortexMemory") -> None:
        self._orch = orchestrator
        self._benchmark_ingest_service_instance: Optional[
            BenchmarkConversationIngestService
        ] = None
        self._benchmark_session_records_instance: Optional[SessionRecordsRepository] = (
            None
        )

    @property
    def _config(self) -> Any:
        return self._orch._config

    @property
    def _storage(self) -> Any:
        return self._orch._storage

    @property
    def _embedder(self) -> Any:
        return self._orch._embedder

    @property
    def _fs(self) -> Any:
        return self._orch._fs

    @property
    def _observer(self) -> Any:
        return self._orch._observer

    @property
    def _trace_splitter(self) -> Any:
        return self._orch._trace_splitter

    @property
    def _trace_store(self) -> Any:
        return self._orch._trace_store

    @property
    def _archivist(self) -> Any:
        return self._orch._archivist

    @property
    def _skill_evaluator(self) -> Any:
        return self._orch._skill_evaluator

    @property
    def _autophagy_kernel(self) -> Any:
        return self._orch._autophagy_kernel

    def _ensure_init(self) -> None:
        self._orch._ensure_init()

    def _get_collection(self) -> str:
        return self._orch._get_collection()

    @property
    def _benchmark_session_records(self) -> SessionRecordsRepository:
        inst = self._benchmark_session_records_instance
        if inst is None:
            inst = SessionRecordsRepository(
                storage=self._storage,
                collection_resolver=self._get_collection,
            )
            self._benchmark_session_records_instance = inst
        return inst

    @property
    def _benchmark_ingest_service(self) -> BenchmarkConversationIngestService:
        inst = self._benchmark_ingest_service_instance
        if inst is None:
            if not self._orch._context_manager:
                raise RuntimeError("ContextManager not initialized")
            inst = BenchmarkConversationIngestService(
                manager=self._orch._context_manager,
                repo=self._benchmark_session_records,
            )
            self._benchmark_ingest_service_instance = inst
        return inst

    async def _write_immediate(
        self,
        session_id: str,
        msg_index: int,
        text: str,
        tool_calls: Optional[list] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Write a single message for immediate searchability."""
        orch = self._orch
        tid, uid = get_effective_identity()
        nid = uuid4().hex
        uri = CortexURI.build_private(tid, uid, "memories", "events", nid)
        meta = dict(meta or {})
        explicit_entities = _merge_unique_strings(meta.get("entities"))
        explicit_topics = _merge_unique_strings(meta.get("topics"))
        record_meta = dict(meta)
        if explicit_topics:
            record_meta["topics"] = _merge_unique_strings(
                record_meta.get("topics"),
                explicit_topics,
            )
        record_meta.update(
            {
                "layer": "immediate",
                "msg_index": msg_index,
                "session_id": session_id,
                "tool_calls": tool_calls or [],
            }
        )

        embed_input = text
        if self._config.context_flattening_enabled:
            speaker = ""
            for prefix in ("user:", "assistant:", "system:"):
                if text.lower().startswith(prefix):
                    speaker = prefix.rstrip(":")
                    break
            if speaker:
                embed_input = f"[{speaker}] {text}"

        vector = None
        sparse_vector = None
        if self._embedder:
            loop = asyncio.get_running_loop()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, self._embedder.embed, embed_input),
                    timeout=_IMMEDIATE_EMBED_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                fallback_embedder = None
                if (
                    (self._config.embedding_provider or "").strip().lower() == "openai"
                    and orch._is_retryable_immediate_embed_exception(exc)
                ):
                    fallback_embedder = orch._get_immediate_fallback_embedder()
                if fallback_embedder is None:
                    raise
                logger.warning(
                    "[SessionLifecycleService] Immediate remote embedding failed; "
                    "retrying local fallback model=%s exc_type=%s exc=%r",
                    getattr(fallback_embedder, "model_name", "local-fallback"),
                    type(exc).__name__,
                    exc,
                )
                try:
                    result = await loop.run_in_executor(
                        None,
                        fallback_embedder.embed,
                        embed_input,
                    )
                except Exception as fallback_exc:
                    logger.warning(
                        "[SessionLifecycleService] Immediate local fallback embedding "
                        "failed model=%s exc_type=%s exc=%r",
                        getattr(fallback_embedder, "model_name", "local-fallback"),
                        type(fallback_exc).__name__,
                        fallback_exc,
                    )
                    raise exc from fallback_exc
            vector = result.dense_vector
            sparse_vector = result.sparse_vector

        record = {
            "uri": uri,
            "parent_uri": CortexURI.build_private(
                tid, uid, "memories", "events", session_id
            ),
            "is_leaf": True,
            "abstract": text,
            "overview": "",
            "context_type": "memory",
            "category": "events",
            "scope": "private",
            "source_user_id": uid,
            "source_tenant_id": tid,
            "keywords": ", ".join(explicit_topics),
            "entities": explicit_entities,
            "meta": {
                **record_meta,
            },
            "session_id": session_id,
            "project_id": get_effective_project_id(),
            "ttl_expires_at": orch._ttl_from_hours(
                self._config.immediate_event_ttl_hours
            ),
            "speaker": str(record_meta.get("speaker", "") or ""),
            "event_date": record_meta.get("event_date"),
        }

        if vector:
            record["vector"] = vector
        if sparse_vector:
            record["sparse_vector"] = sparse_vector

        abstract_json = orch._build_abstract_json(
            uri=uri,
            context_type="memory",
            category="events",
            abstract=text,
            overview="",
            content=text,
            entities=explicit_entities,
            meta=record_meta,
            keywords=explicit_topics,
            parent_uri=record["parent_uri"],
            session_id=session_id,
        )
        record.update(orch._memory_object_payload(abstract_json, is_leaf=True))
        record["abstract_json"] = abstract_json

        record_id = await self._storage.upsert(self._get_collection(), record)
        record["id"] = record_id
        await orch._sync_anchor_projection_records(
            source_record=record,
            abstract_json=abstract_json,
        )
        if orch._entity_index and explicit_entities:
            orch._entity_index.add(
                self._get_collection(),
                str(record.get("id") or record_id),
                explicit_entities,
            )
        try:
            await self._fs.write_context(
                uri=uri,
                content=text,
                abstract=text,
                abstract_json=abstract_json,
                overview="",
                is_leaf=True,
            )
        except Exception as exc:
            logger.warning(
                "[SessionLifecycleService] Immediate CortexFS write failed for %s: %s",
                uri,
                exc,
            )
        return uri

    async def _resolve_memory_owner_ids(self, matches: List[Any]) -> List[str]:
        """Resolve memory owner ids from matched contexts using persisted ids."""
        if not matches or not self._storage:
            return []

        uris = []
        for match in matches:
            uri = getattr(match, "uri", "")
            if isinstance(uri, str) and uri:
                uris.append(uri)
        if not uris:
            return []

        try:
            records = await self._storage.filter(
                self._get_collection(),
                FilterExpr.eq("uri", *dict.fromkeys(uris)).to_dict(),
                limit=max(len(uris), 1) * 4,
            )
        except Exception as exc:
            logger.debug(
                "[SessionLifecycleService] Failed to resolve memory owner ids: %s",
                exc,
            )
            return []

        ids_by_uri = {
            record.get("uri", ""): str(record.get("id", ""))
            for record in records
            if record.get("uri") and record.get("id")
        }
        owner_ids: List[str] = []
        for uri in uris:
            owner_id = ids_by_uri.get(uri)
            if owner_id and owner_id not in owner_ids:
                owner_ids.append(owner_id)
        return owner_ids

    async def _get_record_by_uri(self, uri: str) -> Optional[Dict[str, Any]]:
        """Look up a single record by its URI."""
        if not uri or not self._storage:
            return None
        try:
            records = await self._storage.filter(
                self._get_collection(),
                FilterExpr.eq("uri", uri).to_dict(),
                limit=1,
            )
        except Exception as exc:
            logger.debug(
                "[SessionLifecycleService] Failed to load record for uri=%s: %s",
                uri,
                exc,
            )
            return None
        return records[0] if records else None

    async def _initialize_autophagy_owner_state(
        self,
        *,
        owner_type: OwnerType,
        owner_id: str,
        tenant_id: str,
        user_id: str,
        project_id: str,
    ) -> None:
        """Bootstrap autophagy state for a new owner when enabled."""
        if not self._autophagy_kernel or not owner_id:
            return
        try:
            await self._autophagy_kernel.initialize_owner(
                owner_type=owner_type,
                owner_id=owner_id,
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=project_id,
            )
        except Exception as exc:
            logger.warning(
                "[SessionLifecycleService] Autophagy owner init failed type=%s "
                "owner=%s tenant=%s user=%s: %s",
                owner_type.value,
                owner_id,
                tenant_id,
                user_id,
                exc,
            )

    async def _on_trace_saved(self, trace: Any) -> None:
        """Callback invoked after a trace is persisted."""
        await self._orch._initialize_autophagy_owner_state(
            owner_type=OwnerType.TRACE,
            owner_id=str(getattr(trace, "trace_id", "")),
            tenant_id=str(getattr(trace, "tenant_id", "")),
            user_id=str(getattr(trace, "user_id", "")),
            project_id=str(getattr(trace, "project_id", ""))
            or get_effective_project_id(),
        )

    async def _resolve_and_update_access_stats(self, uris: list) -> None:
        """Resolve URIs once, then update access stats in parallel."""
        if not uris:
            return
        try:
            recs = await self._storage.filter(
                self._get_collection(),
                FilterExpr.eq("uri", *uris).to_dict(),
                limit=len(uris),
            )
        except Exception:
            return
        if not recs:
            return
        await self._orch._update_access_stats_batch(recs)

    async def _update_access_stats_batch(self, records: list) -> None:
        """Parallel batch update access_count + accessed_at."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        async def _one(record: dict) -> None:
            record_id = record.get("id", "")
            if not record_id:
                return
            try:
                await self._storage.update(
                    self._get_collection(),
                    record_id,
                    {
                        "active_count": record.get("active_count", 0) + 1,
                        "accessed_at": now,
                    },
                )
            except Exception as exc:
                logger.debug(
                    "[SessionLifecycleService] Access stats update failed for %s: %s",
                    record_id,
                    exc,
                )

        await asyncio.gather(
            *[_one(record) for record in records],
            return_exceptions=True,
        )

    async def session_begin(
        self,
        session_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Begin a new session."""
        self._ensure_init()
        tid, uid = get_effective_identity()
        if self._observer:
            self._observer.begin_session(
                session_id=self._orch._observer_session_id(
                    session_id,
                    tenant_id=tid,
                    user_id=uid,
                ),
                tenant_id=tid,
                user_id=uid,
                meta=meta,
            )
        from opencortex.utils.time_utils import get_current_timestamp

        return {
            "session_id": session_id,
            "started_at": get_current_timestamp(),
            "status": "active",
        }

    async def session_message(
        self,
        session_id: str,
        role: str,
        content: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Add a message to an active session."""
        self._ensure_init()
        tid, uid = get_effective_identity()
        message_count = 0
        if self._observer:
            observer_session_id = self._orch._observer_session_id(
                session_id,
                tenant_id=tid,
                user_id=uid,
            )
            self._observer.record_message(
                session_id=observer_session_id,
                role=role,
                content=content,
                tenant_id=tid,
                user_id=uid,
                meta=meta,
            )
            message_count = len(self._observer.get_transcript(observer_session_id))
        return {
            "added": True,
            "message_count": message_count,
        }

    async def benchmark_conversation_ingest(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        segments: List[List[Dict[str, Any]]],
        include_session_summary: bool = True,
        ingest_shape: str = "merged_recompose",
        enforce_admin: bool = True,
    ) -> Dict[str, Any]:
        """Public facade for benchmark-only offline conversation ingest."""
        self._ensure_init()
        if enforce_admin:
            from opencortex.http.request_context import is_admin

            if not is_admin():
                raise PermissionError(
                    "benchmark_conversation_ingest requires admin role "
                    "(set request role contextvar to 'admin' or pass "
                    "enforce_admin=False for trusted in-process callers)"
                )
        return await self._benchmark_ingest_service.ingest(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            segments=segments,
            include_session_summary=include_session_summary,
            ingest_shape=ingest_shape,
        )

    async def session_end(
        self,
        session_id: str,
        quality_score: float = 0.5,
    ) -> Dict[str, Any]:
        """End a session and trigger trace splitting."""
        self._ensure_init()

        alpha_traces_count = 0
        if self._observer:
            tid, uid = get_effective_identity()
            transcript = self._observer.flush(
                self._orch._observer_session_id(
                    session_id,
                    tenant_id=tid,
                    user_id=uid,
                )
            )
            if transcript and self._trace_splitter and self._trace_store:
                try:
                    traces = await self._trace_splitter.split(
                        messages=transcript,
                        session_id=session_id,
                        tenant_id=tid,
                        user_id=uid,
                    )
                    for trace in traces:
                        await self._trace_store.save(trace)
                    alpha_traces_count = len(traces)
                    logger.info(
                        "[Alpha] Split session %s into %d traces",
                        session_id,
                        alpha_traces_count,
                    )

                    if self._archivist and self._trace_store:
                        count = await self._trace_store.count_new_traces(tid)
                        if self._archivist.should_trigger(count):
                            asyncio.create_task(
                                self._orch._knowledge_service.run_archivist(tid, uid)
                            )

                except Exception as exc:
                    logger.warning("[Alpha] Trace splitting failed: %s", exc)

            if self._skill_evaluator:
                asyncio.create_task(
                    self._skill_evaluator.evaluate_session(tid, uid, session_id)
                )

        return {
            "session_id": session_id,
            "quality_score": quality_score,
            "alpha_traces": alpha_traces_count,
        }
