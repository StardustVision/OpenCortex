# SPDX-License-Identifier: Apache-2.0
"""Benchmark conversation ingest service.

REVIEW §25 Phase 3 — extracts the orchestration body of
``ContextManager.benchmark_ingest_conversation`` (~390 lines, 6
responsibilities) into a dedicated service so each responsibility lives
on its own private method. ``ContextManager`` retains the helpers the
service borrows (source persist, recomposition entries build,
recompose, summary generate, hydrate, export, evidence URI build,
session lifecycle dict mutation) — moving those wholesale is a
follow-up.

The six responsibilities, in order:

1. ``_normalize_segments`` — strip empty role/content rows; build the
   transcript list used by the source-hash check.
2. ``_idempotent_hit_response`` — hash-match short-circuit (returns
   the existing records' response when ``run_complete`` is set;
   triggers torn-replay purge otherwise).
3. ``_write_merged_leaves`` — per-segment merged-leaf writes plus
   ``defer_derive`` task scheduling (with sibling-cancel on first
   exception, REVIEW F1 / REL-01 / ADV-001).
4. ``_recompose_and_summarize`` — call recomposition; on failure
   drain the partial directory URIs into the cleanup tracker; then
   optionally run the summary.
5. ``_build_response`` — load merged records via the repo, hydrate
   content, mark source run_complete, return the dict shape the
   admin route serializes.
6. ``_ingest_direct_evidence`` — sibling code path for the
   ``ingest_shape="direct_evidence"`` branch, kept on the same
   service class because it shares the cleanup pattern + run_complete
   marker logic.

The cleanup tracker (``_BenchmarkRunCleanup``) and
``RecompositionError`` continue to live in ``context/manager.py`` —
moving them is a separate cosmetic follow-up. The service imports
both.

References:
- §25.1 verification gate: behavior golden test (response byte-equal
  to legacy method for the same input).
- §25.2 abstraction guardrails: only Service / Repository / DTO; no
  Strategy / Abstract Factory.
- Closure tracker entries this unit closes: R2-11 (SRP), R2-12
  (cleanup ownership boundary), F14 (ContextManager bloat).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from opencortex.context.manager import ContextManager
    from opencortex.context.session_records import SessionRecordsRepository

logger = logging.getLogger(__name__)


class BenchmarkConversationIngestService:
    """Orchestrates the benchmark offline conversation ingest flow.

    Constructed once per ``ContextManager`` (manager owns the
    instance and the cleanup tracker / shared semaphores the service
    borrows). The service holds a reference to the manager and to the
    session records repository — those are the only collaborators it
    needs to thread together; everything else lives on one of those
    two and is reached via dot-access.
    """

    def __init__(
        self,
        manager: "ContextManager",
        repo: "SessionRecordsRepository",
    ) -> None:
        self._manager = manager
        self._repo = repo

    # =========================================================================
    # Public entry point
    # =========================================================================

    async def ingest(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        segments: List[List[Dict[str, Any]]],
        include_session_summary: bool = True,
        ingest_shape: str = "merged_recompose",
    ) -> Dict[str, Any]:
        """Drive the full benchmark ingest lifecycle for one session.

        Returns the response dict the admin route currently serializes.
        U5 will switch this return type to
        ``BenchmarkConversationIngestResponse`` (Pydantic). Until then
        the dict shape is locked by the existing test suite.
        """
        shape = str(ingest_shape or "merged_recompose").strip().lower()
        if shape not in {"merged_recompose", "direct_evidence"}:
            raise ValueError(f"unsupported benchmark ingest_shape: {ingest_shape}")

        manager = self._manager
        sk = manager._make_session_key(tenant_id, user_id, session_id)
        manager._touch_session(sk)
        manager._remember_session_project(sk)
        lock = manager._session_locks.setdefault(sk, asyncio.Lock())

        async with lock:
            normalized_segments, transcript = self._normalize_segments(segments)

            if not normalized_segments:
                logger.info(
                    "benchmark_ingest_conversation: no normalized segments "
                    "for session %s — returning empty record set",
                    session_id,
                )
                return {
                    "status": "ok",
                    "session_id": session_id,
                    "source_uri": None,
                    "summary_uri": None,
                    "records": [],
                }

            # SourceConflictError propagates out of the lock — the HTTP
            # layer maps it to 409. Raised before the cleanup-tracker
            # scope because there is nothing to roll back: the existing
            # source belongs to a prior run.
            source_uri = await manager._persist_rendered_conversation_source(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                transcript=transcript,
                enforce_transcript_hash=True,
            )

            if shape == "direct_evidence":
                return await self._ingest_direct_evidence(
                    session_id=session_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    source_uri=source_uri,
                    normalized_segments=normalized_segments,
                )

            # Idempotent hit OR torn-replay detection. Returns either
            # the cached response (idempotent hit) or None (cold ingest
            # after purge / no prior records).
            idempotent_response = await self._idempotent_hit_response(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                source_uri=source_uri,
            )
            if idempotent_response is not None:
                return idempotent_response

            return await self._ingest_merged_recompose(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                source_uri=source_uri,
                normalized_segments=normalized_segments,
                include_session_summary=include_session_summary,
            )

    # =========================================================================
    # Step 1 — normalize incoming segments
    # =========================================================================

    @staticmethod
    def _normalize_segments(
        segments: List[List[Dict[str, Any]]],
    ) -> Tuple[List[List[Dict[str, Any]]], List[Dict[str, Any]]]:
        """Strip empty role/content rows; return (segments, transcript).

        ``transcript`` is the flat list used for source-hash computation
        in ``_persist_rendered_conversation_source(..., enforce_transcript_hash=True)``.
        """
        normalized_segments: List[List[Dict[str, Any]]] = []
        transcript: List[Dict[str, Any]] = []
        for segment in segments:
            normalized_messages: List[Dict[str, Any]] = []
            for message in segment:
                role = str(message.get("role", "") or "").strip()
                content = str(message.get("content", "") or "").strip()
                if not role or not content:
                    continue
                normalized = {
                    "role": role,
                    "content": content,
                    "meta": dict(message.get("meta") or {}),
                }
                normalized_messages.append(normalized)
                transcript.append(normalized)
            if normalized_messages:
                normalized_segments.append(normalized_messages)
        return normalized_segments, transcript

    # =========================================================================
    # Step 2 — idempotent hit / torn-replay short-circuit
    # =========================================================================

    async def _idempotent_hit_response(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the cached response if this is a true idempotent replay.

        Returns ``None`` when there are no prior records (cold ingest) OR
        when prior records exist but the source's ``run_complete`` marker
        is missing (torn prior run — purge inline and fall through to
        cold ingest).
        """
        manager = self._manager
        existing_records = await self._repo.load_merged(
            session_id=session_id,
            source_uri=source_uri,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if not existing_records:
            return None

        # Verify the prior run actually completed (REVIEW F5 / ADV-007).
        # Hash matches but if ``run_complete`` is absent the prior
        # ingest crashed before marking itself done — treat as cold
        # ingest after purging the stale records.
        source_record = await manager._orchestrator._get_record_by_uri(source_uri)
        source_meta = (
            dict(source_record.get("meta") or {}) if source_record else {}
        )
        run_complete = bool(source_meta.get("run_complete"))

        if not run_complete:
            logger.warning(
                "benchmark_ingest_conversation: torn prior run "
                "detected sid=%s source=%s — purging stale "
                "records and re-ingesting",
                session_id,
                source_uri,
            )
            await manager._purge_torn_benchmark_run(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                source_uri=source_uri,
                merged_records=existing_records,
            )
            return None

        # Genuine idempotent hit: surface the prior run's summary URI
        # if one was persisted (REVIEW F4 / api-contract-005).
        existing_summary_uri = manager._session_summary_uri(
            tenant_id, user_id, session_id
        )
        existing_summary = await self._repo.load_summary(existing_summary_uri)
        summary_uri_for_response = (
            existing_summary_uri if existing_summary else None
        )

        logger.info(
            "benchmark_ingest_conversation: idempotent hit "
            "sid=%s source_uri=%s records=%d summary=%s",
            session_id,
            source_uri,
            len(existing_records),
            "present" if existing_summary else "absent",
        )
        hydrated = await manager._hydrate_record_contents(existing_records)
        return {
            "status": "ok",
            "session_id": session_id,
            "source_uri": source_uri,
            "summary_uri": summary_uri_for_response,
            "records": [
                manager._export_memory_record(
                    record,
                    hydrated_content=hydrated.get(
                        str(record.get("uri", "") or ""),
                        "",
                    ),
                )
                for record in existing_records
            ],
        }

    # =========================================================================
    # Step 3 — merged_recompose ingest path
    # =========================================================================

    async def _ingest_merged_recompose(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: str,
        normalized_segments: List[List[Dict[str, Any]]],
        include_session_summary: bool,
    ) -> Dict[str, Any]:
        """Cold-ingest path for ``ingest_shape="merged_recompose"``."""
        from opencortex.context.manager import (  # avoid circular import
            RecompositionError,
            _BenchmarkRunCleanup,
        )

        manager = self._manager
        cleanup = _BenchmarkRunCleanup(source_uri=source_uri)

        try:
            merged_content_by_uri = await self._write_merged_leaves(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                source_uri=source_uri,
                normalized_segments=normalized_segments,
                cleanup=cleanup,
            )

            if cleanup.merged_uris:
                await self._recompose_and_summarize(
                    session_id=session_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    source_uri=source_uri,
                    include_session_summary=include_session_summary,
                    cleanup=cleanup,
                )

            return await self._build_response(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                source_uri=source_uri,
                cleanup=cleanup,
                merged_content_by_uri=merged_content_by_uri,
            )

        except asyncio.CancelledError:
            # CancelledError descends from BaseException, not Exception,
            # so the prior `except Exception` (in the legacy ContextManager
            # method) let cancellation (FastAPI request cancel, server
            # timeout, client disconnect) bypass cleanup entirely.
            # Compensate explicitly, then re-raise so cancellation
            # semantics flow through unchanged.
            logger.warning(
                "benchmark_ingest_conversation cancelled mid-flight "
                "sid=%s — running compensation",
                session_id,
            )
            await cleanup.compensate(manager)
            raise
        except Exception:
            logger.warning(
                "benchmark_ingest_conversation failed sid=%s — "
                "running compensation",
                session_id,
                exc_info=True,
            )
            await cleanup.compensate(manager)
            raise

    # =========================================================================
    # Step 3a — write merged leaves + schedule defer-derive
    # =========================================================================

    async def _write_merged_leaves(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: str,
        normalized_segments: List[List[Dict[str, Any]]],
        cleanup: Any,
    ) -> Dict[str, str]:
        """Per-segment merged-leaf writes + bounded-concurrency derive.

        Returns the in-memory map of leaf URI -> raw conversation text
        so the response builder can hydrate ``content`` without racing
        the orchestrator's fire-and-forget CortexFS write.
        """
        manager = self._manager
        merged_content_by_uri: Dict[str, str] = {}
        derive_tasks: List[Tuple[str, asyncio.Task]] = []

        async def _bounded_complete(
            sem: asyncio.Semaphore,
            **dkw: Any,
        ) -> None:
            async with sem:
                await manager._orchestrator._complete_deferred_derive(**dkw)

        entries = manager._benchmark_recomposition_entries(normalized_segments)
        offline_segments = manager._build_recomposition_segments(entries)

        for segment in offline_segments:
            segment_texts = [str(text) for text in segment.get("messages", [])]
            if not segment_texts:
                continue

            msg_range = list(segment["msg_range"])
            source_records = segment.get("source_records", [])
            merged_meta = await manager._aggregate_records_metadata(source_records)
            all_tool_calls: List[Dict[str, Any]] = []
            for record in source_records:
                meta = dict(record.get("meta") or {})
                for call in meta.get("tool_calls", []) or []:
                    if isinstance(call, dict):
                        all_tool_calls.append(call)

            combined = "\n\n".join(segment_texts)
            leaf_meta = {
                **merged_meta,
                "layer": "merged",
                "ingest_mode": "memory",
                "msg_range": list(msg_range),
                "source_uri": source_uri or "",
                "session_id": session_id,
                "recomposition_stage": "benchmark_offline",
                "tool_calls": all_tool_calls if all_tool_calls else [],
            }
            merged_context = await manager._orchestrator.add(
                uri=manager._merged_leaf_uri(
                    tenant_id, user_id, session_id, msg_range
                ),
                abstract="",
                content=combined,
                category="events",
                context_type="memory",
                meta=leaf_meta,
                session_id=session_id,
                dedup=False,
                defer_derive=True,
            )
            cleanup.merged_uris.append(merged_context.uri)
            merged_content_by_uri[merged_context.uri] = combined

            # Schedule LLM derive on the same global semaphore the
            # production lifecycle uses.
            task = asyncio.create_task(
                _bounded_complete(
                    manager._derive_semaphore,
                    uri=merged_context.uri,
                    content=combined,
                    abstract="",
                    overview="",
                    session_id=session_id,
                    meta=dict(leaf_meta),
                    raise_on_error=True,
                )
            )
            derive_tasks.append((merged_context.uri, task))

        if derive_tasks:
            # Wait for every scheduled derive so the response represents
            # post-derive state. Cancel siblings on first exception
            # (REVIEW F1 / REL-01 / ADV-001) so they do not race the
            # cleanup tracker after compensate runs.
            pending = [task for _, task in derive_tasks]
            try:
                await asyncio.gather(*pending)
            except BaseException:
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise

        return merged_content_by_uri

    # =========================================================================
    # Step 3b — recompose + optional summary
    # =========================================================================

    async def _recompose_and_summarize(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: str,
        include_session_summary: bool,
        cleanup: Any,
    ) -> None:
        """Run full-session recomposition; optionally generate summary.

        Catches ``RecompositionError`` to drain partial directory URIs
        into the cleanup tracker before re-raising the original
        exception (REVIEW REL-02). This keeps cleanup ownership in
        the run-scoped tracker even when recompose fails partway.
        """
        from opencortex.context.manager import (  # avoid circular import
            RecompositionError,
        )

        manager = self._manager
        try:
            directory_uris = await manager._run_full_session_recomposition(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                source_uri=source_uri,
                raise_on_error=True,
                return_created_uris=True,
            )
        except RecompositionError as exc:
            cleanup.directory_uris.extend(exc.created_uris)
            raise exc.original from exc

        if directory_uris:
            cleanup.directory_uris.extend(directory_uris)
        if include_session_summary:
            cleanup.summary_uri = await manager._generate_session_summary(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                source_uri=source_uri,
            )

    # =========================================================================
    # Step 3c — build response (load + hydrate + mark run_complete)
    # =========================================================================

    async def _build_response(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: str,
        cleanup: Any,
        merged_content_by_uri: Dict[str, str],
    ) -> Dict[str, Any]:
        """Load final merged records, hydrate content, mark run_complete."""
        manager = self._manager
        merged_records = await self._repo.load_merged(
            session_id=session_id,
            source_uri=source_uri,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        # Single-call hydration: in-memory map covers our own writes,
        # FS read is the fallback for records that came back from
        # ``load_merged`` outside our write set.
        hydrated = await manager._hydrate_record_contents(
            merged_records, overrides=merged_content_by_uri
        )
        # Mark the source as run_complete BEFORE returning so an
        # immediate retry sees the marker and short-circuits via the
        # idempotent path (REVIEW F5 / ADV-007).
        await manager._mark_source_run_complete(source_uri)
        return {
            "status": "ok",
            "session_id": session_id,
            "source_uri": source_uri,
            "summary_uri": cleanup.summary_uri,
            "records": [
                manager._export_memory_record(
                    record,
                    hydrated_content=hydrated.get(
                        str(record.get("uri", "") or ""),
                        "",
                    ),
                )
                for record in merged_records
            ],
        }

    # =========================================================================
    # Step 4 — direct_evidence ingest path
    # =========================================================================

    async def _ingest_direct_evidence(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: Optional[str],
        normalized_segments: List[List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """Store benchmark segments directly as searchable evidence records.

        Sibling to ``_ingest_merged_recompose``. No recompose, no
        directory writes, no session summary; one ``orchestrator.add``
        per input segment. Cleanup uses the legacy
        ``_delete_immediate_families`` path because the cleanup tracker
        is overkill for this single-write-loop pattern.
        """
        manager = self._manager
        created_uris: List[str] = []
        records: List[Dict[str, Any]] = []
        evidence_content_by_uri: Dict[str, str] = {}
        next_msg_index = 0

        try:
            for segment_index, segment in enumerate(normalized_segments):
                segment_texts: List[str] = []
                segment_start = next_msg_index
                for message in segment:
                    meta = dict(message.get("meta") or {})
                    role = str(message.get("role", "") or "").strip()
                    content = str(message.get("content", "") or "").strip()
                    if not content:
                        continue
                    rendered = f"{role}: {content}" if role else content
                    segment_texts.append(
                        manager._decorate_message_text(rendered, meta)
                    )
                    next_msg_index += 1

                if not segment_texts:
                    continue

                msg_range = [segment_start, next_msg_index - 1]
                segment_meta = manager._benchmark_segment_meta(segment)
                meta = {
                    **segment_meta,
                    "layer": "benchmark_evidence",
                    "ingest_mode": "memory",
                    "msg_range": list(msg_range),
                    "source_uri": source_uri or "",
                    "session_id": session_id,
                    "recomposition_stage": "benchmark_direct_evidence",
                }
                evidence_uri = manager._benchmark_evidence_uri(
                    tenant_id, user_id, session_id, segment_index, msg_range
                )
                combined = "\n".join(segment_texts)
                stored = await manager._orchestrator.add(
                    uri=evidence_uri,
                    abstract="",
                    content=combined,
                    category="events",
                    context_type="memory",
                    meta=meta,
                    session_id=session_id,
                    dedup=False,
                    defer_derive=True,
                )
                created_uris.append(stored.uri)
                evidence_content_by_uri[stored.uri] = combined
                record = await manager._orchestrator._get_record_by_uri(stored.uri)
                if record:
                    records.append(record)

            await manager._mark_source_run_complete(source_uri or "")
            hydrated = await manager._hydrate_record_contents(
                records, overrides=evidence_content_by_uri
            )
            return {
                "status": "ok",
                "session_id": session_id,
                "source_uri": source_uri,
                "summary_uri": None,
                "ingest_shape": "direct_evidence",
                "records": [
                    manager._export_memory_record(
                        record,
                        hydrated_content=hydrated.get(
                            str(record.get("uri", "") or ""),
                            "",
                        ),
                    )
                    for record in records
                ],
            }
        except asyncio.CancelledError:
            if created_uris:
                with contextlib.suppress(Exception):
                    await manager._delete_immediate_families(created_uris)
            raise
        except Exception:
            if created_uris:
                with contextlib.suppress(Exception):
                    await manager._delete_immediate_families(created_uris)
            raise
