# SPDX-License-Identifier: Apache-2.0
"""ContextManager — commit/end lifecycle for the HTTP context API.

Manages session recording and closeout. Recall now uses the intent/search
pipeline directly instead of a context prepare phase.

Design doc: docs/memory-context-protocol.md v1.2
"""

import asyncio
import contextlib
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import orjson as json

from opencortex.context.recomposition_types import RecompositionEntry
from opencortex.context.session_records import (
    SessionRecordOverflowError,
    SessionRecordsRepository,
)
from opencortex.http.request_context import (
    get_effective_project_id,
    reset_request_identity,
    set_request_identity,
)

logger = logging.getLogger(__name__)

_SEGMENT_MAX_MESSAGES = 16
_SEGMENT_MAX_TOKENS = 1200
_SEGMENT_MIN_MESSAGES = 2
_RECOMPOSE_TAIL_MAX_MERGED_LEAVES = 6
_RECOMPOSE_TAIL_MAX_MESSAGES = 24
# Anchor-clustered recomposition caps. Previous values (1_000_000) were
# effectively no limit, so anchorless or low-jaccard inputs could grow a
# single cluster until ``_derive_parent_summary`` blew the LLM context
# window. Targets here match production conversation-mode budget headroom
# for the typical ``children_abstracts`` prompt.
_RECOMPOSE_CLUSTER_MAX_TOKENS = 6_000
_RECOMPOSE_CLUSTER_MAX_MESSAGES = 60

# Bounded concurrency for ``_derive_parent_summary`` calls inside
# ``_run_full_session_recomposition``. Production conversation lifecycle
# also benefits — the loop used to be serial (R3-P-02), so an 8-directory
# session paid 8 × ~4s LLM latency. Three concurrent derives cuts this
# to roughly ``ceil(N/3) × derive_latency`` without saturating downstream
# LLM rate limits at the typical benchmark fan-out.
_DIRECTORY_DERIVE_CONCURRENCY = 3
_RECOMPOSE_CLUSTER_JACCARD_THRESHOLD = 0.15
_COARSE_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COARSE_HUMAN_DATE_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]+,\s+\d{4}$")
_COARSE_WEEKDAY_RE = re.compile(
    r"^(?:周[一二三四五六日天]|星期[一二三四五六日天]|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)$",
    re.IGNORECASE,
)

# Type aliases — all internal state keyed by these to prevent cross-collection collision
SessionKey = Tuple[str, str, str, str]  # (collection, tenant_id, user_id, session_id)


class RecompositionError(Exception):
    """Raised by ``_run_full_session_recomposition`` with raise_on_error=True
    when work fails partway through.

    Carries the directory URIs that were already created before the
    failure so the caller can register them with its run-scoped cleanup
    tracker (REVIEW REL-02). Without this, raise_on_error=True callers
    saw the inner ``contextlib.suppress`` best-effort cleanup as a
    silent black box: any URI that failed to delete inside the
    suppressed block became an orphan with no signal to the outer
    layer.

    Callers should drain ``created_uris`` into their tracker, then
    ``raise exc.original from exc`` (or just ``raise``) so the outer
    handler still sees the underlying failure.
    """

    def __init__(
        self,
        original: BaseException,
        created_uris: List[str],
    ) -> None:
        super().__init__(str(original))
        self.original = original
        self.created_uris = list(created_uris)


class SourceConflictError(Exception):
    """Same session_id ingested twice with a different transcript.

    Surfaced by ``_persist_rendered_conversation_source`` (and only on
    the benchmark path) so the HTTP layer can translate it into a 409
    Conflict that includes both the existing and supplied transcript
    hashes. Letting the second transcript silently overwrite the first
    would mix two unrelated benchmark runs into the same source URI
    while the merged-leaf URIs (deterministic on msg_range) only happen
    to coincide on prefix — recall results would be undefined.
    """

    def __init__(self, *, existing_hash: str, supplied_hash: str) -> None:
        super().__init__(
            "Conversation transcript conflict for benchmark session: "
            f"existing_hash={existing_hash} supplied_hash={supplied_hash}"
        )
        self.existing_hash = existing_hash
        self.supplied_hash = supplied_hash


@dataclass
class _BenchmarkRunCleanup:
    """Tracks URIs created during one benchmark ingest run for compensation.

    Each successful side effect (source persist, merged-leaf write,
    directory write, summary write) registers its URI here so a single
    failure or ``CancelledError`` can roll back every prior write —
    including the directory records the recomposition step creates,
    which the previous benchmark cleanup path was never told about
    (REVIEW Finding #6 / R2-09).

    ``compensate`` iterates URIs in reverse-creation order with per-item
    failure isolation: one bad ``remove`` does not abort the rest of
    the cleanup, and each failure is logged with the URI for
    post-mortem.
    """

    source_uri: Optional[str] = None
    merged_uris: List[str] = dc_field(default_factory=list)
    directory_uris: List[str] = dc_field(default_factory=list)
    summary_uri: Optional[str] = None

    async def compensate(self, manager: "ContextManager") -> None:
        """Best-effort rollback of every registered URI.

        Order: summary -> directories -> merged -> source. This mirrors
        the dependency graph: summary points at directories, directories
        at merged leaves, all of them at the source. Cleaning up in
        reverse keeps any in-flight reader from briefly seeing a
        partially-deleted graph.
        """
        for uri in [self.summary_uri] if self.summary_uri else []:
            await self._safe_remove(manager, uri)
        for uri in reversed(self.directory_uris):
            await self._safe_remove(manager, uri)
        for uri in reversed(self.merged_uris):
            await self._safe_remove(manager, uri)
        # source_uri is intentionally NOT removed by default. The
        # source is keyed deterministically on (tenant, user, session)
        # — leaving it lets a follow-up ingest with the same transcript
        # be idempotent (U5). On a failed run the next request either
        # carries the same hash and short-circuits, or carries a
        # different hash and gets 409.

    @staticmethod
    async def _safe_remove(manager: "ContextManager", uri: Optional[str]) -> None:
        """Remove a URI, logging and swallowing exceptions."""
        if not uri:
            return
        try:
            await manager._orchestrator.remove(uri)
        except Exception as exc:  # pragma: no cover - defensive log path
            logger.warning(
                "benchmark_ingest cleanup failed for %s: %s",
                uri,
                exc,
                exc_info=True,
            )


@dataclass
class ConversationBuffer:
    """Per-session buffer for conversation mode incremental chunking."""

    messages: list = dc_field(default_factory=list)
    token_count: int = 0
    start_msg_index: int = 0
    immediate_uris: list = dc_field(default_factory=list)
    tool_calls_per_turn: list = dc_field(default_factory=list)


class ContextManager:
    """Manages commit/end lifecycle for the context protocol.

    Args:
        orchestrator: MemoryOrchestrator instance.
        observer: Observer instance for transcript recording.
        session_idle_ttl: Session idle auto-close TTL in seconds (default 30min).
        idle_check_interval: Idle sweep interval in seconds (default 60s).
    """

    def __init__(
        self,
        orchestrator,  # MemoryOrchestrator (avoid circular import)
        observer,  # Observer
        *,
        session_idle_ttl: float = 1800.0,
        idle_check_interval: float = 60.0,
    ):
        self._orchestrator = orchestrator
        self._observer = observer

        # Session-scoped record queries (§25 Phase 5 — REVIEW closure
        # tracker U1). Constructed once per ContextManager so callers go
        # through a single gateway instead of reaching into the storage
        # adapter directly.
        self._session_records = SessionRecordsRepository(
            storage=orchestrator._storage,
            collection_resolver=orchestrator._get_collection,
        )

        # Benchmark ingest service (§25 Phase 3 — REVIEW closure
        # tracker U4). Lazy import avoids the manager <-> service
        # circular dependency: the service holds a back-ref to the
        # manager and calls many of its private helpers.
        from opencortex.context.benchmark_ingest_service import (
            BenchmarkConversationIngestService,
        )

        self._benchmark_ingest_service = BenchmarkConversationIngestService(
            manager=self, repo=self._session_records
        )

        # Committed turn_ids: {session_key: set(turn_id)}
        self._committed_turns: Dict[SessionKey, Set[str]] = {}
        # Session activity: {session_key: last_activity_timestamp}
        self._session_activity: Dict[SessionKey, float] = {}
        # Session-level locks: prevent concurrent begin_session
        self._session_locks: Dict[SessionKey, asyncio.Lock] = {}
        # Session-scoped merge locks: protect conversation buffer snapshots.
        self._session_merge_locks: Dict[SessionKey, asyncio.Lock] = {}
        # At most one background merge worker per session.
        self._session_merge_tasks: Dict[SessionKey, asyncio.Task] = {}
        self._session_merge_task_failures: Dict[SessionKey, List[BaseException]] = {}
        # Deferred follow-up tasks spawned by session merge workers.
        self._session_merge_followup_tasks: Dict[SessionKey, Set[asyncio.Task]] = {}
        self._session_merge_followup_failures: Dict[
            SessionKey, List[BaseException]
        ] = {}
        # At most one background full-session recomposition worker per session.
        self._session_full_recompose_tasks: Dict[SessionKey, asyncio.Task] = {}
        # Session project id snapshot for explicit/idle/background end flows.
        self._session_project_ids: Dict[SessionKey, str] = {}
        # Pending async tasks (cited_uris reward, etc.)
        self._pending_tasks: Set[asyncio.Task] = set()
        # Session-scoped flag for rare full immediate cleanup retries.
        self._session_pending_immediate_cleanup: Dict[SessionKey, bool] = {}
        # Conversation buffers: per-session incremental chunking
        self._conversation_buffers: Dict[SessionKey, ConversationBuffer] = {}
        # Semaphore limiting concurrent fire-and-forget deferred derives
        self._derive_semaphore = asyncio.Semaphore(3)
        # Hoisted from per-call construction in
        # ``_run_full_session_recomposition`` (REVIEW PERF-001 / KP-09):
        # a per-call semaphore meant U13's cross-conversation concurrency
        # multiplied the in-flight directory-derive count
        # (concurrency × 3). Instance-scoped enforces the same global cap
        # regardless of how many sessions are recomposing in parallel.
        self._directory_derive_semaphore = asyncio.Semaphore(
            _DIRECTORY_DERIVE_CONCURRENCY
        )
        # Lazy-initialized recomposition engine (survives ``__new__`` bypass).
        self._recomposition_engine_instance: Optional[Any] = None
        # Lazy-initialized commit service (survives ``__new__`` bypass).
        self._commit_service_instance: Optional[Any] = None
        # Lazy-initialized end service (survives ``__new__`` bypass).
        self._end_service_instance: Optional[Any] = None

        # Config
        self._session_idle_ttl = session_idle_ttl
        self._idle_check_interval = idle_check_interval

        # Background task
        self._idle_checker: Optional[asyncio.Task] = None

    @property
    def _recomposition_engine(self) -> "SessionRecompositionEngine":
        """Lazy-initialized recomposition engine (survives ``__new__`` bypass)."""
        inst = getattr(self, "_recomposition_engine_instance", None)
        if inst is None:
            from opencortex.context.recomposition_engine import (
                SessionRecompositionEngine,
            )

            inst = SessionRecompositionEngine(self)
            self._recomposition_engine_instance = inst
        return inst

    @property
    def _commit_service(self) -> "ContextCommitService":
        """Lazy-initialized commit coordinator."""
        inst = getattr(self, "_commit_service_instance", None)
        if inst is None:
            from opencortex.context.commit_service import ContextCommitService

            inst = ContextCommitService(self)
            self._commit_service_instance = inst
        return inst

    @property
    def _end_service(self) -> "ContextEndService":
        """Lazy-initialized end coordinator."""
        inst = getattr(self, "_end_service_instance", None)
        if inst is None:
            from opencortex.context.end_service import ContextEndService

            inst = ContextEndService(self)
            self._end_service_instance = inst
        return inst

    @staticmethod
    def _new_conversation_buffer() -> ConversationBuffer:
        """Create a fresh conversation buffer for commit coordination."""
        return ConversationBuffer()

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """Start background idle session sweeper."""
        self._idle_checker = asyncio.create_task(self._idle_session_loop())

    async def close(self) -> None:
        """Cancel idle checker and await pending tasks."""
        if self._idle_checker:
            self._idle_checker.cancel()
            try:
                await self._idle_checker
            except asyncio.CancelledError:
                pass
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
            self._pending_tasks.clear()

    # =========================================================================
    # Unified entry point
    # =========================================================================

    async def handle(
        self,
        session_id: str,
        phase: str,
        tenant_id: str,
        user_id: str,
        turn_id: Optional[str] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        cited_uris: Optional[List[str]] = None,
        config: Optional[Dict[str, Any]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Unified entry point for context commit/end lifecycle calls."""
        if phase == "prepare":
            raise ValueError("prepare phase has been removed; use intent/search APIs")

        if phase == "commit":
            if not turn_id:
                raise ValueError("turn_id is required for commit")
            if not messages or len(messages) < 2:
                raise ValueError("commit requires at least user + assistant messages")
            return await self._commit(
                session_id,
                turn_id,
                messages,
                tenant_id,
                user_id,
                cited_uris,
                tool_calls,
            )

        if phase == "end":
            return await self._end(session_id, tenant_id, user_id, config)

        raise ValueError(f"Unknown phase: {phase}")

    # =========================================================================
    # Phase: commit
    # =========================================================================

    @staticmethod
    def _merge_unique_strings(*groups: Any) -> List[str]:
        """Return a stable ordered union of non-empty string values."""
        from opencortex.context.recomposition_engine import (
            _merge_unique_strings as _impl,
        )

        return _impl(*groups)

    @staticmethod
    def _split_topic_values(raw_value: Any) -> List[str]:
        """Normalize topic-like values, splitting comma-separated strings."""
        from opencortex.context.recomposition_engine import (
            _split_topic_values as _impl,
        )

        return _impl(raw_value)

    @classmethod
    def _decorate_message_text(
        cls,
        text: str,
        meta: Optional[Dict[str, Any]],
    ) -> str:
        """Prefix stored text with the strongest available absolute time hint."""
        if not text:
            return text
        time_refs = cls._merge_unique_strings(
            (meta or {}).get("time_refs"),
            (meta or {}).get("event_date"),
        )
        if not time_refs:
            return text
        first_ref = time_refs[0]
        if first_ref in text:
            return text
        return f"[{first_ref}] {text}"

    @staticmethod
    def _conversation_source_uri(
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> str:
        """Return the stable transcript source URI for one conversation session."""
        from opencortex.utils.uri import CortexURI

        return CortexURI.build_private(
            tenant_id,
            user_id,
            "session",
            "conversations",
            session_id,
            "source",
        )

    @staticmethod
    def _session_summary_uri(
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> str:
        """Build the URI for a session-level summary record."""
        from opencortex.utils.uri import CortexURI

        return CortexURI.build_private(
            tenant_id,
            user_id,
            "session",
            "conversations",
            session_id,
            "summary",
        )

    @staticmethod
    def _merged_leaf_uri(
        tenant_id: str,
        user_id: str,
        session_id: str,
        msg_range: List[int],
    ) -> str:
        """Return one stable merged-leaf URI for a session message span."""
        from opencortex.context.recomposition_engine import SessionRecompositionEngine

        return SessionRecompositionEngine._merged_leaf_uri(
            tenant_id,
            user_id,
            session_id,
            msg_range,
        )

    @staticmethod
    def _directory_uri(
        tenant_id: str,
        user_id: str,
        session_id: str,
        index: int,
    ) -> str:
        """Return URI for a directory parent record."""
        from opencortex.context.recomposition_engine import SessionRecompositionEngine

        return SessionRecompositionEngine._directory_uri(
            tenant_id,
            user_id,
            session_id,
            index,
        )

    async def _run_full_session_recomposition(self, **kwargs) -> Optional[List[str]]:
        """Create directory parent records for semantically related leaf clusters."""
        return await self._recomposition_engine._run_full_session_recomposition(
            **kwargs,
        )

    def _build_recomposition_segments(
        self,
        entries: List[RecompositionEntry],
    ) -> List[Dict[str, Any]]:
        """Split ordered recomposition entries into bounded semantic segments."""
        return self._recomposition_engine._build_recomposition_segments(entries)

    async def _build_recomposition_entries(
        self,
        *,
        snapshot: "ConversationBuffer",
        immediate_records: List[Dict[str, Any]],
        tail_records: List[Dict[str, Any]],
    ) -> List[RecompositionEntry]:
        """Delegate recomposition entry building to the recomposition engine."""
        return await self._recomposition_engine._build_recomposition_entries(
            snapshot=snapshot,
            immediate_records=immediate_records,
            tail_records=tail_records,
        )

    def _build_anchor_clustered_segments(
        self,
        entries: List[RecompositionEntry],
    ) -> List[Dict[str, Any]]:
        """Delegate anchor clustering to the recomposition engine."""
        return self._recomposition_engine._build_anchor_clustered_segments(entries)

    async def _aggregate_records_metadata(
        self,
        records: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Delegate metadata aggregation to the recomposition engine."""
        return await self._recomposition_engine._aggregate_records_metadata(records)

    @classmethod
    def _render_conversation_source(
        cls,
        transcript: List[Dict[str, Any]],
    ) -> str:
        """Render a readable transcript source from observer messages."""
        lines: List[str] = []
        for message in transcript:
            role = str(message.get("role", "") or "").strip() or "unknown"
            content = str(message.get("content", "") or "").strip()
            if not content:
                continue
            meta = message.get("meta")
            if isinstance(meta, dict):
                content = cls._decorate_message_text(content, meta)
            lines.append(f"[{role}] {content}")
        return "\n".join(lines)

    async def _persist_conversation_source(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> Optional[str]:
        """Persist the stable transcript source for a session if transcript exists."""
        transcript = self._observer.get_transcript(
            self._observer_session_id(
                session_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
        )
        if not transcript:
            return None

        return await self._persist_rendered_conversation_source(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            transcript=transcript,
        )

    @staticmethod
    def _canonicalize_for_hash(value: Any) -> Any:
        """Recursively canonicalize a value so benign reordering does not
        change the digest (REVIEW ADV-006).

        - ``dict`` values: recurse on each entry. Key ordering is handled
          downstream by ``OPT_SORT_KEYS`` during serialization.
        - ``list`` values: when every element is a primitive
          (``str | int | float | bool | None``), sort the list — these
          are typically anchor sets like ``time_refs`` whose order is
          not semantic. When elements are dicts (e.g. ``tool_calls``),
          leave order intact: the sequence carries meaning.
        - Everything else: returned as-is. Strings, numbers, None, and
          any non-list/dict objects pass through unchanged.
        """
        if isinstance(value, dict):
            return {
                k: ContextManager._canonicalize_for_hash(v) for k, v in value.items()
            }
        if isinstance(value, list):
            if all(
                isinstance(item, (str, int, float, bool, type(None))) for item in value
            ):
                # Sort by string projection to keep mixed-type lists stable;
                # primitive lists in benchmark meta are almost always
                # homogeneous strings.
                return sorted(value, key=lambda x: (x is None, str(x)))
            return [ContextManager._canonicalize_for_hash(item) for item in value]
        return value

    @staticmethod
    def _hash_transcript(transcript: List[Dict[str, Any]]) -> str:
        """SHA-256 over the canonical normalized transcript shape.

        Message order is semantic and preserved as-is. Inside each
        message's ``meta`` dict, list values that contain only
        primitives are sorted so benign reordering of e.g. ``time_refs``
        does not produce a false 409 conflict on benchmark replay
        (REVIEW ADV-006). Lists of dicts (``tool_calls``) keep their
        sequence — order is treated as semantic for those.
        """
        normalized = [
            {
                "role": str(message.get("role", "") or ""),
                "content": str(message.get("content", "") or ""),
                "meta": ContextManager._canonicalize_for_hash(
                    message.get("meta") or {}
                ),
            }
            for message in transcript
        ]
        digest = hashlib.sha256()
        digest.update(json.dumps(normalized, option=json.OPT_SORT_KEYS))
        return digest.hexdigest()

    async def _persist_rendered_conversation_source(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        transcript: List[Dict[str, Any]],
        enforce_transcript_hash: bool = False,
    ) -> Optional[str]:
        """Persist one transcript payload as the stable conversation source.

        ``enforce_transcript_hash`` is the benchmark-only knob (U5). When
        True the helper:

        - Attaches ``transcript_hash`` to the source meta on first write.
        - On re-ingest with the same hash, returns the existing URI
          (idempotent hit, no rewrite).
        - On re-ingest with a different hash, raises
          ``SourceConflictError`` so the caller can map it to HTTP 409.

        Production callers leave ``enforce_transcript_hash=False`` so
        existing context_end / context_commit lifecycle behavior is
        preserved unchanged.
        """
        if not transcript:
            return None

        source_uri = self._conversation_source_uri(tenant_id, user_id, session_id)
        existing = await self._orchestrator._get_record_by_uri(source_uri)
        if existing and not enforce_transcript_hash:
            return source_uri

        supplied_hash = (
            self._hash_transcript(transcript) if enforce_transcript_hash else ""
        )

        if existing and enforce_transcript_hash:
            existing_meta = dict(existing.get("meta") or {})
            existing_hash = str(existing_meta.get("transcript_hash") or "").strip()
            if existing_hash and existing_hash == supplied_hash:
                # Same transcript replayed: short-circuit. The caller
                # treats this as an idempotent hit and skips leaf/recompose
                # rewrite — pre-existing merged records still resolve.
                return source_uri
            if existing_hash and existing_hash != supplied_hash:
                raise SourceConflictError(
                    existing_hash=existing_hash,
                    supplied_hash=supplied_hash,
                )
            # Existing source has no hash recorded (legacy / production
            # write). Treat as idempotent — refusing here would block
            # benchmark replays of pre-versioning sessions.
            return source_uri

        content = self._render_conversation_source(transcript)
        if not content:
            return None

        meta: Dict[str, Any] = {
            "layer": "conversation_source",
            "session_id": session_id,
            "message_count": len(transcript),
        }
        if supplied_hash:
            meta["transcript_hash"] = supplied_hash

        await self._orchestrator.add(
            uri=source_uri,
            abstract=f"Conversation transcript for {session_id}",
            content=content,
            category="documents",
            context_type="resource",
            is_leaf=False,
            session_id=session_id,
            meta=meta,
        )
        # REVIEW closure tracker R2-23 / R4-P2-6 — orchestrator.add()
        # already schedules the CortexFS write as a fire-and-forget
        # task (see orchestrator.py: ``asyncio.create_task(self._fs.
        # write_context(...))`` after the storage upsert). The previous
        # explicit follow-up ``await self._orchestrator._fs.write_context(...)``
        # here was a redundant double-write — same uri, same content —
        # that doubled the FS I/O for source persistence and could race
        # with the scheduled task on slow filesystems.
        return source_uri

    async def _mark_source_run_complete(self, source_uri: str) -> None:
        """Set ``meta.run_complete=True`` on a benchmark source record.

        REVIEW F5 / ADV-007: written only after a benchmark ingest run
        finishes successfully. The idempotent-hit path treats a
        hash-match WITHOUT this marker as a torn prior run (compensate
        partially failed, leaving stale leaves) and re-ingests after
        purging, instead of silently returning the partial set. The
        marker write goes through the lower-level storage update so we
        avoid the heavy ``MemoryOrchestrator.update`` re-derive path —
        this is meta-only.
        """
        if not source_uri:
            return
        try:
            records = await self._orchestrator._storage.filter(
                self._orchestrator._get_collection(),
                {"op": "must", "field": "uri", "conds": [source_uri]},
                limit=1,
            )
            if not records:
                return
            record = records[0]
            record_id = str(record.get("id", "") or "")
            if not record_id:
                return
            existing_meta = dict(record.get("meta") or {})
            existing_meta["run_complete"] = True
            await self._orchestrator._storage.update(
                self._orchestrator._get_collection(),
                record_id,
                {"meta": existing_meta},
            )
        except Exception as exc:  # pragma: no cover - defensive log path
            logger.warning(
                "benchmark_ingest: failed to mark source %s run_complete: %s",
                source_uri,
                exc,
                exc_info=True,
            )

    async def _purge_torn_benchmark_run(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: str,
        merged_records: List[Dict[str, Any]],
    ) -> None:
        """Drop stale records left by a prior failed benchmark ingest run.

        Used when the idempotent-hit path detects a hash-match without
        the ``run_complete`` marker (REVIEW F5). Removes:

        - merged leaves under the source (the records we already loaded)
        - directory records under the source (re-loaded here)
        - the deterministic session_summary record if one exists

        The source record itself stays — it is keyed deterministically
        on (tenant, user, session_id) and the next ingest will rewrite
        the transcript_hash and ``run_complete`` marker fresh.
        """
        merged_uris = [
            str(rec.get("uri", "") or "").strip()
            for rec in merged_records
            if rec.get("uri")
        ]
        # REVIEW closure tracker ADV-U-001: every other repo call site
        # threads (tenant_id, user_id) through so cross-tenant
        # ``session_id`` collisions cannot bleed across scope. Without
        # the kwargs the directory query loaded *every* directory record
        # carrying this ``session_id`` regardless of who owned it, and
        # the URI list flowed straight into ``_purge_records_and_fs_subtree``
        # — a hard-delete path that walks URI prefixes without a layer
        # check. Pin scope here so torn-replay purge can never touch
        # another tenant's records.
        #
        # REVIEW closure tracker REL-01: the outer ``except Exception``
        # used to also swallow ``SessionRecordOverflowError`` (subclass
        # of Exception). That meant a directory query exceeding the
        # repo's safety cap would silently treat the directory list as
        # empty, leaving every directory-layer record from the failed
        # prior run as a permanent storage orphan with no operator
        # signal. Surface overflow explicitly so the admin route maps it
        # to 507 instead.
        try:
            directory_records = await self._session_records.load_directories(
                session_id=session_id,
                source_uri=source_uri,
                tenant_id=tenant_id,
                user_id=user_id,
            )
        except SessionRecordOverflowError:
            logger.error(
                "benchmark_ingest: torn-run purge aborted — directory "
                "query exceeded safety cap sid=%s source=%s. Directory "
                "records from the failed prior run remain in storage; "
                "rotate session_id or page manually before re-ingesting.",
                session_id,
                source_uri,
            )
            raise
        except Exception:  # pragma: no cover - defensive
            directory_records = []
        directory_uris = [
            str(rec.get("uri", "") or "").strip()
            for rec in directory_records
            if rec.get("uri")
        ]
        summary_uri = self._session_summary_uri(tenant_id, user_id, session_id)

        all_uris = [u for u in (merged_uris + directory_uris + [summary_uri]) if u]
        if not all_uris:
            return
        logger.info(
            "benchmark_ingest: purging torn prior run sid=%s source=%s "
            "merged=%d directories=%d summary=%s",
            session_id,
            source_uri,
            len(merged_uris),
            len(directory_uris),
            "present" if summary_uri else "absent",
        )
        with contextlib.suppress(Exception):
            await self._purge_records_and_fs_subtree(all_uris)

    @classmethod
    def _benchmark_segment_meta(
        cls,
        messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Aggregate segment-level anchors for offline benchmark leaves."""
        entities: List[str] = []
        topics: List[str] = []
        time_refs: List[str] = []
        event_date = ""
        tool_calls: List[Dict[str, Any]] = []
        benchmark_meta: Dict[str, Any] = {}

        for message in messages:
            meta = dict(message.get("meta") or {})
            for key, value in meta.items():
                if key.startswith("lme_") and value not in (None, ""):
                    if key not in benchmark_meta:
                        benchmark_meta[key] = value
                    elif benchmark_meta[key] != value:
                        benchmark_meta[key] = cls._merge_unique_strings(
                            benchmark_meta[key],
                            value,
                        )
            entities = cls._merge_unique_strings(
                entities,
                meta.get("entities"),
            )
            topics = cls._merge_unique_strings(
                topics,
                meta.get("topics"),
            )
            time_refs = cls._merge_unique_strings(
                time_refs,
                meta.get("time_refs"),
                meta.get("event_date"),
            )
            if not event_date:
                event_date = str(meta.get("event_date") or "").strip()
            for call in meta.get("tool_calls", []) or []:
                if isinstance(call, dict):
                    tool_calls.append(call)

        aggregated: Dict[str, Any] = {}
        if entities:
            aggregated["entities"] = entities
        if topics:
            aggregated["topics"] = topics
        if time_refs:
            aggregated["time_refs"] = time_refs
        if event_date:
            aggregated["event_date"] = event_date
        if tool_calls:
            aggregated["tool_calls"] = tool_calls
        aggregated.update(benchmark_meta)
        return aggregated

    @staticmethod
    def _export_memory_record(
        record: Dict[str, Any],
        *,
        hydrated_content: str = "",
    ) -> Dict[str, Any]:
        """Return one adapter-friendly memory payload from a stored record.

        ``Context.to_dict`` does not include the L2 content (it lives in
        CortexFS, not on the Qdrant payload). Callers that need raw
        content for benchmark scoring or recall fallback must pre-hydrate
        it from the filesystem and pass ``hydrated_content``; otherwise
        the field falls back to whatever the stored record carries (or
        empty string).
        """
        meta = dict(record.get("meta") or {})
        content = hydrated_content or str(record.get("content", "") or "")
        return {
            "uri": str(record.get("uri", "") or ""),
            "abstract": str(record.get("abstract", "") or ""),
            "overview": str(record.get("overview", "") or ""),
            "content": content,
            "meta": meta,
            "abstract_json": record.get("abstract_json", {}),
            "session_id": str(record.get("session_id", "") or ""),
            "speaker": str(record.get("speaker", "") or ""),
            "event_date": record.get("event_date", ""),
            "msg_range": meta.get("msg_range"),
            "recomposition_stage": meta.get("recomposition_stage"),
            "source_uri": meta.get("source_uri"),
        }

    async def _hydrate_record_contents(
        self,
        records: List[Dict[str, Any]],
        overrides: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """Return URI -> L2 content for a record set.

        ``overrides`` short-circuits the FS read for URIs already in
        memory (REVIEW KP-06). The benchmark merged_recompose path
        passes the in-memory write-time map so the response avoids
        racing the orchestrator's fire-and-forget CortexFS write; the
        direct_evidence path passes its own captured map. Records whose
        URI is not in ``overrides`` go through the existing CortexFS
        ``read_file(uri/content.md)`` path; missing files map to
        empty string so a single FS hiccup does not fail the response.

        Returned dict contains every URI extracted from ``records`` so
        the caller can do a flat ``hydrated.get(uri, "")`` instead of
        layering 3 fallback dicts at the comprehension site.
        """
        overrides = overrides or {}
        uris = [
            str(record.get("uri", "") or "").strip()
            for record in records
            if record.get("uri")
        ]
        if not uris:
            return {}

        result: Dict[str, str] = {
            uri: overrides[uri] for uri in uris if uri in overrides
        }
        missing = [uri for uri in uris if uri not in overrides]
        if not missing:
            return result

        fs = getattr(self._orchestrator, "_fs", None)
        if fs is None:
            for uri in missing:
                result[uri] = ""
            return result

        async def _read_one(uri: str) -> Tuple[str, str]:
            """Read L2 content for a single URI, returning (uri, content)."""
            try:
                return uri, await fs.read_file(f"{uri}/content.md")
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "benchmark_ingest content hydration failed for %s: %s",
                    uri,
                    exc,
                )
                return uri, ""

        fs_results = await asyncio.gather(*[_read_one(u) for u in missing])
        result.update(dict(fs_results))
        return result

    def _benchmark_recomposition_entries(
        self,
        normalized_segments: List[List[Dict[str, Any]]],
    ) -> List[RecompositionEntry]:
        """Build message-level entries for benchmark offline chunking.

        Each entry is tagged with its source input-segment index so
        ``_build_recomposition_segments`` can hard-split at input-segment
        boundaries (REVIEW closure tracker R3-RC-02 / R2-14).
        """
        entries: List[RecompositionEntry] = []
        msg_index = 0
        for segment_index, segment in enumerate(normalized_segments):
            segment_meta = self._benchmark_segment_meta(segment)
            for message in segment:
                # Segment-level aggregation (entities/topics/time_refs/
                # event_date/tool_calls/lme_*) MUST win over per-message
                # meta — the aggregation is the canonical anchor source
                # for the merged leaf. Inverted from the original order
                # to fix R2-03: the prior {**segment_meta, **message_meta}
                # silently let one message's narrow meta overwrite the
                # whole segment's anchors, leaving _benchmark_segment_meta
                # as dead code on the hot path.
                meta = {
                    **dict(message.get("meta") or {}),
                    **segment_meta,
                }
                rendered = self._decorate_message_text(
                    str(message.get("content", "") or ""),
                    meta,
                )
                if not rendered:
                    continue
                record = {
                    "uri": "",
                    "abstract": rendered,
                    "content": rendered,
                    "overview": "",
                    "meta": {
                        **meta,
                        "msg_range": [msg_index, msg_index],
                    },
                    "keywords": ", ".join(
                        str(topic)
                        for topic in self._merge_unique_strings(meta.get("topics"))
                    ),
                    "entities": self._merge_unique_strings(meta.get("entities")),
                }
                entries.append(
                    RecompositionEntry(
                        text=rendered,
                        uri="",
                        msg_start=msg_index,
                        msg_end=msg_index,
                        token_count=max(self._estimate_tokens(rendered), 1),
                        anchor_terms=self._segment_anchor_terms(record),
                        time_refs=self._segment_time_refs(record),
                        source_record=record,
                        immediate_uris=[],
                        superseded_merged_uris=[],
                        source_segment_index=segment_index,
                    )
                )
                msg_index += 1
        return entries

    async def benchmark_ingest_conversation(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        segments: List[List[Dict[str, Any]]],
        include_session_summary: bool = True,
        ingest_shape: str = "merged_recompose",
    ) -> Dict[str, Any]:
        """Benchmark-only offline conversation ingest.

        §25 Phase 3 — REVIEW closure tracker U4. The orchestration body
        lives in BenchmarkConversationIngestService. This thin shim
        preserves the ContextManager-rooted call path so existing
        callers (admin route, tests) keep working unchanged.
        """
        return await self._benchmark_ingest_service.ingest(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            segments=segments,
            include_session_summary=include_session_summary,
            ingest_shape=ingest_shape,
        )

    @staticmethod
    def _benchmark_evidence_uri(
        tenant_id: str,
        user_id: str,
        session_id: str,
        segment_index: int,
        msg_range: List[int],
    ) -> str:
        """Return a stable benchmark evidence URI for one direct segment."""
        tenant = tenant_id or "public"
        user = user_id or "default"
        return (
            f"opencortex://{tenant}/{user}/memory/events/{session_id}/"
            f"benchmark_evidence_{segment_index}_{msg_range[0]}_{msg_range[1]}"
        )

    def _segment_anchor_terms(self, record: Dict[str, Any]) -> Set[str]:
        """Extract coarse anchor terms used for sequential merge boundaries."""
        return self._recomposition_engine._segment_anchor_terms(record)

    def _segment_time_refs(self, record: Dict[str, Any]) -> Set[str]:
        return self._recomposition_engine._segment_time_refs(record)

    async def _commit(
        self,
        session_id: str,
        turn_id: str,
        messages: List[Dict[str, str]],
        tenant_id: str,
        user_id: str,
        cited_uris: Optional[List[str]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Commit a turn by writing messages and triggering merge/recomposition.

        Args:
            session_id: Active session identifier.
            turn_id: Turn within the session.
            messages: Messages to commit.
            tenant_id: Tenant identifier.
            user_id: User identifier.
            cited_uris: URIs cited during the turn for reward scoring.
            tool_calls: Tool call records for the turn.

        Returns:
            Dict with commit status and metadata.
        """
        return await self._commit_service.commit(
            session_id=session_id,
            turn_id=turn_id,
            messages=messages,
            tenant_id=tenant_id,
            user_id=user_id,
            cited_uris=cited_uris,
            tool_calls=tool_calls,
        )

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Estimate token count for merge threshold."""
        from opencortex.parse.base import estimate_tokens

        return estimate_tokens(text)

    def _spawn_merge_task(
        self,
        sk: SessionKey,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        """Start one background merge worker for the session if needed."""
        return self._recomposition_engine._spawn_merge_task(
            sk,
            session_id,
            tenant_id,
            user_id,
        )

    def _spawn_full_recompose_task(
        self,
        sk: SessionKey,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: Optional[str],
        raise_on_error: bool = False,
    ) -> None:
        """Start one async full-session recomposition worker per session."""
        return self._recomposition_engine._spawn_full_recompose_task(
            sk,
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            source_uri=source_uri,
            raise_on_error=raise_on_error,
        )

    async def _generate_session_summary(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: Optional[str],
    ) -> Optional[str]:
        """Generate a session-level summary from directory abstracts."""
        return await self._recomposition_engine._generate_session_summary(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            source_uri=source_uri,
        )

    def _merge_trigger_threshold(self) -> int:
        """Return the token threshold that triggers a background merge."""
        return self._recomposition_engine._merge_trigger_threshold()

    async def _wait_for_merge_task(self, sk: SessionKey) -> List[BaseException]:
        """Wait until any in-flight background merge for the session finishes."""
        return await self._recomposition_engine._wait_for_merge_task(sk)

    async def _wait_for_merge_followup_tasks(
        self, sk: SessionKey
    ) -> List[BaseException]:
        """Wait until deferred follow-up tasks for the session merge finish."""
        return await self._recomposition_engine._wait_for_merge_followup_tasks(sk)

    async def _purge_records_and_fs_subtree(self, uris: List[str]) -> None:
        """Purge each URI's record and CortexFS subtree by URI prefix."""
        return await self._recomposition_engine._purge_records_and_fs_subtree(uris)

    async def _list_immediate_uris(self, session_id: str) -> List[str]:
        """Return current session immediate source URIs for fallback cleanup."""
        return await self._recomposition_engine._list_immediate_uris(session_id)

    async def _merge_buffer(
        self,
        sk: SessionKey,
        session_id: str,
        tenant_id: str,
        user_id: str,
        *,
        flush_all: bool,
        collection_name: Optional[str] = None,
        raise_on_error: bool = False,
    ) -> None:
        """Merge accumulated buffer snapshots into durable merged records."""
        return await self._recomposition_engine._merge_buffer(
            sk,
            session_id,
            tenant_id,
            user_id,
            flush_all=flush_all,
            collection_name=collection_name,
            raise_on_error=raise_on_error,
        )

    # =========================================================================

    async def _end(
        self,
        session_id: str,
        tenant_id: str,
        user_id: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """End a session, flushing buffers and triggering trace processing.

        Args:
            session_id: Session to close.
            tenant_id: Tenant identifier.
            user_id: User identifier.
            config: Optional override configuration (e.g. fail_fast_end).

        Returns:
            Dict with session end status, trace count, and timing.
        """
        return await self._end_service.end(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            config=config,
        )

    # =========================================================================
    # Session state helpers
    # =========================================================================

    def _current_collection_name(self) -> str:
        """Return the active storage collection for the current request context."""
        return self._orchestrator._get_collection()

    def _make_session_key(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> SessionKey:
        """Build a (collection, tenant, user, session) tuple for session state lookup."""
        return (
            self._current_collection_name(),
            tenant_id,
            user_id,
            session_id,
        )

    def _observer_session_id(
        self,
        session_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> str:
        """Return the observer-only session namespace for the active collection."""
        return self._orchestrator._observer_session_id(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )

    def _touch_session(self, sk: SessionKey) -> None:
        """Record the current time as last activity for a session."""
        self._session_activity[sk] = time.time()

    def _remember_session_project(self, sk: SessionKey) -> None:
        """Snapshot the current project ID for a session."""
        self._session_project_ids[sk] = get_effective_project_id()

    def _cleanup_session(self, sk: SessionKey) -> None:
        """Remove all session state including cache entries via reverse index."""
        self._committed_turns.pop(sk, None)
        self._session_activity.pop(sk, None)
        self._session_locks.pop(sk, None)
        self._session_merge_locks.pop(sk, None)
        self._session_merge_tasks.pop(sk, None)
        self._session_merge_task_failures.pop(sk, None)
        self._session_merge_followup_tasks.pop(sk, None)
        self._session_merge_followup_failures.pop(sk, None)
        self._conversation_buffers.pop(sk, None)
        self._session_project_ids.pop(sk, None)
        self._session_pending_immediate_cleanup.pop(sk, None)

    # =========================================================================
    # Idle session auto-close
    # =========================================================================

    async def _idle_session_loop(self) -> None:
        """Periodic sweep to auto-close idle sessions."""
        while True:
            await asyncio.sleep(self._idle_check_interval)
            now = time.time()
            expired = [
                sk
                for sk, ts in self._session_activity.items()
                if now - ts > self._session_idle_ttl
            ]
            for sk in expired:
                collection, tid, uid, sid = sk
                logger.info(
                    "[ContextManager] idle-close sid=%s (collection=%s tenant=%s, user=%s)",
                    sid,
                    collection,
                    tid,
                    uid,
                )
                try:
                    # Set contextvars for orchestrator.session_end()
                    tokens = set_request_identity(tid, uid)
                    try:
                        await self._end(sid, tid, uid)
                    finally:
                        reset_request_identity(tokens)
                except Exception as exc:
                    logger.warning(
                        "[ContextManager] Auto-close failed for %s: %s",
                        sid,
                        exc,
                    )

    # =========================================================================
    # Fallback log
    # =========================================================================

    def _write_fallback(
        self,
        session_id: str,
        turn_id: str,
        messages: List[Dict[str, str]],
        tenant_id: str,
        user_id: str,
    ) -> None:
        """Write commit messages to fallback JSONL when Observer fails."""
        try:
            data_root = self._orchestrator._config.data_root
            fallback_path = Path(data_root) / "commit_fallback.jsonl"
            entry = {
                "session_id": session_id,
                "turn_id": turn_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "messages": messages,
                "timestamp": time.time(),
            }
            with open(fallback_path, "ab") as f:
                f.write(json.dumps(entry) + b"\n")
        except Exception as exc:
            logger.error(
                "[ContextManager] Failed to write fallback log: %s",
                exc,
            )

    # =========================================================================
    # Reward scoring for cited URIs
    # =========================================================================

    async def _apply_cited_rewards(self, uris: List[str]) -> None:
        """Apply +0.1 reward to each cited memory URI."""
        for uri in uris:
            try:
                await self._orchestrator.feedback(uri=uri, reward=0.1)
            except Exception as exc:
                logger.debug(
                    "[ContextManager] Reward feedback failed for %s: %s",
                    uri,
                    exc,
                )
