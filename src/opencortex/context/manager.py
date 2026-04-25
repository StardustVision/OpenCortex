# SPDX-License-Identifier: Apache-2.0
"""ContextManager — three-phase lifecycle for the Memory Context Protocol.

Manages prepare/commit/end phases for platform-agnostic memory recall and
session recording.  Replaces Claude Code hooks with a single MCP tool.

Design doc: docs/memory-context-protocol.md v1.2
"""

import asyncio
import contextlib
import hashlib
import logging
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import orjson as json

from opencortex.http.request_context import (
    get_collection_name,
    get_effective_project_id,
    reset_collection_name,
    reset_request_identity,
    reset_request_project_id,
    set_collection_name,
    set_request_identity,
    set_request_project_id,
)
from opencortex.context.recomposition_types import RecompositionEntry
from opencortex.context.session_records import (
    SessionRecordOverflowError,
    SessionRecordsRepository,
    record_msg_range,
    record_text,
)
from opencortex.intent import RetrievalPlan, SearchResult
from opencortex.intent.retrieval_support import build_probe_scope_input
from opencortex.intent.timing import StageTimingCollector, measure_async, measure_sync
from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
)
from opencortex.utils.text import smart_truncate

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
CacheKey = Tuple[
    str, str, str, str, str
]  # (collection, tenant_id, user_id, session_id, turn_id)


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


@dataclass
class PrepareOptions:
    """Normalized prepare-phase inputs used across helper boundaries."""

    max_items: int
    detail_level_override: Optional[str]
    recall_mode: str
    category: Optional[str]
    context_type: Optional[ContextType]
    include_knowledge: bool
    session_scope_enabled: bool
    session_context: Optional[Dict[str, str]]


@dataclass
class PreparePlanningState:
    """Recall-planning outputs needed by retrieval and result assembly."""

    query: str
    probe_result: SearchResult
    retrieve_plan: Optional[RetrievalPlan]
    runtime_bound: Dict[str, Any]
    intent_ms: int


@dataclass
class PrepareRetrievalState:
    """Retrieved prepare payloads and runtime attribution."""

    memory_items: List[Dict[str, Any]] = dc_field(default_factory=list)
    knowledge_items: List[Dict[str, Any]] = dc_field(default_factory=list)
    memory_ms: int = 0
    knowledge_ms: int = 0
    memory_runtime_trace: Dict[str, Any] = dc_field(default_factory=dict)


class ContextManager:
    """Manages the prepare/commit/end lifecycle for memory_context protocol.

    Args:
        orchestrator: MemoryOrchestrator instance.
        observer: Observer instance for transcript recording.
        prepare_cache_ttl: Prepare result cache TTL in seconds (default 5min).
        session_idle_ttl: Session idle auto-close TTL in seconds (default 30min).
        idle_check_interval: Idle sweep interval in seconds (default 60s).
        max_content_chars: Per-item content hard limit (default 50k chars).
    """

    def __init__(
        self,
        orchestrator,  # MemoryOrchestrator (avoid circular import)
        observer,  # Observer
        *,
        prepare_cache_ttl: float = 300.0,
        session_idle_ttl: float = 1800.0,
        idle_check_interval: float = 60.0,
        max_content_chars: int = 50_000,
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

        # Prepare cache: {(collection, tid, uid, sid, turn_id): (result, timestamp)}
        self._prepare_cache: Dict[CacheKey, Tuple[Dict, float]] = {}
        # Reverse index: {session_key: set(cache_key)} — for end cleanup
        self._session_cache_keys: Dict[SessionKey, Set[CacheKey]] = {}
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
        # Session-scoped memory owner ids recalled during prepare().
        self._session_memory_owner_ids: Dict[SessionKey, Set[str]] = {}
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
        # Skill selection tracking: (session_key, turn_id) -> set of skill URIs
        # Turn-scoped to prevent stale selections leaking across turns
        self._selected_skill_uris: Dict[tuple, Set[str]] = {}

        # Config
        self._prepare_cache_ttl = prepare_cache_ttl
        self._session_idle_ttl = session_idle_ttl
        self._idle_check_interval = idle_check_interval
        self._max_content_chars = max_content_chars

        # Background task
        self._idle_checker: Optional[asyncio.Task] = None

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
        """Unified entry point — dispatches to prepare/commit/end."""
        if phase == "prepare":
            if not turn_id:
                raise ValueError("turn_id is required for prepare")
            if not messages or not any(m.get("role") == "user" for m in messages):
                raise ValueError("prepare requires at least one user message")
            return await self._prepare(
                session_id,
                turn_id,
                messages,
                tenant_id,
                user_id,
                config,
            )

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
    # Phase: prepare
    # =========================================================================

    @staticmethod
    def _safe_detail_level(value: Optional[str]) -> DetailLevel:
        """Coerce detail level strings without raising on invalid input."""
        try:
            return DetailLevel(value or "l1")
        except (TypeError, ValueError):
            return DetailLevel.L1

    @staticmethod
    def _safe_context_type(value: Optional[str]) -> Optional[ContextType]:
        """Coerce context type strings without raising on invalid input."""
        if not value:
            return None
        try:
            return ContextType(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_max_items(value: Any, *, default: int = 5, maximum: int = 20) -> int:
        """Coerce max_items without raising on invalid input."""
        try:
            return min(max(int(value), 1), maximum)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _extract_skill_uris(skills: Any) -> List[str]:
        """Best-effort skill URI extraction that ignores malformed items."""
        skill_uris: List[str] = []
        for skill in skills or []:
            uri = getattr(skill, "uri", None)
            if uri is None and isinstance(skill, dict):
                uri = skill.get("uri")
            if isinstance(uri, str) and uri:
                skill_uris.append(uri)
        return skill_uris

    def _build_prepare_options(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        config: Optional[Dict[str, Any]],
    ) -> PrepareOptions:
        """Normalize raw prepare config into a stable typed container."""
        config = config or {}
        context_type = self._safe_context_type(config.get("context_type"))
        # Priority: client explicit > server config > default False.
        server_default = False
        if hasattr(self._orchestrator, "_config") and self._orchestrator._config:
            server_default = (
                self._orchestrator._config.cortex_alpha.knowledge_recall_enabled
            )
        session_scope_enabled = bool(config.get("session_scope", False))
        session_context = None
        if session_scope_enabled:
            session_context = {
                "session_id": session_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
            }
        return PrepareOptions(
            max_items=self._safe_max_items(config.get("max_items", 5)),
            detail_level_override=config.get("detail_level"),
            recall_mode=config.get("recall_mode", "auto"),
            category=config.get("category"),
            context_type=context_type,
            include_knowledge=config.get("include_knowledge", server_default),
            session_scope_enabled=session_scope_enabled,
            session_context=session_context,
        )

    @staticmethod
    def _prepare_category_filter(category: Optional[str]) -> Optional[Dict[str, Any]]:
        """Build the category metadata filter for prepare-time recall."""
        if not category:
            return None
        return {"op": "must", "field": "category", "conds": [category]}

    async def _ensure_prepare_session(
        self,
        *,
        sk: SessionKey,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        """Touch session state and lazily auto-create the observer session."""
        self._touch_session(sk)
        self._remember_session_project(sk)
        lock = self._session_locks.setdefault(sk, asyncio.Lock())
        observer_session_id = self._observer_session_id(
            session_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        async with lock:
            if observer_session_id not in self._observer.active_sessions():
                self._observer.begin_session(
                    observer_session_id,
                    tenant_id,
                    user_id,
                )

    async def _plan_prepare_recall(
        self,
        *,
        query: str,
        session_id: str,
        turn_id: str,
        tenant_id: str,
        user_id: str,
        options: PrepareOptions,
        stage_timings: StageTimingCollector,
    ) -> PreparePlanningState:
        """Run probe/planner/runtime bind and keep no-recall baseline on failures."""
        detail_level = self._safe_detail_level(options.detail_level_override)
        probe_result = SearchResult(should_recall=False)
        retrieve_plan: Optional[RetrievalPlan] = None
        runtime_bound: Dict[str, Any] = {
            "probe": probe_result.to_dict(),
            "planner": None,
            "sources": [],
            "context_types": [],
            "category_filter": [],
            "memory_limit": 0,
            "knowledge_limit": 0,
            "planned_depth": detail_level.value,
            "effective_depth": detail_level.value,
            "association_mode": "off",
            "rerank": False,
            "hydration_allowed": detail_level != DetailLevel.L2,
            "scope": {},
            "degrade": {"applied": False, "reasons": [], "actions": []},
        }
        if options.recall_mode != "never":
            try:
                probe_result = await asyncio.wait_for(
                    measure_async(
                        stage_timings,
                        "probe",
                        self._orchestrator.probe_memory,
                        query,
                        context_type=options.context_type,
                        session_context=options.session_context,
                        metadata_filter=self._prepare_category_filter(options.category),
                    ),
                    timeout=10.0,
                )
                scope_input = build_probe_scope_input(
                    context_type=options.context_type,
                    target_uri="",
                    target_doc_id=None,
                    session_context=options.session_context,
                )
                retrieve_plan = measure_sync(
                    stage_timings,
                    "plan",
                    self._orchestrator.plan_memory,
                    query=query,
                    probe_result=probe_result,
                    max_items=options.max_items,
                    recall_mode=options.recall_mode,
                    detail_level_override=options.detail_level_override,
                    scope_input=scope_input,
                )
                if retrieve_plan is not None:
                    runtime_bound = measure_sync(
                        stage_timings,
                        "bind",
                        self._orchestrator.bind_memory_runtime,
                        probe_result=probe_result,
                        retrieve_plan=retrieve_plan,
                        max_items=options.max_items,
                        session_context=options.session_context,
                        include_knowledge=options.include_knowledge,
                    )
            except asyncio.TimeoutError:
                retrieve_plan = None
                logger.warning(
                    "[ContextManager] Recall planning timeout sid=%s turn=%s tenant=%s user=%s",
                    session_id,
                    turn_id,
                    tenant_id,
                    user_id,
                )
            except Exception as exc:
                retrieve_plan = None
                logger.warning(
                    "[ContextManager] Recall planning failed sid=%s turn=%s tenant=%s user=%s: %s",
                    session_id,
                    turn_id,
                    tenant_id,
                    user_id,
                    exc,
                )
        intent_timings = stage_timings.snapshot()
        intent_ms = (
            intent_timings["probe"] + intent_timings["plan"] + intent_timings["bind"]
        )
        return PreparePlanningState(
            query=query,
            probe_result=probe_result,
            retrieve_plan=retrieve_plan,
            runtime_bound=runtime_bound,
            intent_ms=intent_ms,
        )

    async def _retrieve_prepare_payload(
        self,
        *,
        sk: SessionKey,
        session_id: str,
        turn_id: str,
        tenant_id: str,
        user_id: str,
        options: PrepareOptions,
        planning: PreparePlanningState,
        stage_timings: StageTimingCollector,
    ) -> PrepareRetrievalState:
        """Run memory/knowledge retrieval fan-out for prepare()."""
        should_recall = planning.retrieve_plan is not None
        if not should_recall:
            return PrepareRetrievalState()

        detail_level = planning.runtime_bound["effective_depth"]
        include_memory = (
            "memory" in planning.runtime_bound["sources"]
            and planning.runtime_bound["memory_limit"] > 0
        )
        include_knowledge = (
            "knowledge" in planning.runtime_bound["sources"]
            and planning.runtime_bound["knowledge_limit"] > 0
        )
        retrieval_started = time.monotonic()

        async def _memory_search() -> Tuple[
            List[Dict[str, Any]],
            int,
            List[str],
            List[str],
            Dict[str, Any],
        ]:
            started = time.monotonic()
            try:
                search_kwargs: Dict[str, Any] = {
                    "query": planning.query,
                    "limit": options.max_items,
                    "detail_level": detail_level,
                    "probe_result": planning.probe_result,
                    "retrieve_plan": planning.retrieve_plan,
                }
                if options.session_context is not None:
                    search_kwargs["session_context"] = options.session_context
                if options.context_type:
                    search_kwargs["context_type"] = options.context_type
                if options.category:
                    search_kwargs["metadata_filter"] = self._prepare_category_filter(
                        options.category
                    )
                find_result = await self._orchestrator.search(**search_kwargs)
                # Expand directory hits into children leaf records
                find_result.memories = await self._expand_directory_hits(
                    find_result.memories
                )
                owner_ids = await self._orchestrator._resolve_memory_owner_ids(
                    find_result.memories
                )
                skill_uris = self._extract_skill_uris(
                    getattr(find_result, "skills", []) or []
                )
                runtime_trace = {}
                if getattr(find_result, "runtime_result", None) is not None:
                    runtime_trace = find_result.runtime_result.trace.to_dict()
                return (
                    self._format_memories(find_result, detail_level),
                    int((time.monotonic() - started) * 1000),
                    skill_uris,
                    owner_ids,
                    runtime_trace,
                )
            except Exception as exc:
                logger.warning(
                    "[ContextManager] Memory search failed sid=%s turn=%s tenant=%s user=%s: %s",
                    session_id,
                    turn_id,
                    tenant_id,
                    user_id,
                    exc,
                )
                return [], int((time.monotonic() - started) * 1000), [], [], {}

        async def _knowledge_search() -> Tuple[List[Dict[str, Any]], int]:
            started = time.monotonic()
            try:
                k_result = await self._orchestrator.knowledge_search(
                    query=planning.query,
                    limit=planning.runtime_bound["knowledge_limit"],
                )
                return (
                    self._format_knowledge(k_result.get("results", [])),
                    int((time.monotonic() - started) * 1000),
                )
            except Exception as exc:
                logger.warning(
                    "[ContextManager] Knowledge search failed sid=%s turn=%s tenant=%s user=%s: %s",
                    session_id,
                    turn_id,
                    tenant_id,
                    user_id,
                    exc,
                )
                return [], int((time.monotonic() - started) * 1000)

        coros = []
        if include_memory:
            coros.append(_memory_search())
        if include_knowledge:
            coros.append(_knowledge_search())

        results = await asyncio.gather(*coros) if coros else []
        retrieval = PrepareRetrievalState()
        result_idx = 0
        if include_memory:
            (
                retrieval.memory_items,
                retrieval.memory_ms,
                skill_uris,
                memory_owner_ids,
                retrieval.memory_runtime_trace,
            ) = results[result_idx]
            if memory_owner_ids:
                self._session_memory_owner_ids.setdefault(sk, set()).update(
                    memory_owner_ids
                )
            if (
                skill_uris
                and hasattr(self._orchestrator, "_skill_event_store")
                and self._orchestrator._skill_event_store
            ):
                self._selected_skill_uris[(sk, turn_id)] = set(skill_uris)
                for skill_uri in skill_uris:
                    await self._append_skill_event(
                        session_id,
                        turn_id,
                        skill_uri,
                        tenant_id,
                        user_id,
                        "selected",
                    )
            result_idx += 1
        if include_knowledge:
            retrieval.knowledge_items, retrieval.knowledge_ms = results[result_idx]

        stage_timings.record_elapsed("retrieve", retrieval_started)
        return retrieval

    def _build_prepare_result(
        self,
        *,
        session_id: str,
        turn_id: str,
        stage_timings: StageTimingCollector,
        planning: PreparePlanningState,
        retrieval: PrepareRetrievalState,
    ) -> Dict[str, Any]:
        """Assemble the stable prepare response envelope."""
        should_recall = planning.retrieve_plan is not None
        detail_level = planning.runtime_bound["effective_depth"]
        instructions = self._build_instructions(
            detail_level,
            retrieval.memory_items,
            retrieval.knowledge_items,
        )

        result = {
            "session_id": session_id,
            "turn_id": turn_id,
            "intent": {
                "should_recall": should_recall,
                "probe_candidate_count": planning.probe_result.evidence.candidate_count,
                "probe_top_score": planning.probe_result.evidence.top_score,
                "depth": detail_level,
            },
            "memory": retrieval.memory_items,
            "knowledge": retrieval.knowledge_items,
            "instructions": instructions,
        }
        result["intent"]["memory_pipeline"] = {
            "probe": planning.probe_result.to_dict(),
            "planner": (
                planning.retrieve_plan.to_dict() if planning.retrieve_plan else None
            ),
            "runtime": {
                "trace": {
                    "probe_mode": (
                        self._orchestrator.memory_probe_mode()
                        if hasattr(self._orchestrator, "memory_probe_mode")
                        else "unavailable"
                    ),
                    "probe_trace": (
                        self._orchestrator.memory_probe_trace()
                        if hasattr(self._orchestrator, "memory_probe_trace")
                        else {
                            "backend": "unavailable",
                            "top_k": 0,
                            "degraded": True,
                            "degrade_reason": "probe_trace_unavailable",
                        }
                    ),
                    "probe": dict(
                        retrieval.memory_runtime_trace.get(
                            "probe", planning.probe_result.to_dict()
                        )
                    ),
                    "planner": dict(
                        retrieval.memory_runtime_trace.get(
                            "planner",
                            (
                                planning.retrieve_plan.to_dict()
                                if planning.retrieve_plan
                                else {}
                            ),
                        )
                    ),
                    "effective": dict(
                        retrieval.memory_runtime_trace.get(
                            "effective",
                            {
                                "sources": list(planning.runtime_bound["sources"]),
                                "retrieval_depth": planning.runtime_bound[
                                    "effective_depth"
                                ],
                            },
                        )
                    ),
                    "hydration": list(
                        retrieval.memory_runtime_trace.get("hydration", [])
                    ),
                    "latency_ms": dict(
                        retrieval.memory_runtime_trace.get(
                            "latency_ms",
                            {
                                "execution": 0,
                                "stages": stage_timings.snapshot(),
                                "retrieve": {
                                    "embed": 0.0,
                                    "search": 0.0,
                                    "rerank": 0.0,
                                    "assemble": 0.0,
                                    "total": 0.0,
                                },
                            },
                        )
                    ),
                    "stage_timing_ms": stage_timings.snapshot(),
                },
                "degrade": dict(planning.runtime_bound["degrade"]),
            },
        }
        return result

    async def _prepare(
        self,
        session_id: str,
        turn_id: str,
        messages: List[Dict[str, str]],
        tenant_id: str,
        user_id: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        options = self._build_prepare_options(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            config=config,
        )
        sk = self._make_session_key(tenant_id, user_id, session_id)
        prepare_started = time.monotonic()
        stage_timings = StageTimingCollector()

        # 1. Idempotent: cache hit → return directly
        cache_key = self._make_cache_key(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            turn_id=turn_id,
        )
        cached = self._get_cached_prepare(cache_key)
        if cached is not None:
            cached_result = deepcopy(cached)
            self._touch_session(sk)
            cache_total_ms = int((time.monotonic() - prepare_started) * 1000)
            runtime_trace = (
                cached_result.get("intent", {})
                .get("memory_pipeline", {})
                .get("runtime", {})
                .get("trace")
            )
            if isinstance(runtime_trace, dict):
                cache_stage_timings = StageTimingCollector()
                cache_stage_timings.record_ms("total", cache_total_ms)
                runtime_trace["cache_hit"] = True
                runtime_trace["stage_timing_ms"] = cache_stage_timings.snapshot()
            logger.debug(
                "[ContextManager] prepare CACHE_HIT sid=%s turn=%s tenant=%s user=%s",
                session_id,
                turn_id,
                tenant_id,
                user_id,
            )
            return cached_result

        # 2. Session auto-create (session-level lock prevents concurrent begin)
        await self._ensure_prepare_session(
            sk=sk,
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )

        # 3. Extract user query
        query = self._extract_query(messages)
        if not query:
            result = self._empty_prepare(session_id, turn_id)
            self._cache_prepare(cache_key, sk, result)
            return result

        planning = await self._plan_prepare_recall(
            query=query,
            session_id=session_id,
            turn_id=turn_id,
            tenant_id=tenant_id,
            user_id=user_id,
            options=options,
            stage_timings=stage_timings,
        )
        retrieval = await self._retrieve_prepare_payload(
            sk=sk,
            session_id=session_id,
            turn_id=turn_id,
            tenant_id=tenant_id,
            user_id=user_id,
            options=options,
            planning=planning,
            stage_timings=stage_timings,
        )

        aggregate_started = time.monotonic()
        result = self._build_prepare_result(
            session_id=session_id,
            turn_id=turn_id,
            stage_timings=stage_timings,
            planning=planning,
            retrieval=retrieval,
        )

        total_ms = int((time.monotonic() - prepare_started) * 1000)
        stage_timings.record_elapsed("aggregate", aggregate_started)
        stage_timings.record_ms("total", total_ms)
        result["intent"]["memory_pipeline"]["runtime"]["trace"]["stage_timing_ms"] = (
            stage_timings.snapshot()
        )
        logger.info(
            "[ContextManager] prepare sid=%s turn=%s tenant=%s user=%s "
            "probe_candidates=%d recall=%s memory=%d knowledge=%d "
            "timing_ms(total=%d intent=%d memory=%d knowledge=%d)",
            session_id,
            turn_id,
            tenant_id,
            user_id,
            planning.probe_result.evidence.candidate_count,
            planning.retrieve_plan is not None,
            len(retrieval.memory_items),
            len(retrieval.knowledge_items),
            total_ms,
            planning.intent_ms,
            retrieval.memory_ms,
            retrieval.knowledge_ms,
        )
        self._cache_prepare(cache_key, sk, result)
        return result

    # =========================================================================
    # Phase: commit
    # =========================================================================

    @staticmethod
    def _merge_unique_strings(*groups: Any) -> List[str]:
        """Return a stable ordered union of non-empty string values."""
        merged: List[str] = []
        for group in groups:
            if not group:
                continue
            if isinstance(group, str):
                values = [group]
            else:
                values = list(group)
            for value in values:
                normalized = str(value).strip()
                if normalized and normalized not in merged:
                    merged.append(normalized)
        return merged

    @staticmethod
    def _split_topic_values(raw_value: Any) -> List[str]:
        """Normalize topic-like values, splitting comma-separated strings."""
        if not raw_value:
            return []
        if isinstance(raw_value, str):
            return [
                token.strip()
                for token in raw_value.split(",")
                if token and token.strip()
            ]
        return ContextManager._merge_unique_strings(raw_value)

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
        from opencortex.utils.uri import CortexURI

        start = int(msg_range[0])
        end = int(msg_range[1])
        session_hash = hashlib.md5(session_id.encode("utf-8")).hexdigest()[:12]
        node_name = f"conversation-{session_hash}-{start:06d}-{end:06d}"
        return CortexURI.build_private(
            tenant_id,
            user_id,
            "memories",
            "events",
            node_name,
        )

    @staticmethod
    def _directory_uri(
        tenant_id: str,
        user_id: str,
        session_id: str,
        index: int,
    ) -> str:
        """Return URI for a directory parent record."""
        from opencortex.utils.uri import CortexURI

        session_hash = hashlib.md5(session_id.encode("utf-8")).hexdigest()[:12]
        node_name = f"conversation-{session_hash}/dir-{index:03d}"
        return CortexURI.build_private(
            tenant_id,
            user_id,
            "memories",
            "events",
            node_name,
        )

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
            return {k: ContextManager._canonicalize_for_hash(v) for k, v in value.items()}
        if isinstance(value, list):
            if all(isinstance(item, (str, int, float, bool, type(None))) for item in value):
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

        result: Dict[str, str] = {uri: overrides[uri] for uri in uris if uri in overrides}
        missing = [uri for uri in uris if uri not in overrides]
        if not missing:
            return result

        fs = getattr(self._orchestrator, "_fs", None)
        if fs is None:
            for uri in missing:
                result[uri] = ""
            return result

        async def _read_one(uri: str) -> Tuple[str, str]:
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

    async def _load_immediate_records(
        self,
        immediate_uris: List[str],
    ) -> List[Dict[str, Any]]:
        """Load immediate records and return them ordered by message index."""
        if not immediate_uris:
            return []
        records = await self._orchestrator._storage.filter(
            self._orchestrator._get_collection(),
            {"op": "must", "field": "uri", "conds": immediate_uris},
            limit=max(len(immediate_uris), 1),
        )
        by_uri = {
            str(record.get("uri", "")).strip(): record
            for record in records
            if str(record.get("uri", "")).strip()
        }
        ordered: List[Dict[str, Any]] = []
        for uri in immediate_uris:
            record = by_uri.get(str(uri).strip())
            if record is not None:
                ordered.append(record)
        return ordered

    # NOTE: ``_record_msg_range`` and ``_record_text`` previously lived
    # here as staticmethods. They moved to
    # ``src/opencortex/context/session_records.py`` as module-level
    # ``record_msg_range`` / ``record_text`` (REVIEW §25 Phase 5 / U1)
    # so the new ``SessionRecordsRepository`` can reuse them without a
    # circular import. ContextManager imports them from there.
    #
    # ``_load_session_merged_records``, ``_load_session_directory_records``,
    # and ``_session_layer_counts`` similarly moved to the repository as
    # ``load_merged`` / ``load_directories`` / ``layer_counts``. Call
    # sites use ``self._session_records`` instead.

    async def _select_tail_merged_records(
        self,
        *,
        session_id: str,
        source_uri: str,
    ) -> List[Dict[str, Any]]:
        """Select a bounded recent merged-tail window for online recomposition."""
        merged_records = await self._session_records.load_merged(
            session_id=session_id,
            source_uri=source_uri,
        )
        if not merged_records:
            return []

        selected: List[Dict[str, Any]] = []
        selected_message_count = 0
        for record in reversed(merged_records):
            msg_range = record_msg_range(record)
            if msg_range is None:
                continue
            width = (msg_range[1] - msg_range[0]) + 1
            if len(selected) >= _RECOMPOSE_TAIL_MAX_MERGED_LEAVES:
                break
            if (
                selected
                and (selected_message_count + width) > _RECOMPOSE_TAIL_MAX_MESSAGES
            ):
                break
            selected.append(record)
            selected_message_count += width
        selected.reverse()
        return selected

    @classmethod
    def _segment_anchor_terms(cls, record: Dict[str, Any]) -> Set[str]:
        """Extract coarse anchor terms used for sequential merge boundaries."""
        meta = dict(record.get("meta") or {})
        abstract_json = record.get("abstract_json")
        slots = (
            abstract_json.get("slots", {}) if isinstance(abstract_json, dict) else {}
        )
        return set(
            cls._merge_unique_strings(
                record.get("entities"),
                meta.get("entities"),
                slots.get("entities"),
                cls._split_topic_values(record.get("keywords")),
                cls._split_topic_values(meta.get("topics")),
                cls._split_topic_values(slots.get("topics")),
            )
        )

    @classmethod
    def _segment_time_refs(cls, record: Dict[str, Any]) -> Set[str]:
        """Extract normalized time references used for sequential merge boundaries."""
        meta = dict(record.get("meta") or {})
        abstract_json = record.get("abstract_json")
        slots = (
            abstract_json.get("slots", {}) if isinstance(abstract_json, dict) else {}
        )
        return set(
            cls._merge_unique_strings(
                meta.get("time_refs"),
                slots.get("time_refs"),
                record.get("event_date"),
                meta.get("event_date"),
            )
        )

    @classmethod
    def _is_coarse_time_ref(cls, value: str) -> bool:
        """Return whether one time ref is too coarse to force two events together."""
        normalized = str(value or "").strip()
        if not normalized:
            return False
        return bool(
            _COARSE_ISO_DATE_RE.fullmatch(normalized)
            or _COARSE_HUMAN_DATE_RE.fullmatch(normalized)
            or _COARSE_WEEKDAY_RE.fullmatch(normalized)
        )

    @classmethod
    def _time_refs_overlap(cls, left: Set[str], right: Set[str]) -> bool:
        """Return whether two time-ref sets meaningfully overlap for segmentation."""
        shared = set(left).intersection(right)
        if not shared:
            return False

        left_specific = {value for value in left if not cls._is_coarse_time_ref(value)}
        right_specific = {
            value for value in right if not cls._is_coarse_time_ref(value)
        }
        if not left_specific or not right_specific:
            return True

        return bool(left_specific.intersection(right_specific))

    async def _aggregate_records_metadata(
        self,
        records: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Collect anchor metadata from already loaded immediate records."""
        if not records:
            return {}

        entities: List[str] = []
        time_refs: List[str] = []
        topics: List[str] = []
        event_date = ""

        for record in records:
            meta = dict(record.get("meta") or {})
            abstract_json = record.get("abstract_json")
            slots = (
                abstract_json.get("slots", {})
                if isinstance(abstract_json, dict)
                else {}
            )

            entities = self._merge_unique_strings(
                entities,
                record.get("entities"),
                meta.get("entities"),
                slots.get("entities"),
            )
            time_refs = self._merge_unique_strings(
                time_refs,
                meta.get("time_refs"),
                slots.get("time_refs"),
                record.get("event_date"),
                meta.get("event_date"),
            )
            topics = self._merge_unique_strings(
                topics,
                self._split_topic_values(record.get("keywords")),
                self._split_topic_values(meta.get("keywords")),
                self._split_topic_values(meta.get("topics")),
                self._split_topic_values(slots.get("topics")),
            )

            if not event_date:
                event_date = str(
                    record.get("event_date") or meta.get("event_date") or ""
                ).strip()

        merged_meta: Dict[str, Any] = {}
        if entities:
            merged_meta["entities"] = entities
        if time_refs:
            merged_meta["time_refs"] = time_refs
        if topics:
            merged_meta["topics"] = topics
        if event_date:
            merged_meta["event_date"] = event_date
        return merged_meta

    async def _build_recomposition_entries(
        self,
        *,
        snapshot: ConversationBuffer,
        immediate_records: List[Dict[str, Any]],
        tail_records: List[Dict[str, Any]],
    ) -> List[RecompositionEntry]:
        """Build ordered recomposition entries from merged-tail + immediates."""
        entries: List[RecompositionEntry] = []

        fs = getattr(self._orchestrator, "_fs", None)
        tail_uris = [
            str(r.get("uri", "") or "").strip() for r in tail_records if r.get("uri")
        ]

        async def _read_l2(uri: str) -> str:
            try:
                return await fs.read_file(f"{uri}/content.md")
            except Exception:
                return ""

        if fs and tail_uris:
            l2_contents = await asyncio.gather(*[_read_l2(u) for u in tail_uris])
            l2_by_uri = dict(zip(tail_uris, l2_contents))
        else:
            l2_by_uri = {}

        for record in tail_records:
            msg_range = record_msg_range(record)
            if msg_range is None:
                continue
            uri = str(record.get("uri", "") or "").strip()
            text = l2_by_uri.get(uri, "") or record_text(record)
            if not text:
                continue
            entries.append(
                RecompositionEntry(
                    text=text,
                    uri=uri,
                    msg_start=msg_range[0],
                    msg_end=msg_range[1],
                    token_count=max(self._estimate_tokens(text), 1),
                    anchor_terms=self._segment_anchor_terms(record),
                    time_refs=self._segment_time_refs(record),
                    source_record=record,
                    immediate_uris=[],
                    superseded_merged_uris=([uri] if uri else []),
                    source_segment_index=None,
                )
            )

        by_uri = {
            str(record.get("uri", "")).strip(): record
            for record in immediate_records
            if str(record.get("uri", "")).strip()
        }
        for offset, text in enumerate(snapshot.messages):
            uri = (
                snapshot.immediate_uris[offset]
                if offset < len(snapshot.immediate_uris)
                else ""
            )
            normalized_uri = str(uri or "").strip()
            record = by_uri.get(normalized_uri)
            if record is None:
                fallback_index = snapshot.start_msg_index + offset
                record = {
                    "uri": normalized_uri,
                    "abstract": text,
                    "meta": {"msg_index": fallback_index},
                    "keywords": "",
                    "entities": [],
                }
            msg_range = record_msg_range(record)
            if msg_range is None:
                msg_index = snapshot.start_msg_index + offset
                msg_range = (msg_index, msg_index)
            entries.append(
                RecompositionEntry(
                    text=str(text),
                    uri=normalized_uri,
                    msg_start=msg_range[0],
                    msg_end=msg_range[1],
                    token_count=max(self._estimate_tokens(text), 1),
                    anchor_terms=self._segment_anchor_terms(record),
                    time_refs=self._segment_time_refs(record),
                    source_record=record,
                    immediate_uris=([normalized_uri] if normalized_uri else []),
                    superseded_merged_uris=[],
                    source_segment_index=None,
                )
            )

        entries.sort(
            key=lambda entry: (
                int(entry["msg_start"]),
                int(entry["msg_end"]),
                str(entry["uri"]),
            )
        )
        return entries

    def _build_recomposition_segments(
        self,
        entries: List[RecompositionEntry],
    ) -> List[Dict[str, Any]]:
        """Split ordered recomposition entries into bounded semantic segments."""
        if not entries:
            return []

        segments: List[Dict[str, Any]] = []
        current: List[RecompositionEntry] = []
        current_tokens = 0
        current_messages = 0

        for entry in entries:
            entry_messages = (int(entry["msg_end"]) - int(entry["msg_start"])) + 1
            should_split = False
            if current:
                # REVIEW closure tracker R3-RC-02 / R2-14: hard split
                # when the new entry crosses an input-segment boundary.
                # Benchmark entries carry source_segment_index; production
                # / re-derived entries carry None and skip this check
                # entirely (no behavior change for non-benchmark paths).
                prev_segment_index = current[-1]["source_segment_index"]
                entry_segment_index = entry["source_segment_index"]
                if (
                    prev_segment_index is not None
                    and entry_segment_index is not None
                    and prev_segment_index != entry_segment_index
                ):
                    should_split = True
                else:
                    current_time_refs: Set[str] = set()
                    for item in current:
                        current_time_refs.update(item["time_refs"])
                    if (
                        current_messages >= _SEGMENT_MAX_MESSAGES
                        or current_tokens + int(entry["token_count"]) > _SEGMENT_MAX_TOKENS
                        or (
                            current_messages >= _SEGMENT_MIN_MESSAGES
                            and current_time_refs
                            and entry["time_refs"]
                            and not self._time_refs_overlap(
                                current_time_refs,
                                entry["time_refs"],
                            )
                        )
                    ):
                        should_split = True

            if should_split:
                segments.append(self._finalize_recomposition_segment(current))
                current = []
                current_tokens = 0
                current_messages = 0

            current.append(entry)
            current_tokens += int(entry["token_count"])
            current_messages += max(entry_messages, 1)

        if current:
            segments.append(self._finalize_recomposition_segment(current))

        return segments

    def _build_anchor_clustered_segments(
        self,
        entries: List[RecompositionEntry],
    ) -> List[Dict[str, Any]]:
        """Cluster entries by anchor Jaccard similarity for full_recompose."""
        if not entries:
            return []

        segments: List[Dict[str, Any]] = []
        # Treat the seed entry the same way the cap check treats any
        # subsequent append (REVIEW ADV-002): the prior implementation
        # initialized ``current`` from ``entries[0]`` unconditionally, so
        # a single oversized leaf — possible under U2's 64 KB content
        # cap — could seed a cluster already larger than
        # ``_RECOMPOSE_CLUSTER_MAX_TOKENS``. The cap then only stopped
        # APPEND, never SPLIT. Now an oversized seed flushes immediately
        # as its own single-entry segment and the next entry becomes the
        # new seed.
        current: List[RecompositionEntry] = []
        current_anchors: Set[str] = set()
        current_tokens = 0
        current_messages = 0

        def _within_caps_with(entry_tokens: int, entry_messages: int) -> bool:
            return (
                current_tokens + entry_tokens <= _RECOMPOSE_CLUSTER_MAX_TOKENS
                and current_messages + max(entry_messages, 1)
                <= _RECOMPOSE_CLUSTER_MAX_MESSAGES
            )

        def _seed_with(entry: RecompositionEntry) -> None:
            nonlocal current, current_anchors, current_tokens, current_messages
            entry_msgs = int(entry["msg_end"]) - int(entry["msg_start"]) + 1
            current = [entry]
            current_anchors = set(entry["anchor_terms"] | entry["time_refs"])
            current_tokens = int(entry["token_count"])
            current_messages = max(entry_msgs, 1)

        for entry in entries:
            entry_anchors: Set[str] = entry["anchor_terms"] | entry["time_refs"]
            entry_messages = int(entry["msg_end"]) - int(entry["msg_start"]) + 1
            entry_tokens = int(entry["token_count"])

            if not current:
                _seed_with(entry)
                # An oversized seed is its own single-entry cluster:
                # downstream ``_run_full_session_recomposition`` skips
                # clusters with ``len(source_records) < 2``, so emitting
                # it alone keeps every multi-entry cluster within caps.
                if (
                    current_tokens > _RECOMPOSE_CLUSTER_MAX_TOKENS
                    or current_messages > _RECOMPOSE_CLUSTER_MAX_MESSAGES
                ):
                    segments.append(self._finalize_recomposition_segment(current))
                    current = []
                    current_anchors = set()
                    current_tokens = 0
                    current_messages = 0
                continue

            within_caps = _within_caps_with(entry_tokens, entry_messages)

            if not entry_anchors:
                # Anchorless entries used to bypass the caps unconditionally
                # (R2-13). For inputs without entities/topics — e.g. some
                # LongMemEval samples — that grew a single cluster until
                # ``_derive_parent_summary`` blew its context window. Now
                # anchorless entries still cluster greedily but respect the
                # same token / message caps as anchored entries.
                if within_caps:
                    current.append(entry)
                    current_tokens += entry_tokens
                    current_messages += max(entry_messages, 1)
                else:
                    segments.append(self._finalize_recomposition_segment(current))
                    current = [entry]
                    current_anchors = set()
                    current_tokens = entry_tokens
                    current_messages = max(entry_messages, 1)
                continue

            union = current_anchors | entry_anchors
            jaccard = (
                len(current_anchors & entry_anchors) / len(union) if union else 0.0
            )

            if jaccard >= _RECOMPOSE_CLUSTER_JACCARD_THRESHOLD and within_caps:
                current.append(entry)
                current_anchors = current_anchors | entry_anchors
                current_tokens += entry_tokens
                current_messages += max(entry_messages, 1)
            else:
                segments.append(self._finalize_recomposition_segment(current))
                current = [entry]
                current_anchors = set(entry_anchors)
                current_tokens = entry_tokens
                current_messages = max(entry_messages, 1)

        if current:
            segments.append(self._finalize_recomposition_segment(current))

        return segments

    def _finalize_recomposition_segment(
        self,
        entries: List[RecompositionEntry],
    ) -> Dict[str, Any]:
        """Materialize one recomposition segment payload."""
        msg_starts = [int(entry["msg_start"]) for entry in entries]
        msg_ends = [int(entry["msg_end"]) for entry in entries]
        source_records: List[Dict[str, Any]] = []
        source_uris: Set[str] = set()
        for entry in entries:
            record = entry.get("source_record") or {}
            uri = str(record.get("uri", "") or "")
            if uri and uri in source_uris:
                continue
            if uri:
                source_uris.add(uri)
            source_records.append(record)
        return {
            "messages": [
                str(entry["text"]) for entry in entries if str(entry["text"]).strip()
            ],
            "immediate_uris": self._merge_unique_strings(
                *[entry.get("immediate_uris", []) for entry in entries]
            ),
            "superseded_merged_uris": self._merge_unique_strings(
                *[entry.get("superseded_merged_uris", []) for entry in entries]
            ),
            "msg_range": [min(msg_starts), max(msg_ends)],
            "source_records": source_records,
        }

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
        sk = self._make_session_key(tenant_id, user_id, session_id)
        self._touch_session(sk)
        self._remember_session_project(sk)
        lock = self._session_locks.setdefault(sk, asyncio.Lock())

        async with lock:
            if turn_id in self._committed_turns.get(sk, set()):
                logger.debug(
                    "[ContextManager] commit DUPLICATE sid=%s turn=%s tenant=%s user=%s",
                    session_id,
                    turn_id,
                    tenant_id,
                    user_id,
                )
                return {
                    "accepted": True,
                    "write_status": "duplicate",
                    "turn_id": turn_id,
                }

            observer_ok = True
            try:
                self._observer.record_batch(
                    self._observer_session_id(
                        session_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    ),
                    messages,
                    tenant_id,
                    user_id,
                    tool_calls=tool_calls,
                )
            except Exception as exc:
                observer_ok = False
                logger.warning(
                    "[ContextManager] Observer record failed sid=%s turn=%s tenant=%s user=%s: %s "
                    "— writing to fallback",
                    session_id,
                    turn_id,
                    tenant_id,
                    user_id,
                    exc,
                )
                self._write_fallback(session_id, turn_id, messages, tenant_id, user_id)

            self._committed_turns.setdefault(sk, set()).add(turn_id)

            if cited_uris:
                valid_uris = [u for u in cited_uris if u.startswith("opencortex://")]
                if valid_uris:
                    task = asyncio.create_task(self._apply_cited_rewards(valid_uris))
                    self._pending_tasks.add(task)
                    task.add_done_callback(self._pending_tasks.discard)

            if (
                cited_uris
                and hasattr(self._orchestrator, "_skill_event_store")
                and self._orchestrator._skill_event_store
            ):
                skill_uris = [u for u in cited_uris if "/skills/" in u]
                server_selected = self._selected_skill_uris.get((sk, turn_id), set())
                for uri in skill_uris:
                    if uri not in server_selected:
                        logger.debug(
                            "[ContextManager] Dropped forged skill citation: %s", uri
                        )
                        continue
                    await self._append_skill_event(
                        session_id,
                        turn_id,
                        uri,
                        tenant_id,
                        user_id,
                        "cited",
                    )

            buffer = self._conversation_buffers.setdefault(sk, ConversationBuffer())
            write_items = []
            for i, msg in enumerate(messages):
                text = msg.get(
                    "content",
                    msg.get("assistant_response", msg.get("user_message", "")),
                )
                if not text:
                    continue
                msg_meta = dict(msg.get("meta") or {})
                stored_text = self._decorate_message_text(text, msg_meta)
                role = msg.get("role", "")
                idx = buffer.start_msg_index + len(buffer.messages) + i
                tc = tool_calls if role == "assistant" else None
                write_items.append((stored_text, idx, tc, msg_meta))

            if write_items:
                tokens_for_identity = set_request_identity(tenant_id, user_id)
                try:
                    results = await asyncio.gather(
                        *[
                            self._orchestrator._write_immediate(
                                session_id=session_id,
                                msg_index=idx,
                                text=text,
                                tool_calls=tc,
                                meta=msg_meta,
                            )
                            for text, idx, tc, msg_meta in write_items
                        ],
                        return_exceptions=True,
                    )
                finally:
                    reset_request_identity(tokens_for_identity)

                merge_lock = self._session_merge_locks.setdefault(sk, asyncio.Lock())
                async with merge_lock:
                    active_buffer = self._conversation_buffers.get(sk)
                    if active_buffer is None:
                        active_buffer = buffer
                        self._conversation_buffers[sk] = active_buffer
                    elif active_buffer is not buffer:
                        logger.debug(
                            "[ContextManager] commit detected buffer rollover "
                            "sid=%s turn=%s tenant=%s user=%s old_start=%d new_start=%d",
                            session_id,
                            turn_id,
                            tenant_id,
                            user_id,
                            buffer.start_msg_index,
                            active_buffer.start_msg_index,
                        )

                    for (text, idx, tc, _msg_meta), result in zip(write_items, results):
                        if isinstance(result, Exception):
                            logger.warning(
                                "[ContextManager] Immediate write failed sid=%s turn=%s msg_index=%d chars=%d exc_type=%s exc=%r",
                                session_id,
                                turn_id,
                                idx,
                                len(text),
                                type(result).__name__,
                                result,
                                exc_info=(
                                    type(result),
                                    result,
                                    result.__traceback__,
                                ),
                            )
                            continue
                        active_buffer.messages.append(text)
                        active_buffer.immediate_uris.append(result)
                        active_buffer.token_count += self._estimate_tokens(text)

                    if tool_calls:
                        active_buffer.tool_calls_per_turn.append(tool_calls)

                    buffer = active_buffer

            if buffer.token_count >= self._merge_trigger_threshold():
                self._spawn_merge_task(sk, session_id, tenant_id, user_id)

            write_status = "ok" if observer_ok else "fallback"
            if not observer_ok:
                logger.warning(
                    "[ContextManager] commit FALLBACK sid=%s turn=%s tenant=%s user=%s",
                    session_id,
                    turn_id,
                    tenant_id,
                    user_id,
                )
            else:
                logger.info(
                    "[ContextManager] commit sid=%s turn=%s tenant=%s user=%s messages=%d cited=%d",
                    session_id,
                    turn_id,
                    tenant_id,
                    user_id,
                    len(messages),
                    len(cited_uris) if cited_uris else 0,
                )

            return {
                "accepted": True,
                "write_status": write_status,
                "turn_id": turn_id,
                "session_turns": len(self._committed_turns.get(sk, set())),
            }

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
        existing_task = self._session_merge_tasks.get(sk)
        if existing_task and not existing_task.done():
            return

        collection_name = get_collection_name()
        task = asyncio.create_task(
            self._merge_buffer(
                sk,
                session_id,
                tenant_id,
                user_id,
                flush_all=False,
                collection_name=collection_name,
                raise_on_error=True,
            )
        )
        self._session_merge_tasks[sk] = task
        self._pending_tasks.add(task)

        def _cleanup(done_task: asyncio.Task) -> None:
            self._pending_tasks.discard(done_task)
            with contextlib.suppress(asyncio.CancelledError):
                exc = done_task.exception()
                if exc is not None:
                    failures = self._session_merge_task_failures.setdefault(sk, [])
                    failures.append(exc)
            if self._session_merge_tasks.get(sk) is done_task:
                self._session_merge_tasks.pop(sk, None)

        task.add_done_callback(_cleanup)

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
        existing_task = self._session_full_recompose_tasks.get(sk)
        if existing_task and not existing_task.done():
            return

        collection_name = get_collection_name()
        task = asyncio.create_task(
            self._run_full_session_recomposition(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                source_uri=source_uri,
                collection_name=collection_name,
                raise_on_error=raise_on_error,
            )
        )
        self._session_full_recompose_tasks[sk] = task
        self._pending_tasks.add(task)

        def _cleanup(done_task: asyncio.Task) -> None:
            self._pending_tasks.discard(done_task)
            if self._session_full_recompose_tasks.get(sk) is done_task:
                self._session_full_recompose_tasks.pop(sk, None)

        task.add_done_callback(_cleanup)

    async def _run_full_session_recomposition(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: Optional[str],
        collection_name: Optional[str] = None,
        raise_on_error: bool = False,
        return_created_uris: bool = False,
    ) -> Optional[List[str]]:
        """Create directory parent records for semantically related leaf clusters.

        Preserves original merged leaf records. For each cluster of >=2 leaves,
        generates a directory summary from children abstracts and writes a
        directory record (layer="directory", is_leaf=False).

        When ``return_created_uris`` is True returns the list of created
        directory URIs so callers (e.g. the benchmark ingest cleanup
        tracker) can register them for rollback. Defaults to False so all
        existing call sites — including the production conversation
        lifecycle in ``context_end`` — preserve their previous return
        contract (None) and behavior.
        """
        tokens_for_identity = set_request_identity(tenant_id, user_id)
        coll_token = set_collection_name(collection_name) if collection_name else None
        created_directory_uris: List[str] = []
        try:
            merged_records = await self._session_records.load_merged(
                session_id=session_id,
                source_uri=source_uri,
            )
            logger.info(
                "[ContextManager] Full recompose start sid=%s tenant=%s user=%s collection=%s source_uri=%s merged=%d",
                session_id,
                tenant_id,
                user_id,
                self._orchestrator._get_collection(),
                source_uri,
                len(merged_records),
            )
            if len(merged_records) <= 1:
                return [] if return_created_uris else None

            entries: List[RecompositionEntry] = []
            for record in merged_records:
                msg_range = record_msg_range(record)
                if msg_range is None:
                    continue
                uri = str(record.get("uri", "") or "").strip()
                text = record_text(record)
                if not text:
                    continue
                entries.append(
                    RecompositionEntry(
                        text=text,
                        uri=uri,
                        msg_start=msg_range[0],
                        msg_end=msg_range[1],
                        token_count=max(self._estimate_tokens(text), 1),
                        anchor_terms=self._segment_anchor_terms(record),
                        time_refs=self._segment_time_refs(record),
                        source_record=record,
                        immediate_uris=[],
                        superseded_merged_uris=[],
                        source_segment_index=None,
                    )
                )

            segments = self._build_anchor_clustered_segments(entries)
            logger.info(
                "[ContextManager] Full recompose planned sid=%s entries=%d segments=%d ranges=%s",
                session_id,
                len(entries),
                len(segments),
                [segment.get("msg_range") for segment in segments[:8]],
            )
            if not segments:
                return [] if return_created_uris else None

            # Pre-compute eligible segments and their stable directory_index
            # so the bounded-concurrency derive loop below has deterministic
            # URIs regardless of which derive future resolves first.
            eligible: List[Tuple[int, Dict[str, Any], List[str]]] = []
            for segment in segments:
                source_records = segment.get("source_records", [])
                if len(source_records) < 2:
                    continue
                children_abstracts = [
                    str(rec.get("abstract") or "").strip()
                    for rec in source_records
                    if str(rec.get("abstract") or "").strip()
                ]
                if not children_abstracts:
                    continue
                eligible.append(
                    (len(eligible), segment, children_abstracts)
                )

            if not eligible:
                logger.info(
                    "[ContextManager] Full recompose: no eligible directories sid=%s",
                    session_id,
                )
                return [] if return_created_uris else None

            # Use the instance-scoped semaphore so cross-conversation
            # concurrency (U13) does not multiply in-flight LLM derives:
            # one global cap of _DIRECTORY_DERIVE_CONCURRENCY across all
            # sessions, not concurrency × 3.
            derive_semaphore = self._directory_derive_semaphore

            async def _derive_one(
                directory_index: int,
                children_abstracts: List[str],
            ) -> Tuple[int, Optional[Dict[str, Any]]]:
                cluster_title = f"Directory-{directory_index:03d}"
                async with derive_semaphore:
                    return directory_index, await (
                        self._orchestrator._derive_parent_summary(
                            doc_title=cluster_title,
                            children_abstracts=children_abstracts,
                        )
                    )

            derive_results = await asyncio.gather(
                *[
                    _derive_one(idx, kids)
                    for idx, _, kids in eligible
                ]
            )
            derived_by_index: Dict[int, Optional[Dict[str, Any]]] = dict(derive_results)

            # Sequential write phase preserves storage-order invariants the
            # production lifecycle relied on (URIs are written in
            # directory_index order; keywords-patch and FS writes happen
            # right after each Qdrant upsert).
            for directory_index, segment, children_abstracts in eligible:
                source_records = segment.get("source_records", [])
                logger.info(
                    "[ContextManager] Full recompose segment sid=%s dir_index=%d msg_range=%s children=%d",
                    session_id,
                    directory_index,
                    segment.get("msg_range"),
                    len(source_records),
                )

                derived = derived_by_index.get(directory_index)
                if not derived:
                    continue

                llm_abstract = derived.get("abstract", "")
                llm_overview = derived.get("overview", "")
                keywords_list = derived.get("keywords", [])
                keywords_str = (
                    ", ".join(str(k) for k in keywords_list if k)
                    if isinstance(keywords_list, list)
                    else ""
                )

                dir_uri = self._directory_uri(
                    tenant_id,
                    user_id,
                    session_id,
                    directory_index,
                )

                aggregated_meta = await self._aggregate_records_metadata(source_records)
                all_tool_calls: List[Dict[str, Any]] = []
                for rec in source_records:
                    meta = dict(rec.get("meta") or {})
                    for call in meta.get("tool_calls", []) or []:
                        if isinstance(call, dict):
                            all_tool_calls.append(call)

                content = "\n\n".join(children_abstracts)

                await self._orchestrator.add(
                    uri=dir_uri,
                    abstract=llm_abstract,
                    content=content,
                    category="events",
                    context_type="memory",
                    is_leaf=False,
                    session_id=session_id,
                    meta={
                        **aggregated_meta,
                        "layer": "directory",
                        "ingest_mode": "memory",
                        "msg_range": list(segment["msg_range"]),
                        "source_uri": source_uri or "",
                        "session_id": session_id,
                        "child_count": len(source_records),
                        "child_uris": [str(r.get("uri", "")) for r in source_records],
                        "tool_calls": all_tool_calls if all_tool_calls else [],
                    },
                    overview=llm_overview,
                )

                created_directory_uris.append(dir_uri)

                if keywords_str:
                    try:
                        records = await self._orchestrator._storage.filter(
                            self._orchestrator._get_collection(),
                            {"op": "must", "field": "uri", "conds": [dir_uri]},
                            limit=1,
                        )
                        if records:
                            await self._orchestrator._storage.update(
                                self._orchestrator._get_collection(),
                                str(records[0].get("id", "")),
                                {"keywords": keywords_str},
                            )
                    except Exception:
                        logger.warning(
                            "[ContextManager] Failed to patch keywords for %s", dir_uri
                        )

                fs = getattr(self._orchestrator, "_fs", None)
                if fs is not None:
                    await fs.write_context(
                        uri=dir_uri,
                        content=content,
                        abstract=llm_abstract,
                        abstract_json={
                            "keywords": keywords_list,
                            "child_count": len(source_records),
                        },
                        overview=llm_overview,
                        is_leaf=False,
                    )

            logger.info(
                "[ContextManager] Full recompose completed sid=%s directories=%d leaves_preserved=%d",
                session_id,
                len(created_directory_uris),
                len(merged_records),
            )
            if return_created_uris:
                return list(created_directory_uris)
            return None
        except Exception as exc:
            logger.warning(
                "[ContextManager] Full-session recomposition failed sid=%s tenant=%s user=%s collection=%s source_uri=%s created_dirs=%d: %s",
                session_id,
                tenant_id,
                user_id,
                self._orchestrator._get_collection(),
                source_uri,
                len(created_directory_uris),
                exc,
                exc_info=True,
            )
            if raise_on_error:
                # Hand back the partial URIs to the caller via
                # RecompositionError so its run-scoped cleanup tracker
                # can drive compensation (REVIEW REL-02). The previous
                # inline ``contextlib.suppress`` cleanup was a silent
                # black box on the raise_on_error=True path: any URI
                # that failed to delete inside the suppressed block
                # became an orphan with no signal upstream.
                raise RecompositionError(exc, created_directory_uris) from exc
            # raise_on_error=False (production lifecycle) keeps the
            # legacy best-effort inline cleanup. Production callers do
            # not maintain a tracker and rely on this fallback.
            if created_directory_uris:
                with contextlib.suppress(Exception):
                    await self._purge_records_and_fs_subtree(created_directory_uris)
            if return_created_uris:
                # Failure path with reporting requested: hand back what
                # we created before the failure so the caller's tracker
                # can still record/compensate. ``raise_on_error=False``
                # callers swallow the exception, so they need this hook.
                return list(created_directory_uris)
            return None
        finally:
            reset_request_identity(tokens_for_identity)
            if coll_token is not None:
                reset_collection_name(coll_token)

    async def _generate_session_summary(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: Optional[str],
    ) -> Optional[str]:
        """Generate a session-level summary from directory abstracts (or leaf abstracts as fallback).

        Uses ``load_layers({"merged", "directory"})`` (REVIEW closure
        tracker PERF-01): a single storage scroll returns both layers.
        Previously the directory-present path called
        ``load_directories`` and then ``load_merged``, paying two full
        session scans for the same data.
        """
        layers = await self._session_records.load_layers(
            layers=["merged", "directory"],
            session_id=session_id,
            source_uri=source_uri,
        )
        directory_records = layers.get("directory", [])
        merged_records = layers.get("merged", [])

        abstracts: List[str] = []

        if directory_records:
            # Use directory abstracts as primary source
            dir_child_uris: Set[str] = set()
            for rec in directory_records:
                meta = dict(rec.get("meta") or {})
                for child_uri in meta.get("child_uris", []) or []:
                    if child_uri:
                        dir_child_uris.add(child_uri)
                abstract = str(rec.get("abstract") or "").strip()
                if abstract:
                    abstracts.append(abstract)

            # Include ungrouped leaf abstracts (leaves not in any directory)
            for rec in merged_records:
                uri = str(rec.get("uri", "") or "").strip()
                if uri and uri not in dir_child_uris:
                    abstract = str(rec.get("abstract") or "").strip()
                    if abstract:
                        abstracts.append(abstract)
        else:
            # Fallback: use leaf abstracts directly
            if len(merged_records) < 2:
                return None
            for record in merged_records:
                abstract = str(record.get("abstract") or "").strip()
                if abstract:
                    abstracts.append(abstract)

        if not abstracts:
            return None

        # REVIEW closure tracker R2-21 — 1-directory short-circuit.
        # When recomposition produced exactly one directory whose
        # abstract is the sole contributor to ``abstracts``, the LLM
        # call below would summarize one already-summarized abstract.
        # Wasteful: 1 LLM call + 2 storage scans per session_end with
        # single-cluster recomposition. Promote the directory's
        # existing abstract / overview / topics to the session_summary
        # verbatim instead.
        #
        # The guard requires THREE conditions because a less strict
        # check would shadow ungrouped-leaf content:
        #
        #  1. ``len(directory_records) == 1`` — exactly one directory
        #  2. ``only_dir_abstract`` is non-empty — otherwise the
        #     directory loop's ``.strip()`` filter dropped it from
        #     ``abstracts`` and the single entry came from an
        #     ungrouped leaf (correctness bug if we promote the dir's
        #     empty abstract over the leaf's content).
        #  3. ``len(abstracts) == 1`` AND ``abstracts[0] ==
        #     only_dir_abstract`` — belt+braces: the only entry IS
        #     the directory's abstract, not a coincidentally-equal
        #     leaf abstract.
        only_dir_abstract = (
            str(directory_records[0].get("abstract") or "").strip()
            if len(directory_records) == 1
            else ""
        )
        summary_uri = self._session_summary_uri(tenant_id, user_id, session_id)
        if (
            len(directory_records) == 1
            and only_dir_abstract
            and len(abstracts) == 1
            and abstracts[0] == only_dir_abstract
        ):
            only_dir = directory_records[0]
            only_dir_meta = dict(only_dir.get("meta") or {})
            llm_abstract = only_dir_abstract
            llm_overview = str(only_dir.get("overview") or "")
            topics = only_dir_meta.get("topics") or []
            keywords_list = list(topics) if isinstance(topics, list) else []
        else:
            derived = await self._orchestrator._derive_parent_summary(
                doc_title=session_id,
                children_abstracts=abstracts,
            )
            if not derived:
                return None
            llm_abstract = derived.get("abstract", "")
            llm_overview = derived.get("overview", "")
            keywords_list = derived.get("keywords", [])
        keywords_str = (
            ", ".join(str(k) for k in keywords_list if k)
            if isinstance(keywords_list, list)
            else ""
        )

        content = "\n\n".join(abstracts)

        await self._orchestrator.add(
            uri=summary_uri,
            abstract=llm_abstract,
            content=content,
            category="events",
            context_type="memory",
            is_leaf=False,
            session_id=session_id,
            meta={
                "layer": "session_summary",
                "session_id": session_id,
                "source_uri": source_uri or "",
                "child_count": len(abstracts),
                "topics": keywords_list,
            },
            overview=llm_overview,
        )

        # Patch keywords into the Qdrant record (add() fast-path skips them).
        if keywords_str:
            try:
                records = await self._orchestrator._storage.filter(
                    self._orchestrator._get_collection(),
                    {"op": "must", "field": "uri", "conds": [summary_uri]},
                    limit=1,
                )
                if records:
                    await self._orchestrator._storage.update(
                        self._orchestrator._get_collection(),
                        str(records[0].get("id", "")),
                        {"keywords": keywords_str},
                    )
            except Exception:
                logger.warning(
                    "[ContextManager] Failed to patch keywords for %s", summary_uri
                )

        fs = getattr(self._orchestrator, "_fs", None)
        if fs is not None:
            await fs.write_context(
                uri=summary_uri,
                content=content,
                abstract=llm_abstract,
                abstract_json={
                    "keywords": keywords_list,
                    "child_count": len(abstracts),
                },
                overview=llm_overview,
                is_leaf=False,
            )

        logger.info(
            "[ContextManager] Session summary generated sid=%s uri=%s children=%d",
            session_id,
            summary_uri,
            len(abstracts),
        )
        return summary_uri

    def _merge_trigger_threshold(self) -> int:
        cfg = getattr(self._orchestrator, "_config", None)
        if cfg is None:
            return 6144
        return max(1, int(getattr(cfg, "conversation_merge_token_budget", 6144)))

    @staticmethod
    def _task_failures(results: List[Any]) -> List[BaseException]:
        """Extract task failures from ``asyncio.gather(..., return_exceptions=True)``."""
        return [result for result in results if isinstance(result, BaseException)]

    async def _wait_for_merge_task(self, sk: SessionKey) -> List[BaseException]:
        """Wait until any in-flight background merge for the session finishes."""
        failures = list(self._session_merge_task_failures.pop(sk, []))
        task = self._session_merge_tasks.get(sk)
        if not task:
            return failures
        results = await asyncio.gather(task, return_exceptions=True)
        failures.extend(self._task_failures(results))
        deduped: List[BaseException] = []
        seen_ids: Set[int] = set()
        for failure in failures:
            if id(failure) in seen_ids:
                continue
            seen_ids.add(id(failure))
            deduped.append(failure)
        return deduped

    def _track_session_merge_followup_task(
        self,
        sk: SessionKey,
        task: asyncio.Task,
    ) -> None:
        """Track deferred tasks spawned from a session merge worker."""
        session_tasks = self._session_merge_followup_tasks.setdefault(sk, set())
        session_tasks.add(task)
        self._pending_tasks.add(task)

        def _cleanup(done_task: asyncio.Task) -> None:
            self._pending_tasks.discard(done_task)
            with contextlib.suppress(asyncio.CancelledError):
                exc = done_task.exception()
                if exc is not None:
                    failures = self._session_merge_followup_failures.setdefault(sk, [])
                    failures.append(exc)
            active_tasks = self._session_merge_followup_tasks.get(sk)
            if active_tasks is None:
                return
            active_tasks.discard(done_task)
            if not active_tasks:
                self._session_merge_followup_tasks.pop(sk, None)

        task.add_done_callback(_cleanup)

    async def _wait_for_merge_followup_tasks(
        self, sk: SessionKey
    ) -> List[BaseException]:
        """Wait until deferred follow-up tasks for the session merge finish."""
        failures: List[BaseException] = list(
            self._session_merge_followup_failures.pop(sk, []),
        )
        while True:
            tasks = tuple(self._session_merge_followup_tasks.get(sk, set()))
            if not tasks:
                deduped: List[BaseException] = []
                seen_ids: Set[int] = set()
                for failure in failures:
                    if id(failure) in seen_ids:
                        continue
                    seen_ids.add(id(failure))
                    deduped.append(failure)
                return deduped
            logger.info(
                "[ContextManager] Waiting for merge follow-up tasks sk=%s pending=%d",
                sk,
                len(tasks),
            )
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failures.extend(self._task_failures(results))

    async def _take_merge_snapshot(
        self,
        sk: SessionKey,
        *,
        flush_all: bool,
    ) -> Optional[ConversationBuffer]:
        """Detach the current buffer snapshot for merge processing."""
        merge_lock = self._session_merge_locks.setdefault(sk, asyncio.Lock())
        async with merge_lock:
            buffer = self._conversation_buffers.get(sk)
            if not buffer or not buffer.messages:
                return None
            if not flush_all and buffer.token_count < self._merge_trigger_threshold():
                return None

            snapshot = ConversationBuffer(
                messages=list(buffer.messages),
                token_count=buffer.token_count,
                start_msg_index=buffer.start_msg_index,
                immediate_uris=list(buffer.immediate_uris),
                tool_calls_per_turn=[list(item) for item in buffer.tool_calls_per_turn],
            )
            next_start = buffer.start_msg_index + len(buffer.messages)
            self._conversation_buffers[sk] = ConversationBuffer(
                start_msg_index=next_start
            )
            return snapshot

    async def _restore_merge_snapshot(
        self,
        sk: SessionKey,
        snapshot: ConversationBuffer,
    ) -> None:
        """Restore a detached buffer snapshot after merge failure."""
        merge_lock = self._session_merge_locks.setdefault(sk, asyncio.Lock())
        async with merge_lock:
            current = self._conversation_buffers.get(sk)
            if current is None:
                self._conversation_buffers[sk] = snapshot
                return

            merged = ConversationBuffer(
                messages=list(snapshot.messages) + list(current.messages),
                token_count=snapshot.token_count + current.token_count,
                start_msg_index=snapshot.start_msg_index,
                immediate_uris=list(snapshot.immediate_uris)
                + list(current.immediate_uris),
                tool_calls_per_turn=list(snapshot.tool_calls_per_turn)
                + list(current.tool_calls_per_turn),
            )
            self._conversation_buffers[sk] = merged

    async def _purge_records_and_fs_subtree(self, uris: List[str]) -> None:
        """Purge each URI's record and CortexFS subtree by URI prefix.

        For every input URI:
        - ``storage.remove_by_uri(uri)`` deletes the URI itself AND every
          record whose URI starts with the same prefix (so derived
          children — fact_points, abstract.json, etc. — go with it).
        - ``fs.rm(uri, recursive=True)`` recursively removes the
          CortexFS subtree rooted at the URI's path.

        FS failures are logged but never raise — the storage delete is
        the source of truth, the FS write is fire-and-forget on the
        creation path. Callers don't need to compensate.

        Renamed from ``_delete_immediate_families`` (REVIEW closure
        tracker PE-1) — the old name encoded a stale concept ("immediate
        families" was leftover terminology from the immediate-layer era
        that no longer exists). This function deletes records + FS
        subtrees regardless of layer; the new name reflects that.
        """
        unique_uris: list[str] = []
        for uri in uris:
            normalized = str(uri or "").strip()
            if normalized and normalized not in unique_uris:
                unique_uris.append(normalized)

        fs = getattr(self._orchestrator, "_fs", None)
        for uri in unique_uris:
            await self._orchestrator._storage.remove_by_uri(
                self._orchestrator._get_collection(),
                uri,
            )
            if fs:
                try:
                    await fs.rm(uri, recursive=True)
                except Exception as exc:
                    logger.warning(
                        "[ContextManager] CortexFS cleanup failed for %s: %s",
                        uri,
                        exc,
                    )

    async def _list_immediate_uris(self, session_id: str) -> List[str]:
        """Return current session immediate source URIs for fallback cleanup."""
        records = await self._orchestrator._storage.filter(
            self._orchestrator._get_collection(),
            {"op": "must", "field": "session_id", "conds": [session_id]},
            limit=10000,
        )
        return [
            str(record.get("uri", "")).strip()
            for record in records
            if (
                str(record.get("uri", "")).strip()
                and str((record.get("meta") or {}).get("layer", "") or "")
                == "immediate"
            )
        ]

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
        tokens_for_identity = None
        coll_token = set_collection_name(collection_name) if collection_name else None
        self._session_pending_immediate_cleanup.pop(sk, None)
        try:
            while True:
                snapshot = await self._take_merge_snapshot(
                    sk,
                    flush_all=flush_all,
                )
                if snapshot is None:
                    return
                logger.info(
                    "[ContextManager] Merge start sid=%s tenant=%s user=%s collection=%s flush_all=%s snapshot_messages=%d snapshot_tokens=%d snapshot_immediates=%d start_msg_index=%d",
                    session_id,
                    tenant_id,
                    user_id,
                    self._orchestrator._get_collection(),
                    flush_all,
                    len(snapshot.messages),
                    snapshot.token_count,
                    len(snapshot.immediate_uris),
                    snapshot.start_msg_index,
                )

                records = await self._load_immediate_records(snapshot.immediate_uris)
                source_uri = self._conversation_source_uri(
                    tenant_id,
                    user_id,
                    session_id,
                )
                tail_records = await self._select_tail_merged_records(
                    session_id=session_id,
                    source_uri=source_uri,
                )
                entries = await self._build_recomposition_entries(
                    snapshot=snapshot,
                    immediate_records=records,
                    tail_records=tail_records,
                )
                segments = self._build_recomposition_segments(entries)
                logger.info(
                    "[ContextManager] Merge planned sid=%s immediate_records=%d tail_records=%d entries=%d segments=%d segment_ranges=%s",
                    session_id,
                    len(records),
                    len(tail_records),
                    len(entries),
                    len(segments),
                    [segment.get("msg_range") for segment in segments[:8]],
                )

                all_tool_calls = []
                for tc_list in snapshot.tool_calls_per_turn:
                    all_tool_calls.extend(tc_list)

                if tokens_for_identity is None:
                    tokens_for_identity = set_request_identity(tenant_id, user_id)
                created_merged_uris: List[str] = []
                for segment in segments:
                    if not segment["messages"]:
                        continue
                    combined = "\n\n".join(segment["messages"])
                    aggregated_meta = await self._aggregate_records_metadata(
                        segment["source_records"]
                    )
                    merged_context = await self._orchestrator.add(
                        uri=self._merged_leaf_uri(
                            tenant_id,
                            user_id,
                            session_id,
                            segment["msg_range"],
                        ),
                        abstract="",
                        content=combined,
                        category="events",
                        context_type="memory",
                        meta={
                            **aggregated_meta,
                            "layer": "merged",
                            "ingest_mode": "memory",
                            "msg_range": list(segment["msg_range"]),
                            "source_uri": source_uri,
                            "session_id": session_id,
                            "recomposition_stage": "online_tail",
                            "tool_calls": all_tool_calls if all_tool_calls else [],
                        },
                        session_id=session_id,
                        defer_derive=True,
                    )
                    created_merged_uris.append(merged_context.uri)

                    async def _bounded_derive(
                        sem=self._derive_semaphore,
                        **dkw,
                    ):
                        async with sem:
                            await self._orchestrator._complete_deferred_derive(**dkw)

                    _defer_task = asyncio.create_task(
                        _bounded_derive(
                            uri=merged_context.uri,
                            content=combined,
                            abstract="",
                            overview="",
                            session_id=session_id,
                            meta=aggregated_meta,
                            raise_on_error=True,
                        )
                    )
                    self._track_session_merge_followup_task(sk, _defer_task)
                    _defer_task.add_done_callback(
                        lambda t: (
                            None
                            if t.cancelled()
                            else (
                                logger.warning(
                                    "[ContextManager] deferred derive failed: %s",
                                    t.exception(),
                                )
                                if t.exception()
                                else None
                            )
                        )
                    )

                superseded_merged_uris = self._merge_unique_strings(
                    *[segment.get("superseded_merged_uris", []) for segment in segments]
                )
                superseded_merged_uris = [
                    uri
                    for uri in superseded_merged_uris
                    if uri not in created_merged_uris
                ]
                if superseded_merged_uris:
                    try:
                        await self._purge_records_and_fs_subtree(superseded_merged_uris)
                    except Exception as exc:
                        logger.warning(
                            "[ContextManager] Superseded merged cleanup after merge: %s",
                            exc,
                        )

                if snapshot.immediate_uris:
                    try:
                        await self._purge_records_and_fs_subtree(snapshot.immediate_uris)
                    except Exception as exc:
                        self._session_pending_immediate_cleanup[sk] = True
                        logger.warning(
                            "[ContextManager] Immediate cleanup after merge: %s", exc
                        )
        except Exception as exc:
            logger.error(
                "[ContextManager] Merge failed sid=%s tenant=%s user=%s collection=%s flush_all=%s source_uri=%s snapshot_messages=%s immediate_records=%s tail_records=%s segments=%s created_merged=%s: %s",
                session_id,
                tenant_id,
                user_id,
                self._orchestrator._get_collection(),
                flush_all,
                locals().get("source_uri"),
                len(snapshot.messages)
                if "snapshot" in locals() and snapshot is not None
                else None,
                len(records) if "records" in locals() and records is not None else None,
                len(tail_records)
                if "tail_records" in locals() and tail_records is not None
                else None,
                len(segments)
                if "segments" in locals() and segments is not None
                else None,
                len(created_merged_uris)
                if "created_merged_uris" in locals() and created_merged_uris is not None
                else None,
                exc,
                exc_info=True,
            )
            if "created_merged_uris" in locals() and created_merged_uris:
                with contextlib.suppress(Exception):
                    await self._purge_records_and_fs_subtree(created_merged_uris)
            if "snapshot" in locals() and snapshot is not None:
                await self._restore_merge_snapshot(sk, snapshot)
            if raise_on_error:
                raise
        finally:
            if tokens_for_identity:
                reset_request_identity(tokens_for_identity)
            if coll_token is not None:
                reset_collection_name(coll_token)

    # =========================================================================

    async def _end(
        self,
        session_id: str,
        tenant_id: str,
        user_id: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        sk = self._make_session_key(tenant_id, user_id, session_id)
        total_turns = len(self._committed_turns.get(sk, set()))
        session_project_id = (
            self._session_project_ids.get(sk) or get_effective_project_id()
        )
        project_token = set_request_project_id(session_project_id)
        lock = self._session_locks.setdefault(sk, asyncio.Lock())

        try:
            async with lock:
                start_time = time.monotonic()
                status = "closed"
                traces = 0
                knowledge_candidates = 0
                session_owner_ids = sorted(
                    self._session_memory_owner_ids.get(sk, set())
                )
                source_uri = None
                fail_fast = bool((config or {}).get("fail_fast_end", False))

                def _handle_end_failure(
                    message: str,
                    exc: Optional[BaseException] = None,
                ) -> None:
                    nonlocal status
                    if fail_fast:
                        raise RuntimeError(message) from exc
                    status = "partial"
                    if exc is None:
                        logger.warning("[ContextManager] %s", message)
                    else:
                        logger.warning(
                            "[ContextManager] %s: %s",
                            message,
                            exc,
                            exc_info=(
                                type(exc),
                                exc,
                                exc.__traceback__,
                            ),
                        )

                try:
                    merge_failures = await self._wait_for_merge_task(sk)
                    if fail_fast and merge_failures:
                        _handle_end_failure(
                            "Background merge task failed "
                            f"sid={session_id} tenant={tenant_id} user={user_id} "
                            f"failures={len(merge_failures)}",
                            merge_failures[0],
                        )

                    buffer = self._conversation_buffers.get(sk)
                    if buffer and buffer.messages:
                        try:
                            await self._merge_buffer(
                                sk,
                                session_id,
                                tenant_id,
                                user_id,
                                flush_all=True,
                                raise_on_error=True,
                            )
                        except Exception as exc:
                            _handle_end_failure(
                                "End-of-session buffer flush failed "
                                f"sid={session_id} tenant={tenant_id} user={user_id}",
                                exc,
                            )

                    if self._session_pending_immediate_cleanup.pop(sk, False):
                        try:
                            immediate_uris = await self._list_immediate_uris(session_id)
                            await self._purge_records_and_fs_subtree(immediate_uris)
                        except Exception as exc:
                            _handle_end_failure(
                                "End cleanup immediates failed "
                                f"sid={session_id} tenant={tenant_id} user={user_id}",
                                exc,
                            )

                    try:
                        source_uri = await self._persist_conversation_source(
                            session_id=session_id,
                            tenant_id=tenant_id,
                            user_id=user_id,
                        )
                        result = await self._orchestrator.session_end(
                            session_id=session_id,
                            quality_score=0.5,
                        )
                        traces = result.get("alpha_traces", 0)
                        knowledge_candidates = result.get("knowledge_candidates", 0)
                    except Exception as exc:
                        _handle_end_failure(
                            "session_end failed "
                            f"sid={session_id} tenant={tenant_id} user={user_id}",
                            exc,
                        )

                    if (
                        session_owner_ids
                        and getattr(self._orchestrator, "_autophagy_kernel", None)
                        is not None
                    ):
                        task = asyncio.create_task(
                            self._run_autophagy_metabolism(
                                session_id=session_id,
                                tenant_id=tenant_id,
                                user_id=user_id,
                                owner_ids=session_owner_ids,
                            )
                        )
                        self._pending_tasks.add(task)
                        task.add_done_callback(self._pending_tasks.discard)

                    followup_failures = await self._wait_for_merge_followup_tasks(sk)
                    if fail_fast and followup_failures:
                        _handle_end_failure(
                            "Merge follow-up task failed "
                            f"sid={session_id} tenant={tenant_id} user={user_id} "
                            f"failures={len(followup_failures)}",
                            followup_failures[0],
                        )

                    # Full recompose: re-segment all merged records semantically,
                    # preserve conversation order, delete intermediate chunks.
                    # Await completion so callers see final records, not intermediates.
                    self._spawn_full_recompose_task(
                        sk,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        source_uri=source_uri,
                        raise_on_error=fail_fast,
                    )
                    recompose_task = self._session_full_recompose_tasks.get(sk)
                    if recompose_task is not None:
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(recompose_task),
                                timeout=120.0,
                            )
                        except asyncio.TimeoutError as exc:
                            recompose_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await recompose_task
                            _handle_end_failure(
                                "Full-session recomposition timed out "
                                f"sid={session_id} tenant={tenant_id} user={user_id} timeout=120s",
                                exc,
                            )
                        except Exception as exc:
                            _handle_end_failure(
                                "Full-session recomposition wait failed "
                                f"sid={session_id} tenant={tenant_id} user={user_id}",
                                exc,
                            )

                    # Generate session-level directory summary from final merged records.
                    try:
                        await self._generate_session_summary(
                            session_id=session_id,
                            tenant_id=tenant_id,
                            user_id=user_id,
                            source_uri=source_uri,
                        )
                    except Exception as exc:
                        _handle_end_failure(
                            f"Session summary generation failed sid={session_id}",
                            exc,
                        )

                    layer_counts: Optional[Dict[str, int]] = None
                    try:
                        layer_counts = await self._session_records.layer_counts(
                            session_id,
                            source_uri=source_uri,
                            tenant_id=tenant_id,
                            user_id=user_id,
                        )
                        logger.info(
                            "[ContextManager] End state sid=%s source_uri=%s layer_counts=%s",
                            session_id,
                            source_uri,
                            layer_counts,
                        )
                    except Exception as exc:
                        _handle_end_failure(
                            f"Failed to inspect end state sid={session_id}",
                            exc,
                        )

                    if layer_counts is not None:
                        integrity_errors: List[str] = []
                        if total_turns > 0 and layer_counts.get("merged", 0) == 0:
                            integrity_errors.append("merged=0")
                        if layer_counts.get("immediate", 0) > 0:
                            integrity_errors.append(
                                f"immediate={layer_counts.get('immediate', 0)}",
                            )
                        if (
                            layer_counts.get("merged", 0) >= 2
                            and self._orchestrator._llm_completion is not None
                            and layer_counts.get("session_summary", 0) == 0
                        ):
                            integrity_errors.append("session_summary=0")
                        if integrity_errors:
                            if fail_fast:
                                _handle_end_failure(
                                    "End degraded "
                                    f"sid={session_id} source_uri={source_uri} "
                                    f"layer_counts={layer_counts} integrity_errors={integrity_errors}",
                                )
                            else:
                                logger.warning(
                                    "[ContextManager] End degraded sid=%s source_uri=%s layer_counts=%s integrity_errors=%s",
                                    session_id,
                                    source_uri,
                                    layer_counts,
                                    integrity_errors,
                                )

                    duration_ms = int((time.monotonic() - start_time) * 1000)
                    logger.info(
                        "[ContextManager] end sid=%s tenant=%s user=%s turns=%d traces=%d latency=%dms",
                        session_id,
                        tenant_id,
                        user_id,
                        total_turns,
                        traces,
                        duration_ms,
                    )

                    return {
                        "session_id": session_id,
                        "status": status,
                        "total_turns": total_turns,
                        "traces": traces,
                        "knowledge_candidates": knowledge_candidates,
                        "duration_ms": duration_ms,
                        "source_uri": source_uri,
                    }
                except Exception as exc:
                    duration_ms = int((time.monotonic() - start_time) * 1000)
                    logger.warning(
                        "[ContextManager] end failed sid=%s tenant=%s user=%s latency=%dms fail_fast=%s: %s",
                        session_id,
                        tenant_id,
                        user_id,
                        duration_ms,
                        fail_fast,
                        exc,
                        exc_info=(
                            type(exc),
                            exc,
                            exc.__traceback__,
                        ),
                    )
                    raise
                finally:
                    self._cleanup_session(sk)
        finally:
            reset_request_project_id(project_token)

    # =========================================================================
    # Cache management
    # =========================================================================

    def _cache_prepare(self, cache_key: CacheKey, sk: SessionKey, result: Dict) -> None:
        """Cache prepare result with reverse index for session cleanup."""
        now = time.time()

        # LRU eviction: over 1000 entries → evict oldest
        if len(self._prepare_cache) >= 1000:
            oldest_key = min(
                self._prepare_cache,
                key=lambda k: self._prepare_cache[k][1],
            )
            self._prepare_cache.pop(oldest_key)
            for keys in self._session_cache_keys.values():
                keys.discard(oldest_key)

        self._prepare_cache[cache_key] = (result, now)
        self._session_cache_keys.setdefault(sk, set()).add(cache_key)

    def _get_cached_prepare(self, cache_key: CacheKey) -> Optional[Dict]:
        """Return cached result if exists and not expired."""
        entry = self._prepare_cache.get(cache_key)
        if entry is None:
            return None
        result, ts = entry
        if time.time() - ts > self._prepare_cache_ttl:
            self._prepare_cache.pop(cache_key, None)
            return None
        return result

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
        return (
            self._current_collection_name(),
            tenant_id,
            user_id,
            session_id,
        )

    def _make_cache_key(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        turn_id: str,
    ) -> CacheKey:
        """Build one prepare cache key scoped to the active collection."""
        return (
            self._current_collection_name(),
            tenant_id,
            user_id,
            session_id,
            turn_id,
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
        self._session_activity[sk] = time.time()

    def _remember_session_project(self, sk: SessionKey) -> None:
        self._session_project_ids[sk] = get_effective_project_id()

    def _cleanup_session(self, sk: SessionKey) -> None:
        """Remove all session state including cache entries via reverse index."""
        cache_keys = self._session_cache_keys.pop(sk, set())
        for key in cache_keys:
            self._prepare_cache.pop(key, None)
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
        self._session_memory_owner_ids.pop(sk, None)
        self._session_pending_immediate_cleanup.pop(sk, None)
        # Clean up turn-scoped skill selections for this session
        stale_keys = [k for k in self._selected_skill_uris if k[0] == sk]
        for k in stale_keys:
            del self._selected_skill_uris[k]

    async def _run_autophagy_metabolism(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        owner_ids: List[str],
    ) -> None:
        try:
            await self._orchestrator._autophagy_kernel.metabolize_states(owner_ids)
        except Exception as exc:
            logger.warning(
                "[ContextManager] Autophagy metabolism failed sid=%s tenant=%s user=%s owners=%d: %s",
                session_id,
                tenant_id,
                user_id,
                len(owner_ids),
                exc,
            )

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
    # Formatting helpers
    # =========================================================================

    def _extract_query(self, messages: List[Dict[str, str]]) -> str:
        """Extract the last user message content as query."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return msg.get("content", "").strip()
        return ""

    def _empty_prepare(self, session_id: str, turn_id: str) -> Dict[str, Any]:
        """Return an empty prepare result (no query to search)."""
        return {
            "session_id": session_id,
            "turn_id": turn_id,
            "intent": {
                "should_recall": False,
                "probe_candidate_count": 0,
                "probe_top_score": None,
                "depth": "l1",
            },
            "memory": [],
            "knowledge": [],
            "instructions": self._empty_instructions(),
        }

    async def _expand_directory_hits(
        self,
        find_result,
    ) -> list:
        """Replace directory records with their children leaf records.

        Directories serve as broad semantic surfaces for vector matching.
        When hit, we expand to the actual leaf records for content delivery.
        """
        from opencortex.retrieve.types import DetailLevel

        expanded = []
        seen_uris: set = set()

        for matched in find_result:
            if getattr(matched, "layer", None) != "directory":
                if matched.uri not in seen_uris:
                    expanded.append(matched)
                    seen_uris.add(matched.uri)
                continue

            # Directory hit — load children URIs from meta
            meta = {}
            try:
                records = await self._orchestrator._storage.filter(
                    self._orchestrator._get_collection(),
                    {"op": "must", "field": "uri", "conds": [matched.uri]},
                    limit=1,
                )
                if records:
                    meta = dict(records[0].get("meta") or {})
            except Exception:
                pass

            child_uris = meta.get("child_uris") or []
            if not child_uris:
                continue

            # Load child leaf records
            try:
                children = await self._orchestrator._storage.filter(
                    self._orchestrator._get_collection(),
                    {
                        "op": "or",
                        "conds": [
                            {"op": "must", "field": "uri", "conds": [uri]}
                            for uri in child_uris
                        ],
                    },
                    limit=len(child_uris),
                )
            except Exception:
                continue

            if not children:
                continue

            # Convert to MatchedContext via orchestrator helper
            child_contexts = await self._orchestrator._records_to_matched_contexts(
                candidates=children,
                context_type=matched.context_type,
                detail_level=DetailLevel.L1,
            )
            for child in child_contexts:
                if child.uri in seen_uris:
                    continue
                seen_uris.add(child.uri)
                # Carry the directory's score as a floor for ranking
                if child.score < matched.score:
                    child.score = matched.score
                expanded.append(child)

        # Re-sort by score descending
        expanded.sort(key=lambda m: m.score, reverse=True)
        return expanded

    def _format_memories(
        self,
        find_result,
        detail_level: str,
    ) -> List[Dict[str, Any]]:
        """Format FindResult into response items."""
        items = []
        for matched in find_result:
            item: Dict[str, Any] = {
                "uri": matched.uri,
                "abstract": matched.abstract,
                "score": round(matched.score, 3),
                "context_type": str(matched.context_type),
                "category": matched.category,
            }
            if getattr(matched, "session_id", ""):
                item["session_id"] = matched.session_id
            if getattr(matched, "msg_range", None) is not None:
                item["msg_range"] = list(matched.msg_range)
            if getattr(matched, "source_uri", None):
                item["source_uri"] = matched.source_uri
            if getattr(matched, "recomposition_stage", None):
                item["recomposition_stage"] = matched.recomposition_stage
            item["matched_anchors"] = list(
                getattr(matched, "matched_anchors", []) or []
            )
            if getattr(matched, "cone_used", None) is not None:
                item["cone_used"] = bool(matched.cone_used)
            if detail_level in ("l1", "l2") and matched.overview:
                item["overview"] = self._clamp(matched.overview)
            if detail_level == "l2" and matched.content:
                item["content"] = self._clamp(matched.content)
            items.append(item)
        return items

    def _format_knowledge(
        self,
        results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Format knowledge search results."""
        items = []
        for r in results:
            items.append(
                {
                    "knowledge_id": r.get("knowledge_id", r.get("id", "")),
                    "type": r.get("knowledge_type", ""),
                    "abstract": r.get("abstract", ""),
                    "confidence": r.get("confidence", 0.0),
                }
            )
        return items

    def _clamp(self, text: str) -> str:
        """Limit per-item content to max_content_chars at paragraph boundary."""
        if len(text) <= self._max_content_chars:
            return text
        truncated = smart_truncate(text, self._max_content_chars)
        omitted = len(text) - len(truncated)
        return f"{truncated} [...{omitted} chars omitted]"

    def _build_instructions(
        self,
        retrieval_depth: Optional[str],
        memory_items: List[Dict[str, Any]],
        knowledge_items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build instructions for Agent based on retrieval posture and results."""
        total_items = len(memory_items) + len(knowledge_items)

        if total_items == 0:
            return self._empty_instructions()

        avg_score = sum(m.get("score", 0) for m in memory_items) / max(
            len(memory_items), 1
        )
        max_confidence = max(
            [k.get("confidence", 0) for k in knowledge_items],
            default=0.0,
        )
        confidence = max(avg_score, max_confidence)

        guidance_map = {
            "l0": "Direct memory evidence was found. Prefer concise grounded recall.",
            "l1": "Expanded memory context was retrieved. Synthesize across related items when useful.",
            "l2": "Deep evidence was retrieved. Use detailed context carefully and keep citations grounded.",
        }
        guidance = guidance_map.get(
            retrieval_depth or "",
            "Context available for reference.",
        )

        return {
            "should_cite_memory": confidence >= 0.5,
            "memory_confidence": round(confidence, 3),
            "recall_count": total_items,
            "guidance": guidance,
        }

    @staticmethod
    def _empty_instructions() -> Dict[str, Any]:
        """Return the stable empty-instructions payload shape."""
        return {
            "should_cite_memory": False,
            "memory_confidence": 0.0,
            "recall_count": 0,
            "guidance": "",
        }

    # =========================================================================
    # Skill event helpers
    # =========================================================================

    async def _append_skill_event(
        self,
        session_id: str,
        turn_id: str,
        skill_uri: str,
        tenant_id: str,
        user_id: str,
        event_type: str,
    ) -> None:
        """Append a single skill event to the event store (fire-and-forget safe)."""
        try:
            from datetime import datetime, timezone
            from uuid import uuid4

            from opencortex.skill_engine.types import (
                SkillEvent,
                extract_skill_id_from_uri,
            )

            await self._orchestrator._skill_event_store.append(
                SkillEvent(
                    event_id=uuid4().hex,
                    session_id=session_id,
                    turn_id=turn_id,
                    skill_id=extract_skill_id_from_uri(skill_uri),
                    skill_uri=skill_uri,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    event_type=event_type,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            )
        except Exception:
            pass

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
