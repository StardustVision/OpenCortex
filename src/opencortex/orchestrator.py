# SPDX-License-Identifier: Apache-2.0
"""Memory Orchestrator for OpenCortex.

The orchestrator is the primary user-facing API that wires together all
internal components:

- CortexConfig: tenant/user isolation
- CortexFS: three-layer (L0/L1/L2) filesystem abstraction
- StorageInterface: vector storage (Qdrant-backed)
- Object-aware retrieval executor over canonical memory records
- IntentAnalyzer: LLM-driven session-aware query planning
- EmbedderBase: pluggable embedding

Typical usage::

    from opencortex.orchestrator import MemoryOrchestrator

    orch = MemoryOrchestrator(embedder=my_embedder)
    await orch.init()

    # Add a memory
    await orch.add(
        abstract="User prefers dark theme in all editors",
        category="preferences",
    )

    # Search
    results = await orch.search("What theme does the user prefer?")

    # Feedback (reinforcement)
    await orch.feedback(uri=results.memories[0].uri, reward=1.0)
"""

import asyncio
import hashlib
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from opencortex.cognition.state_types import OwnerType
from opencortex.config import CortexConfig, get_config
from opencortex.core.context import Context
from opencortex.core.message import Message
from opencortex.core.user_id import UserIdentifier
from opencortex.http.request_context import (
    get_effective_identity,
    get_effective_project_id,
)
from opencortex.intent import (
    MemoryExecutor,
    QueryAnchorKind,
    RecallPlanner,
    RetrievalPlan,
    ScopeLevel,
    SearchResult,
)
from opencortex.intent.retrieval_support import (
    anchor_rerank_bonus,
    build_probe_scope_input,
    build_scope_filter,
    build_start_point_filter,
    merge_filter_clauses,
    record_anchor_groups,
)
from opencortex.intent.retrieval_support import (
    probe_candidate_ranks as build_probe_candidate_ranks,
)
from opencortex.intent.retrieval_support import (
    query_anchor_groups as build_query_anchor_groups,
)
from opencortex.memory import (
    MemoryKind,
    memory_abstract_from_record,
    memory_anchor_hits_from_abstract,
    memory_kind_policy,
    memory_merge_signature_from_abstract,
)
from opencortex.models.embedder.base import EmbedderBase
from opencortex.prompts import (
    build_doc_summarization_prompt,
    build_layer_abstract_prompt,
    build_layer_anchor_handles_prompt,
    build_layer_derivation_prompt,
    build_layer_entities_prompt,
    build_layer_fact_points_prompt,
    build_layer_keywords_prompt,
    build_layer_overview_only_prompt,
    build_parent_summarization_prompt,
)
from opencortex.retrieve.intent_analyzer import IntentAnalyzer, LLMCompletionCallable
from opencortex.retrieve.rerank_client import RerankClient
from opencortex.retrieve.rerank_config import RerankConfig
from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
    FindResult,
    MatchedContext,
    QueryResult,
    SearchExplain,
    TypedQuery,
)
from opencortex.retrieve.uri_path_scorer import compute_uri_path_scores
from opencortex.storage.cortex_fs import CortexFS
from opencortex.storage.storage_interface import StorageInterface
from opencortex.utils.json_parse import parse_json_from_response
from opencortex.utils.text import chunked_llm_derive, smart_truncate
from opencortex.utils.uri import CortexURI

logger = logging.getLogger(__name__)

# Default collection name for all context types
_CONTEXT_COLLECTION = "context"

_IMMEDIATE_EMBED_TIMEOUT_SECONDS = 8.0
_IMMEDIATE_LOCAL_FALLBACK_MODEL = "BAAI/bge-m3"


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


def _split_keyword_string(raw_keywords: str) -> List[str]:
    """Split a comma-separated keyword string into normalized tokens."""
    if not raw_keywords:
        return []
    return [
        token.strip()
        for token in str(raw_keywords).split(",")
        if token and token.strip()
    ]


@dataclass
class _DeriveTask:
    """Async document derive task — enqueued by Phase A, processed by worker."""
    parent_uri: str
    content: str
    abstract: str
    chunks: list
    category: str
    context_type: str
    meta: Dict[str, Any]
    session_id: Optional[str]
    source_path: str
    source_doc_id: str
    source_doc_title: str
    tenant_id: str
    user_id: str


class MemoryOrchestrator:
    """Top-level orchestrator for OpenCortex memory operations.

    Wires together storage, filesystem, retrieval, embedding, and
    reward-based feedback scoring into a single coherent API.

    Args:
        config: CortexConfig instance. Uses global config if not provided.
        storage: StorageInterface backend. Must be provided (Qdrant-backed).
        embedder: Embedding model. Required for add/search operations.
        rerank_config: Rerank configuration for retrieval scoring.
        llm_completion: Async callable for IntentAnalyzer (session-aware search).
    """

    def __init__(
        self,
        config: Optional[CortexConfig] = None,
        storage: Optional[StorageInterface] = None,
        embedder: Optional[EmbedderBase] = None,
        rerank_config: Optional[RerankConfig] = None,
        llm_completion: Optional[LLMCompletionCallable] = None,
    ):
        self._config = config or get_config()
        self._storage = storage
        self._embedder = embedder
        self._rerank_config = rerank_config or RerankConfig()
        self._llm_completion = llm_completion
        # Plan 009 / RR-01 (PERF-02 follow-up) — process-lifetime
        # RerankClient singleton owned by the orchestrator. Pre-fix
        # ``admin_search_debug`` constructed a new RerankClient per
        # request and never closed it, leaking one TCP connection per
        # admin call. Lifted here so ``MemoryOrchestrator.close()`` can
        # call ``aclose()`` exactly once on shutdown.
        self._rerank_client: Optional["RerankClient"] = None
        # Plan 010 / Phase 1 — MemoryService extracted from God-Object
        # orchestrator. Lazy-built via ``_memory_service`` property
        # below so existing tests that bypass ``__init__`` via
        # ``MemoryOrchestrator.__new__(MemoryOrchestrator)`` and then
        # call delegated methods (e.g. ``oc.search`` once U3 lands)
        # don't crash on a missing attribute. Construction is sync and
        # cheap, so the property's first-access cost is negligible.
        # ADV-PHASE2-BYPASS-LANDMINE in plan 010 review identified this
        # — defused proactively before it bites Phase 2/3.
        self._memory_service_instance: Optional[Any] = None
        self._knowledge_service_instance: Optional[Any] = None
        self._system_status_service_instance: Optional[Any] = None
        self._background_task_manager_instance: Optional[Any] = None
        self._bootstrapper_instance: Optional[Any] = None
        # Plan 009 / RELY-01 — InsightsAgent (when enabled in
        # ``server.py`` lifespan) holds a second LLMCompletion wrapper
        # whose pool would otherwise leak on shutdown. The lifespan
        # writes it here; close() awaits ``aclose()`` on it next to
        # the primary ``_llm_completion``.
        self._insights_llm_completion: Optional[Any] = None
        # Plan 009 / R5 — connection sweeper bookkeeping. Set None at
        # construction; ``_start_connection_sweeper()`` populates them.
        # Read by /admin/health/connections so the endpoint can show
        # the last sweep status.
        self._connection_sweep_task: Optional[asyncio.Task] = None
        self._connection_sweep_guard: Optional[asyncio.Lock] = None
        self._last_connection_sweep_at: Optional[Any] = None
        self._last_connection_sweep_status: str = "not_started"

        self._fs: Optional[CortexFS] = None
        self._analyzer: Optional[IntentAnalyzer] = None
        self._user: Optional[UserIdentifier] = None
        self._initialized = False

        # Cortex Alpha components (initialized in init() if enabled)
        self._observer = None
        self._trace_store = None
        self._trace_splitter = None
        self._knowledge_store = None
        self._archivist = None
        self._context_manager = None
        self._parser_registry = None
        self._cognitive_state_store = None
        self._candidate_store = None
        self._recall_mutation_engine = None
        self._consolidation_gate = None
        self._cognitive_metabolism_controller = None
        self._autophagy_kernel = None

        # Skill Engine
        self._skill_manager = None
        self._skill_event_store = None
        self._skill_evaluator = None

        # Cone Retrieval
        self._entity_index = None
        self._cone_scorer = None
        self._recall_planner = RecallPlanner(
            cone_enabled=self._config.cone_retrieval_enabled
        )
        self._memory_runtime = MemoryExecutor()
        self._memory_probe = None

        # Document derive worker (async queue for background LLM derive)
        self._derive_queue: asyncio.Queue[Optional[_DeriveTask]] = asyncio.Queue()
        self._derive_worker_task: Optional[asyncio.Task] = None
        self._inflight_derive_uris: set = set()
        self._immediate_fallback_embedder: Optional[EmbedderBase] = None
        self._immediate_fallback_embedder_attempted = False

        # Conversation deferred derive tracking
        self._deferred_derive_count: int = 0

        # Autophagy background sweeps (metabolism)
        self._autophagy_sweep_task: asyncio.Task | None = None
        self._autophagy_startup_sweep_task: asyncio.Task | None = None
        self._autophagy_sweep_cursors: dict[OwnerType, str | None] = {
            OwnerType.MEMORY: None,
            OwnerType.TRACE: None,
        }
        # Guard to serialize sweep execution across startup/periodic triggers.
        self._autophagy_sweep_guard = asyncio.Lock()
        self._recall_bookkeeping_tasks: set[asyncio.Task[Any]] = set()

    # =========================================================================
    # Collection Routing
    # =========================================================================

    def _get_collection(self) -> str:
        """Return active collection name (contextvar override or default)."""
        from opencortex.http.request_context import get_collection_name

        return get_collection_name() or _CONTEXT_COLLECTION

    def _observer_session_id(
        self,
        session_id: str,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> str:
        """Return one observer-only session key scoped by collection and identity."""
        resolved_tenant_id = tenant_id
        resolved_user_id = user_id
        if resolved_tenant_id is None or resolved_user_id is None:
            resolved_tenant_id, resolved_user_id = get_effective_identity()
        return "::".join(
            (
                self._get_collection(),
                resolved_tenant_id,
                resolved_user_id,
                session_id,
            )
        )

    # =========================================================================
    # Initialization
    # =========================================================================

    async def init(self) -> "MemoryOrchestrator":
        """Delegate to SubsystemBootstrapper.init().

        Initializes all internal components via the bootstrapper.
        See ``SubsystemBootstrapper.init`` for the full 11-step
        boot sequence.

        Returns:
            self (for chaining)
        """
        return await self._bootstrapper.init()

    # Delegates to SubsystemBootstrapper (bodies live in bootstrapper.py)

    async def _init_cognition(self) -> None:
        """Delegate to SubsystemBootstrapper._init_cognition."""
        await self._bootstrapper._init_cognition()

    async def _init_alpha(self) -> None:
        """Delegate to SubsystemBootstrapper._init_alpha."""
        await self._bootstrapper._init_alpha()

    async def _init_skill_engine(self) -> None:
        """Delegate to SubsystemBootstrapper._init_skill_engine."""
        await self._bootstrapper._init_skill_engine()

    def _create_default_embedder(self) -> Optional[EmbedderBase]:
        """Delegate to SubsystemBootstrapper._create_default_embedder."""
        return self._bootstrapper._create_default_embedder()

    def _create_local_embedder(self) -> Optional[EmbedderBase]:
        """Delegate to SubsystemBootstrapper._create_local_embedder."""
        return self._bootstrapper._create_local_embedder()

    async def _startup_maintenance(self) -> None:
        """Delegate to SubsystemBootstrapper._startup_maintenance."""
        await self._bootstrapper._startup_maintenance()

    async def _check_and_reembed(self) -> None:
        """Delegate to SubsystemBootstrapper._check_and_reembed."""
        await self._bootstrapper._check_and_reembed()

    def _is_retryable_immediate_embed_exception(self, exc: Exception) -> bool:
        """Return True when immediate remote embedding should fall back locally."""
        if isinstance(exc, TimeoutError):
            return True
        try:
            import httpx

            return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))
        except Exception:
            return False

    def _create_immediate_fallback_embedder(self) -> Optional[EmbedderBase]:
        """Create a local fallback embedder for immediate-write remote failures."""
        try:
            from opencortex.models.embedder.local_embedder import LocalEmbedder

            local_config = {"onnx_intra_op_threads": self._config.onnx_intra_op_threads}
            embedder = LocalEmbedder(
                model_name=_IMMEDIATE_LOCAL_FALLBACK_MODEL,
                config=local_config,
            )
            if not embedder.is_available:
                logger.warning(
                    "[MemoryOrchestrator] Immediate local fallback unavailable "
                    "(model=%s)",
                    _IMMEDIATE_LOCAL_FALLBACK_MODEL,
                )
                return None

            detected_dim = embedder.get_dimension()
            expected_dim = self._config.embedding_dimension or detected_dim
            if expected_dim and detected_dim != expected_dim:
                logger.warning(
                    "[MemoryOrchestrator] Immediate local fallback disabled: "
                    "model=%s dim=%d != configured_dim=%d",
                    _IMMEDIATE_LOCAL_FALLBACK_MODEL,
                    detected_dim,
                    expected_dim,
                )
                embedder.close()
                return None

            logger.info(
                "[MemoryOrchestrator] Created immediate local fallback embedder "
                "(model=%s, dim=%d)",
                _IMMEDIATE_LOCAL_FALLBACK_MODEL,
                detected_dim,
            )
            return self._wrap_with_cache(self._wrap_with_hybrid(embedder))
        except Exception as exc:
            logger.warning(
                "[MemoryOrchestrator] Failed to create immediate local fallback "
                "embedder: %s",
                exc,
            )
            return None

    def _get_immediate_fallback_embedder(self) -> Optional[EmbedderBase]:
        """Return cached immediate local fallback embedder if available."""
        if self._immediate_fallback_embedder_attempted:
            return self._immediate_fallback_embedder
        self._immediate_fallback_embedder_attempted = True
        self._immediate_fallback_embedder = self._create_immediate_fallback_embedder()
        return self._immediate_fallback_embedder

    def _wrap_with_hybrid(self, embedder):
        """Wrap dense embedder with BM25 sparse for hybrid search.

        No-op if embedder is already hybrid.
        """
        from opencortex.models.embedder.base import HybridEmbedderBase

        if isinstance(embedder, HybridEmbedderBase):
            return embedder
        from opencortex.models.embedder.base import CompositeHybridEmbedder
        from opencortex.models.embedder.sparse import BM25SparseEmbedder

        return CompositeHybridEmbedder(embedder, BM25SparseEmbedder())

    def _wrap_with_cache(self, embedder: EmbedderBase) -> EmbedderBase:
        """Wrap an embedder with LRU cache."""
        try:
            from opencortex.models.embedder.cache import CachedEmbedder

            cached = CachedEmbedder(embedder, max_size=10000, ttl_seconds=3600)
            logger.info(
                "[MemoryOrchestrator] Wrapped embedder with LRU cache (max=10000, ttl=3600s)"
            )
            return cached
        except Exception as exc:
            logger.warning("[MemoryOrchestrator] Failed to wrap with cache: %s", exc)
            return embedder

    # =========================================================================
    # Background task lifecycle — delegates (bodies live in BackgroundTaskManager)
    # =========================================================================

    def _start_derive_worker(self) -> None:
        """Delegate to BackgroundTaskManager._start_derive_worker."""
        self._background_task_manager._start_derive_worker()

    def _start_autophagy_sweeper(self) -> None:
        """Delegate to BackgroundTaskManager._start_autophagy_sweeper."""
        self._background_task_manager._start_autophagy_sweeper()

    async def _run_autophagy_sweep_once(self) -> None:
        """Delegate to BackgroundTaskManager._run_autophagy_sweep_once."""
        await self._background_task_manager._run_autophagy_sweep_once()

    def _start_connection_sweeper(self) -> None:
        """Delegate to BackgroundTaskManager._start_connection_sweeper."""
        self._background_task_manager._start_connection_sweeper()

    async def _run_connection_sweep_once(self) -> None:
        """Delegate to BackgroundTaskManager._run_connection_sweep_once."""
        await self._background_task_manager._run_connection_sweep_once()

    async def _recover_pending_derives(self) -> None:
        """Delegate to BackgroundTaskManager._recover_pending_derives."""
        await self._background_task_manager._recover_pending_derives()

    async def _drain_derive_queue(self) -> None:
        """Delegate to BackgroundTaskManager._drain_derive_queue. Test-only."""
        await self._background_task_manager._drain_derive_queue()

    async def derive_status(self, uri: str) -> Dict[str, Any]:
        """Check the async derive status for a document URI."""
        return await self._system_status_service.derive_status(uri)

    async def reembed_all(self) -> int:
        """Re-embed all records with the current embedder."""
        return await self._system_status_service.reembed_all()

    def _get_or_create_rerank_client(self) -> RerankClient:
        """Return the process-lifetime RerankClient singleton (lazy).

        Plan 009: Constructed on first access so the orchestrator's
        normal init path stays cheap (eager construction would fire
        ``_init_local_reranker`` -> fastembed model download in every
        test). Once built, the same instance serves every caller for
        the process lifetime — closes the per-request leak that
        triggered the original CLOSE_WAIT incident.
        """
        if self._rerank_client is None:
            self._rerank_client = RerankClient(
                self._build_rerank_config(),
                llm_completion=self._llm_completion,
            )
        return self._rerank_client

    def _build_rerank_config(self) -> RerankConfig:
        """Build RerankConfig by merging explicit rerank_config with CortexConfig fields.

        Priority: explicit rerank_config > CortexConfig rerank_* fields > defaults.
        """
        # Start from the explicit rerank_config (if provided) or defaults
        base = self._rerank_config or RerankConfig()

        # Overlay CortexConfig rerank_* fields (only if they're set and base is default)
        cfg = self._config
        return RerankConfig(
            model=base.model or cfg.rerank_model,
            api_key=base.api_key or cfg.rerank_api_key or cfg.embedding_api_key,
            api_base=base.api_base or cfg.rerank_api_base,
            threshold=base.threshold or cfg.rerank_threshold,
            provider=getattr(base, "provider", "") or cfg.rerank_provider,
            fusion_beta=getattr(base, "fusion_beta", 0.0) or cfg.rerank_fusion_beta,
            max_candidates=getattr(base, "max_candidates", 0)
            or cfg.rerank_max_candidates,
            use_llm_fallback=getattr(base, "use_llm_fallback", True),
        )

    async def _write_immediate(
        self,
        session_id: str,
        msg_index: int,
        text: str,
        tool_calls: Optional[list] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Write a single message for immediate searchability using the shared contract."""
        from uuid import uuid4

        from opencortex.http.request_context import (
            get_effective_identity,
            get_effective_project_id,
        )
        from opencortex.utils.uri import CortexURI

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

        # Embed without LLM
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
                    and self._is_retryable_immediate_embed_exception(exc)
                ):
                    fallback_embedder = self._get_immediate_fallback_embedder()
                if fallback_embedder is None:
                    raise
                logger.warning(
                    "[Orchestrator] Immediate remote embedding failed; "
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
                        "[Orchestrator] Immediate local fallback embedding failed "
                        "model=%s exc_type=%s exc=%r",
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
            "ttl_expires_at": self._ttl_from_hours(
                self._config.immediate_event_ttl_hours
            ),
            "speaker": str(record_meta.get("speaker", "") or ""),
            "event_date": record_meta.get("event_date"),
        }

        if vector:
            record["vector"] = vector
        if sparse_vector:
            record["sparse_vector"] = sparse_vector

        abstract_json = self._build_abstract_json(
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
        record.update(self._memory_object_payload(abstract_json, is_leaf=True))
        record["abstract_json"] = abstract_json

        record_id = await self._storage.upsert(self._get_collection(), record)
        record["id"] = record_id
        await self._sync_anchor_projection_records(
            source_record=record,
            abstract_json=abstract_json,
        )
        if self._entity_index and explicit_entities:
            self._entity_index.add(
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
                "[MemoryOrchestrator] Immediate CortexFS write failed for %s: %s",
                uri,
                exc,
            )
        return uri

    async def _derive_parent_summary(
        self,
        doc_title: str,
        children_abstracts: List[str],
    ) -> Dict[str, Any]:
        """LLM-derive L1/L0 for a parent/section node from children abstracts."""
        if not self._llm_completion:
            return {}
        try:
            prompt = build_parent_summarization_prompt(doc_title, children_abstracts)
            response = await self._derive_layers_llm_completion(prompt)
            data = parse_json_from_response(response)
            if isinstance(data, dict):
                return {
                    "abstract": str(data.get("abstract") or "").strip()[:200],
                    "overview": str(data.get("overview") or "").strip(),
                    "keywords": data.get("keywords", []),
                }
        except Exception as exc:
            logger.warning(
                "[MemoryOrchestrator] _derive_parent_summary failed for '%s': %s",
                doc_title,
                exc,
            )
        return {}

    async def _derive_layers(
        self,
        user_abstract: str,
        content: str,
        user_overview: str = "",
    ) -> Dict[str, str]:
        """Derive L0/L1/keywords from L2 with LLM assistance.

        Returns {"abstract": str, "overview": str, "keywords": str}
        keywords is a comma-separated string (for Qdrant MatchText).
        """
        # Fast path: user already provided both abstract and overview
        if user_abstract and user_overview:
            return {
                "abstract": user_abstract,
                "overview": user_overview,
                "keywords": "",
                "entities": [],
                "anchor_handles": [],
                "fact_points": [],
            }

        if self._llm_completion:
            if len(content) > 4000:
                try:
                    result = await chunked_llm_derive(
                        content=content,
                        prompt_builder=lambda chunk: build_layer_derivation_prompt(
                            chunk, user_abstract
                        ),
                        llm_fn=self._derive_layers_llm_completion,
                        parse_fn=parse_json_from_response,
                        max_chars_per_chunk=4000,
                    )
                    llm_overview = str(result.get("overview") or "").strip()
                    keywords_list = result.get("keywords", [])
                    if isinstance(keywords_list, list):
                        keywords = ", ".join(str(k) for k in keywords_list if k)
                    else:
                        keywords = str(keywords_list)
                    entities_list = result.get("entities", [])
                    if isinstance(entities_list, list):
                        entities = [str(e).strip().lower() for e in entities_list if e][:20]
                    else:
                        entities = []
                    anchor_handles_list = result.get("anchor_handles", [])
                    if isinstance(anchor_handles_list, list):
                        anchor_handles = [
                            str(handle).strip()
                            for handle in anchor_handles_list
                            if str(handle).strip()
                        ][:6]
                    else:
                        anchor_handles = []
                    fact_points_list = result.get("fact_points", [])
                    if isinstance(fact_points_list, list):
                        fact_points = [
                            str(fp).strip()
                            for fp in fact_points_list
                            if str(fp).strip()
                        ][:8]
                    else:
                        fact_points = []
                    resolved_overview = self._fallback_overview_from_content(
                        user_overview=user_overview or llm_overview,
                        content=content,
                    )
                    derived_abstract = self._derive_abstract_from_overview(
                        user_abstract=user_abstract,
                        overview=resolved_overview,
                        content=content,
                    )
                    return {
                        "abstract": derived_abstract,
                        "overview": resolved_overview,
                        "keywords": keywords,
                        "entities": entities,
                        "anchor_handles": anchor_handles,
                        "fact_points": fact_points,
                    }
                except Exception as e:
                    logger.warning(
                        "[Orchestrator] _derive_layers chunked LLM failed: %s", e
                    )
            try:
                return await self._derive_layers_split_fields(
                    user_abstract=user_abstract,
                    content=content,
                    user_overview=user_overview,
                )
            except Exception as e:
                logger.warning("[Orchestrator] _derive_layers LLM failed: %s", e)

        # No-LLM fallback
        overview = self._fallback_overview_from_content(
            user_overview=user_overview,
            content=content,
        )
        abstract = self._derive_abstract_from_overview(
            user_abstract=user_abstract,
            overview=overview,
            content=content,
        )
        if not user_abstract and not self._llm_completion:
            logger.warning(
                "[Orchestrator] No LLM configured — abstract uses raw content"
            )
        return {
            "abstract": abstract,
            "overview": overview,
            "keywords": "",
            "entities": [],
            "anchor_handles": [],
            "fact_points": [],
        }

    @staticmethod
    def _coerce_derived_string(value: str) -> str:
        """Normalize a derived string field."""
        return str(value or "").strip()

    @staticmethod
    def _coerce_derived_list(
        value: Any,
        *,
        limit: int,
        lowercase: bool = False,
    ) -> List[str]:
        """Normalize a derived list field."""
        if not isinstance(value, list):
            return []
        result: List[str] = []
        for item in value:
            normalized = str(item).strip()
            if not normalized:
                continue
            result.append(normalized.lower() if lowercase else normalized)
            if len(result) >= limit:
                break
        return result

    async def _derive_layers_split_fields(
        self,
        *,
        user_abstract: str,
        content: str,
        user_overview: str,
    ) -> Dict[str, Any]:
        """Derive memory fields with split prompts and bounded inner concurrency."""
        semaphore = asyncio.Semaphore(3)
        prompt_builders = {
            "abstract": build_layer_abstract_prompt,
            "overview": build_layer_overview_only_prompt,
            "keywords": build_layer_keywords_prompt,
            "entities": build_layer_entities_prompt,
            "anchor_handles": build_layer_anchor_handles_prompt,
            "fact_points": build_layer_fact_points_prompt,
        }

        async def _run_field(
            field_name: str, prompt: str
        ) -> tuple[str, Dict[str, Any]]:
            """Run a single LLM derivation prompt and return parsed JSON.

            Args:
                field_name: Name of the derived field (e.g. ``"abstract"``).
                prompt: Fully rendered LLM prompt string.

            Returns:
                Tuple of ``(field_name, parsed_dict)``.
            """
            async with semaphore:
                response = await self._derive_layers_llm_completion(prompt)
            parsed = parse_json_from_response(response)
            return field_name, parsed if isinstance(parsed, dict) else {}

        tasks = [
            asyncio.create_task(
                _run_field(
                    field_name,
                    prompt_builder(content, user_abstract),
                )
            )
            for field_name, prompt_builder in prompt_builders.items()
        ]
        parsed_results = await asyncio.gather(*tasks)
        derived_fields = {field_name: data for field_name, data in parsed_results}

        llm_abstract = self._coerce_derived_string(
            derived_fields.get("abstract", {}).get("abstract")
        )
        llm_overview = self._coerce_derived_string(
            derived_fields.get("overview", {}).get("overview")
        )
        keywords = ", ".join(
            self._coerce_derived_list(
                derived_fields.get("keywords", {}).get("keywords"),
                limit=15,
            )
        )
        entities = self._coerce_derived_list(
            derived_fields.get("entities", {}).get("entities"),
            limit=20,
            lowercase=True,
        )
        anchor_handles = self._coerce_derived_list(
            derived_fields.get("anchor_handles", {}).get("anchor_handles"),
            limit=6,
        )
        fact_points = self._coerce_derived_list(
            derived_fields.get("fact_points", {}).get("fact_points"),
            limit=8,
        )
        resolved_overview = self._fallback_overview_from_content(
            user_overview=user_overview or llm_overview,
            content=content,
        )
        derived_abstract = (
            user_abstract
            or llm_abstract
            or self._derive_abstract_from_overview(
                user_abstract=user_abstract,
                overview=resolved_overview,
                content=content,
            )
        )
        return {
            "abstract": derived_abstract,
            "overview": resolved_overview,
            "keywords": keywords,
            "entities": entities,
            "anchor_handles": anchor_handles,
            "fact_points": fact_points,
        }

    async def _complete_deferred_derive(
        self,
        uri: str,
        content: str,
        abstract: str = "",
        overview: str = "",
        session_id: str = "",
        meta: Optional[Dict[str, Any]] = None,
        context_type: str = "memory",
        raise_on_error: bool = False,
    ) -> None:
        """Run LLM derive for a previously deferred record and update Qdrant + CortexFS."""
        self._deferred_derive_count += 1
        try:
            layers = await self._derive_layers(
                user_abstract=abstract,
                content=content,
                user_overview=overview,
            )
            new_abstract = layers.get("abstract") or abstract
            new_overview = layers.get("overview") or overview
            keywords = layers.get("keywords", "")
            entities = layers.get("entities", [])
            anchor_handles = layers.get("anchor_handles", [])
            fact_points = layers.get("fact_points", [])

            keywords_list = _split_keyword_string(keywords)
            keywords_str = ", ".join(keywords_list)


            vectorize_text = (
                f"{new_abstract} {keywords_str}".strip()
                if keywords_str
                else new_abstract
            )

            loop = asyncio.get_event_loop()
            result = None
            if self._embedder:
                result = await loop.run_in_executor(
                    None, self._embedder.embed, vectorize_text,
                )

            meta = dict(meta or {})
            if keywords_list:
                meta["topics"] = _merge_unique_strings(meta.get("topics"), keywords_list)
            if anchor_handles:
                meta["anchor_handles"] = anchor_handles
            if entities:
                meta["entities"] = entities

            effective_category = self._extract_category_from_uri(uri)
            abstract_json = self._build_abstract_json(
                uri=uri,
                context_type=context_type,
                category=effective_category,
                abstract=new_abstract,
                overview=new_overview,
                content=content,
                entities=entities,
                meta=meta,
                keywords=keywords_list,
                parent_uri=self._derive_parent_uri(uri),
                session_id=session_id,
            )
            abstract_json["fact_points"] = fact_points

            update_payload: Dict[str, Any] = {
                "abstract": new_abstract,
                "overview": new_overview,
                "keywords": keywords_str,
                "entities": entities,
                "abstract_json": abstract_json,
            }
            if result and result.dense_vector:
                update_payload["vector"] = result.dense_vector
            if result and result.sparse_vector:
                update_payload["sparse_vector"] = result.sparse_vector

            existing = await self._get_record_by_uri(uri)
            if existing:
                await self._storage.update(
                    self._get_collection(),
                    str(existing["id"]),
                    update_payload,
                )
                record = dict(existing)
                record.update(update_payload)
                record["abstract_json"] = abstract_json
                await self._sync_anchor_projection_records(
                    source_record=record,
                    abstract_json=abstract_json,
                )

            await self._fs.write_context(
                uri=uri,
                content=content,
                abstract=new_abstract,
                abstract_json=abstract_json,
                overview=new_overview,
                is_leaf=True,
            )
            logger.info(
                "[Orchestrator] deferred derive completed for %s", uri,
            )
        except Exception as exc:
            logger.warning(
                "[Orchestrator] deferred derive failed for %s: %s", uri, exc,
            )
            if raise_on_error:
                raise
        finally:
            self._deferred_derive_count -= 1

    async def wait_deferred_derives(self, poll_interval: float = 1.0) -> None:
        """Wait until all in-flight deferred derives complete."""
        return await self._system_status_service.wait_deferred_derives(poll_interval)

    @staticmethod
    def _fallback_overview_from_content(
        *,
        user_overview: str,
        content: str,
    ) -> str:
        """Build a deterministic overview fallback when LLM output is absent."""
        if user_overview:
            return user_overview

        normalized_content = str(content or "").strip()
        if not normalized_content:
            return ""

        max_chars = min(max(len(normalized_content), 1), 1200)
        overview = smart_truncate(normalized_content, max_chars).strip()
        return overview or normalized_content[:max_chars].strip()

    @staticmethod
    def _is_retryable_layer_derivation_error(exc: Exception) -> bool:
        """Return whether one layer-derivation LLM failure is transient."""
        try:
            import httpx
        except ImportError:
            return False

        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code == 429 or exc.response.status_code >= 500
        return False

    async def _derive_layers_llm_completion(self, prompt: str) -> str:
        """Call layer-derivation LLM with a small bounded retry budget."""
        if self._llm_completion is None:
            raise RuntimeError("LLM completion unavailable")

        retry_delays = (0.0, 0.35, 0.8)
        for attempt, delay in enumerate(retry_delays, start=1):
            if delay > 0.0:
                await asyncio.sleep(delay)
            try:
                return await self._llm_completion(prompt)
            except Exception as exc:
                if (
                    not self._is_retryable_layer_derivation_error(exc)
                    or attempt == len(retry_delays)
                ):
                    raise
                logger.warning(
                    "[Orchestrator] _derive_layers transient LLM failure attempt=%d/%d: %s",
                    attempt,
                    len(retry_delays),
                    exc,
                )

        raise RuntimeError("unreachable")

    @staticmethod
    def _derive_abstract_from_overview(
        *,
        user_abstract: str,
        overview: str,
        content: str,
    ) -> str:
        """Derive a short abstract from a richer overview.

        Extracts the first sentence under ## Summary heading when present,
        otherwise falls back to the first line of the overview text.
        """
        if user_abstract:
            return user_abstract

        overview_text = str(overview or "").strip()
        if overview_text:
            # If overview uses Markdown headings, extract from ## Summary
            summary_text = ""
            in_summary = False
            for line in overview_text.splitlines():
                if line.strip() == "## Summary":
                    in_summary = True
                    continue
                if in_summary and line.strip().startswith("## "):
                    break
                if in_summary and line.strip():
                    summary_text = line.strip()
                    break
            if summary_text:
                first_sentence = re.split(r"(?<=[.!?。！？])\s+", summary_text)[0].strip()
                candidate = first_sentence or summary_text
                if len(candidate) > 200:
                    candidate = smart_truncate(candidate, 200).strip()
                if candidate:
                    return candidate

            # Fallback: first line of overview
            first_line = overview_text.splitlines()[0].strip()
            first_sentence = re.split(r"(?<=[.!?。！？])\s+", first_line)[0].strip()
            candidate = first_sentence or first_line
            if len(candidate) > 200:
                candidate = smart_truncate(candidate, 200).strip()
            if candidate:
                return candidate

        return str(content or "").strip()

    def _build_abstract_json(
        self,
        *,
        uri: str,
        context_type: str,
        category: str,
        abstract: str,
        overview: str,
        content: str,
        entities: List[str],
        meta: Optional[Dict[str, Any]],
        keywords: Optional[List[str]] = None,
        parent_uri: str,
        session_id: str,
    ) -> Dict[str, Any]:
        """Build the fixed shared `.abstract.json` payload for one entry."""
        record = {
            "uri": uri,
            "context_type": context_type,
            "category": category,
            "abstract": abstract,
            "overview": overview,
            "content": content,
            "entities": entities,
            "keywords": keywords or [],
            "metadata": meta or {},
            "parent_uri": parent_uri,
            "session_id": session_id,
        }
        result = memory_abstract_from_record(record).to_dict()
        # Inject anchor_handles from LLM derivation into the anchors list
        # so that _memory_object_payload can project them into anchor_hits.
        meta_dict = meta or {}
        anchor_handles = meta_dict.get("anchor_handles")
        if anchor_handles:
            existing_values = {
                a.get("value", "").lower()
                for a in result.get("anchors") or []
                if isinstance(a, dict)
            }
            for handle in anchor_handles:
                if (
                    isinstance(handle, str)
                    and handle.strip()
                    and handle.lower() not in existing_values
                ):
                    result.setdefault("anchors", []).append({
                        "anchor_type": "handle",
                        "value": handle.strip(),
                        "text": handle.strip(),
                    })
                    existing_values.add(handle.lower())
        return result

    @staticmethod
    def _memory_object_payload(
        abstract_json: Dict[str, Any],
        *,
        is_leaf: bool,
    ) -> Dict[str, Any]:
        """Project canonical abstract payload into flat vector metadata."""
        memory_kind = MemoryKind(str(abstract_json["memory_kind"]))
        policy = memory_kind_policy(memory_kind)
        anchor_hits = memory_anchor_hits_from_abstract(abstract_json)
        return {
            "memory_kind": memory_kind.value,
            "anchor_hits": anchor_hits,
            "merge_signature": memory_merge_signature_from_abstract(abstract_json),
            "mergeable": policy.mergeable,
            "retrieval_surface": "l0_object" if is_leaf else "",
            "anchor_surface": bool(is_leaf and anchor_hits),
        }

    @staticmethod
    def _anchor_projection_prefix(uri: str) -> str:
        """Return the reserved child prefix for derived anchor projection records."""
        return f"{uri}/anchors"

    @staticmethod
    def _fact_point_prefix(uri: str) -> str:
        """Return the reserved child prefix for derived fact point records."""
        return f"{uri}/fact_points"

    @staticmethod
    def _is_valid_fact_point(text: str) -> bool:
        """Quality gate: return True only if text is a short, concrete atomic fact.

        Rejects: too short (<8), too long (>80), multiline (paragraph-style),
        or generic text lacking any concrete signal.
        Accepts: text containing digits, CamelCase, ALLCAPS, paths, CJK sequences.
        """
        if not text or len(text) < 8 or len(text) > 80:
            return False
        if "\n" in text:
            return False
        # Must contain at least one concrete signal:
        # digits, CamelCase, ALL_CAPS, paths, or 2+ consecutive CJK characters
        concrete_signal = re.compile(
            r"[\d]"                     # digit
            r"|[A-Z][a-z]+[A-Z]"        # CamelCase
            r"|[A-Z]{2,}"               # ALLCAPS (2+ uppercase)
            r"|[\u4e00-\u9fa5].*[\d]"   # CJK text with digit
            r"|[/\\.]"                  # path separator
            r"|[\u4e00-\u9fa5]{2,}"     # 2+ consecutive CJK chars (Chinese proper nouns)
        )
        return bool(concrete_signal.search(text))

    def _fact_point_records(
        self,
        *,
        source_record: Dict[str, Any],
        fact_points_list: List[str],
    ) -> List[Dict[str, Any]]:
        """Build fact_point projection records for one leaf object.

        Applies quality gate and caps at 8 records.
        """
        source_uri = str(source_record.get("uri", "") or "")
        if not source_uri:
            return []

        prefix = self._fact_point_prefix(source_uri)
        records: List[Dict[str, Any]] = []

        for text in fact_points_list:
            if len(records) >= 8:
                break
            if not self._is_valid_fact_point(text):
                continue
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
            fp_record = {
                "id": uuid4().hex,
                "uri": f"{prefix}/{digest}",
                "parent_uri": source_uri,
                "is_leaf": False,
                "abstract": "",
                "overview": text,
                "content": "",
                "retrieval_surface": "fact_point",
                "anchor_surface": False,
                "meta": {
                    "derived": True,
                    "derived_kind": "fact_point",
                    "projection_target_uri": source_uri,
                },
                "projection_target_uri": source_uri,
                # Inherit access control from source leaf
                "context_type": source_record.get("context_type", ""),
                "category": source_record.get("category", ""),
                "scope": source_record.get("scope", ""),
                "source_user_id": source_record.get("source_user_id", ""),
                "source_tenant_id": source_record.get("source_tenant_id", ""),
                "session_id": source_record.get("session_id", ""),
                "project_id": source_record.get("project_id", ""),
                "memory_kind": source_record.get("memory_kind", ""),
                "source_doc_id": source_record.get("source_doc_id", ""),
                "source_doc_title": source_record.get("source_doc_title", ""),
                "source_section_path": source_record.get("source_section_path", ""),
                "keywords": text,
                "entities": source_record.get("entities", []),
                "mergeable": False,
                "merge_signature": "",
                "anchor_hits": "",
            }
            records.append(fp_record)

        return records

    def _anchor_projection_records(
        self,
        *,
        source_record: Dict[str, Any],
        abstract_json: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Build dedicated anchor projection records for one leaf object."""
        source_uri = str(source_record.get("uri", "") or "")
        if not source_uri:
            return []

        projection_records: List[Dict[str, Any]] = []
        anchors = abstract_json.get("anchors") or []
        prefix = self._anchor_projection_prefix(source_uri)
        base_anchor_hits = memory_anchor_hits_from_abstract(abstract_json)

        for index, anchor in enumerate(anchors):
            if not isinstance(anchor, dict):
                continue
            anchor_text = str(anchor.get("text") or anchor.get("value") or "").strip()
            anchor_value = str(anchor.get("value") or anchor_text).strip()
            anchor_type = str(anchor.get("anchor_type") or "topic").strip() or "topic"
            if not anchor_text:
                continue
            # R11: skip anchors that are too short to be meaningful
            if len(anchor_text) < 4:
                continue

            digest = hashlib.sha1(
                f"{anchor_type}:{anchor_value}:{index}".encode("utf-8")
            ).hexdigest()[:12]
            projection_uri = f"{prefix}/{digest}"
            projection_record = {
                "id": uuid4().hex,
                "uri": projection_uri,
                "parent_uri": source_uri,
                "is_leaf": False,
                "abstract": "",
                "overview": (
                    anchor_text
                    if len(anchor_text) >= 15
                    else f"{anchor_type}: {anchor_text}"
                ),
                "content": "",
                "context_type": source_record.get("context_type", ""),
                "category": source_record.get("category", ""),
                "scope": source_record.get("scope", ""),
                "source_user_id": source_record.get("source_user_id", ""),
                "source_tenant_id": source_record.get("source_tenant_id", ""),
                "session_id": source_record.get("session_id", ""),
                "project_id": source_record.get("project_id", ""),
                "keywords": ", ".join(value for value in [anchor_text, anchor_value] if value),
                "entities": source_record.get("entities", []),
                "meta": {
                    "derived": True,
                    "derived_kind": "anchor_projection",
                    "anchor_type": anchor_type,
                    "anchor_value": anchor_value,
                    "anchor_text": anchor_text,
                    "projection_target_uri": source_uri,
                },
                "memory_kind": source_record.get("memory_kind", ""),
                "anchor_hits": _merge_unique_strings(anchor_text, anchor_value, *base_anchor_hits),
                "merge_signature": "",
                "mergeable": False,
                "retrieval_surface": "anchor_projection",
                "anchor_surface": True,
                "source_doc_id": source_record.get("source_doc_id", ""),
                "source_doc_title": source_record.get("source_doc_title", ""),
                "source_section_path": source_record.get("source_section_path", ""),
                "chunk_role": source_record.get("chunk_role", ""),
                "speaker": source_record.get("speaker", ""),
                "event_date": source_record.get("event_date"),
                "projection_target_uri": source_uri,
                "projection_target_abstract": source_record.get("abstract", ""),
                "projection_target_overview": source_record.get("overview", ""),
            }
            projection_records.append(projection_record)

        return projection_records

    async def _delete_derived_stale(
        self,
        collection: str,
        prefix: str,
        keep_uris: set,
    ) -> None:
        """Delete derived records under *prefix* whose URIs are not in *keep_uris*.

        This implements the write-then-delete contract: caller writes new records
        first, then calls this to remove only the records that were NOT just written.
        Records with matching URIs (same content → same digest) are kept.

        Filter DSL ``op=prefix`` on the Qdrant adapter is tokenised (MatchText)
        and over-matches when a sibling URI shares a literal substring with
        *prefix* (see tests/test_cascade_qdrant_integration.py,
        ``test_delete_derived_stale_does_not_touch_sibling_prefix``).  To avoid
        deleting sibling records that happen to token-match, every candidate
        URI is re-checked with literal ``startswith`` before it is marked
        stale.
        """
        try:
            old_records = await self._storage.filter(
                collection,
                {"op": "prefix", "field": "uri", "prefix": prefix},
                limit=50,
            )
        except Exception as exc:
            logger.warning(
                "[Orchestrator] _delete_derived_stale filter failed prefix=%s: %s",
                prefix, exc,
            )
            return
        descendant_prefix = prefix if prefix.endswith("/") else prefix + "/"
        stale_ids = [
            str(r["id"])
            for r in old_records
            if isinstance(r.get("uri"), str)
            and (r["uri"] == prefix or r["uri"].startswith(descendant_prefix))
            and r["uri"] not in keep_uris
        ]
        if stale_ids:
            try:
                await self._storage.delete(collection, stale_ids)
            except Exception as exc:
                logger.warning(
                    "[Orchestrator] _delete_derived_stale delete failed: %s", exc
                )

    async def _sync_anchor_projection_records(
        self,
        *,
        source_record: Dict[str, Any],
        abstract_json: Dict[str, Any],
    ) -> None:
        """Replace derived anchor and fact_point records for one leaf object.

        Ordering: write-new-then-delete-old (R25).
        Both anchor projections and fact_points are embedded in a single
        embed_batch() call for efficiency.
        """
        if not bool(source_record.get("is_leaf", False)):
            return

        source_uri = str(source_record.get("uri", "") or "")
        if not source_uri:
            return

        anchor_prefix = self._anchor_projection_prefix(source_uri)
        fp_prefix = self._fact_point_prefix(source_uri)

        # Build new anchor records
        anchor_records = self._anchor_projection_records(
            source_record=source_record,
            abstract_json=abstract_json,
        )

        # Build new fact_point records from abstract_json
        raw_fact_points = abstract_json.get("fact_points") or []
        if not isinstance(raw_fact_points, list):
            raw_fact_points = []
        fp_records = self._fact_point_records(
            source_record=source_record,
            fact_points_list=raw_fact_points,
        )

        all_new_records = anchor_records + fp_records

        # REVIEW closure tracker R3-P-06 — short-circuit when the new
        # projection is empty AND there's no abstract_json input.
        # The defer_derive initial leaf write hits this path with both
        # inputs empty and would otherwise pay 2 stale-filter scans
        # over Qdrant prefixes that are guaranteed to be empty (the
        # leaf is brand new). The ``not abstract_json`` guard keeps
        # the cleanup path on legitimate update flows where
        # abstract_json is present but happens to contribute no
        # anchors or fact_points — those updates DO need stale
        # cleanup.
        if not all_new_records and not abstract_json:
            return

        # Embed all texts in a single batch call
        if all_new_records and self._embedder:
            texts = [r["overview"] for r in all_new_records]
            loop = asyncio.get_running_loop()
            try:
                embed_results = await asyncio.wait_for(
                    loop.run_in_executor(None, self._embedder.embed_batch, texts),
                    timeout=5.0,
                )
                for record, embed_result in zip(all_new_records, embed_results):
                    if embed_result.dense_vector:
                        record["vector"] = embed_result.dense_vector
                    if getattr(embed_result, "sparse_vector", None):
                        record["sparse_vector"] = embed_result.sparse_vector
            except Exception as exc:
                logger.warning("[Orchestrator] derived records embed_batch failed: %s", exc)

        # Write new records FIRST (write-then-delete)
        for new_record in all_new_records:
            await self._storage.upsert(self._get_collection(), new_record)

        # Only THEN delete stale records (those NOT in the new set)
        new_anchor_uris = {r["uri"] for r in anchor_records}
        new_fp_uris = {r["uri"] for r in fp_records}
        collection = self._get_collection()
        await self._delete_derived_stale(collection, anchor_prefix, new_anchor_uris)
        await self._delete_derived_stale(collection, fp_prefix, new_fp_uris)

    def _ensure_init(self) -> None:
        """Raise if not initialized."""
        if not self._initialized:
            raise RuntimeError(
                "MemoryOrchestrator not initialized. Call `await orch.init()` first."
            )

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def config(self) -> CortexConfig:
        """Active CortexConfig for this orchestrator instance."""
        return self._config

    @property
    def storage(self) -> StorageInterface:
        """Initialized storage backend (Qdrant adapter)."""
        self._ensure_init()
        return self._storage

    @property
    def fs(self) -> CortexFS:
        """Initialized CortexFS three-layer filesystem."""
        self._ensure_init()
        return self._fs

    @property
    def user(self) -> UserIdentifier:
        """Current effective user identity."""
        self._ensure_init()
        return self._user

    @property
    def _memory_service(self) -> "MemoryService":
        """Lazy-built MemoryService for delegated CRUD/query/scoring methods.

        Phase 1 of plan 010 introduced this back-reference pattern. Lazy
        construction (instead of eager-init in ``__init__``) means tests
        that bypass ``__init__`` via ``MemoryOrchestrator.__new__`` and
        then call a delegated method (the typical perf-test fixture
        pattern in ``tests/test_perf_fixes.py``) get a working service
        instance instead of an ``AttributeError``. Construction is sync
        and cheap (back-reference store only); first-access cost is
        negligible. Uses ``getattr`` with default so the attribute is
        not required to exist on the instance — defends against the
        ``__new__`` bypass which skips ``__init__`` entirely.
        """
        cached = getattr(self, "_memory_service_instance", None)
        if cached is None:
            from opencortex.services.memory_service import MemoryService
            cached = MemoryService(self)
            self._memory_service_instance = cached
        return cached

    @property
    def _knowledge_service(self) -> "KnowledgeService":
        """Lazy-built KnowledgeService for delegated knowledge methods.

        Phase 2 of plan 012 mirrors the ``_memory_service`` lazy-property
        pattern. Uses ``getattr`` with default so ``__new__`` bypass
        tests don't crash on missing attribute.
        """
        from opencortex.services.knowledge_service import KnowledgeService

        cached = getattr(self, "_knowledge_service_instance", None)
        if cached is None:
            cached = KnowledgeService(self)
            self._knowledge_service_instance = cached
        return cached

    @property
    def _system_status_service(self) -> "SystemStatusService":
        """Lazy-built SystemStatusService for delegated status methods.

        Phase 4 of plan 013 mirrors the ``_knowledge_service`` lazy-property
        pattern. Uses ``getattr`` with default so ``__new__`` bypass
        tests don't crash on missing attribute.
        """
        from opencortex.services.system_status_service import SystemStatusService

        cached = getattr(self, "_system_status_service_instance", None)
        if cached is None:
            cached = SystemStatusService(self)
            self._system_status_service_instance = cached
        return cached

    @property
    def _background_task_manager(self) -> "BackgroundTaskManager":
        """Lazy-built BackgroundTaskManager for delegated lifecycle methods.

        Phase 3 of plan 014 mirrors the ``_system_status_service`` lazy-property
        pattern. Uses ``getattr`` with default so ``__new__`` bypass
        tests don't crash on missing attribute.
        """
        from opencortex.lifecycle.background_tasks import BackgroundTaskManager

        cached = getattr(self, "_background_task_manager_instance", None)
        if cached is None:
            cached = BackgroundTaskManager(self)
            self._background_task_manager_instance = cached
        return cached

    @property
    def _bootstrapper(self) -> "SubsystemBootstrapper":
        """Lazy-built SubsystemBootstrapper for subsystem creation and wiring.

        Phase 5 of plan 015 mirrors the ``_background_task_manager``
        lazy-property pattern. Uses ``getattr`` with default so
        ``__new__`` bypass tests don't crash on missing attribute.
        """
        from opencortex.lifecycle.bootstrapper import SubsystemBootstrapper

        cached = getattr(self, "_bootstrapper_instance", None)
        if cached is None:
            cached = SubsystemBootstrapper(self)
            self._bootstrapper_instance = cached
        return cached

    # =========================================================================
    # Add / Update / Remove
    # =========================================================================

    async def add(
        self,
        abstract: str,
        content: str = "",
        overview: str = "",
        category: str = "",
        parent_uri: Optional[str] = None,
        uri: Optional[str] = None,
        context_type: Optional[str] = None,
        is_leaf: bool = True,
        meta: Optional[Dict[str, Any]] = None,
        related_uri: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        dedup: bool = False,
        dedup_threshold: float = 0.82,
        embed_text: str = "",
        defer_derive: bool = False,
    ) -> Context:
        """Delegate to ``MemoryService.add`` (plan 011)."""
        return await self._memory_service.add(
            abstract=abstract,
            content=content,
            overview=overview,
            category=category,
            parent_uri=parent_uri,
            uri=uri,
            context_type=context_type,
            is_leaf=is_leaf,
            meta=meta,
            related_uri=related_uri,
            session_id=session_id,
            dedup=dedup,
            dedup_threshold=dedup_threshold,
            embed_text=embed_text,
            defer_derive=defer_derive,
        )

    def _ttl_from_hours(self, hours: int) -> str:
        """Return RFC3339 UTC expiry string. Non-positive values disable TTL."""
        if hours <= 0:
            return ""
        expires = datetime.now(timezone.utc) + timedelta(hours=hours)
        return expires.strftime("%Y-%m-%dT%H:%M:%SZ")

    async def update(
        self,
        uri: str,
        abstract: Optional[str] = None,
        content: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        overview: Optional[str] = None,
    ) -> bool:
        """Delegate to ``MemoryService.update`` (plan 010 / Phase 1)."""
        return await self._memory_service.update(
            uri,
            abstract=abstract,
            content=content,
            meta=meta,
            overview=overview,
        )


    async def remove(self, uri: str, recursive: bool = True) -> int:
        """Delegate to ``MemoryService.remove`` (plan 010 / Phase 1)."""
        return await self._memory_service.remove(uri, recursive=recursive)

    async def _merge_into(
        self, existing_uri: str, new_abstract: str, new_content: str
    ) -> None:
        """Delegate to ``MemoryService._merge_into`` (plan 011)."""
        return await self._memory_service._merge_into(existing_uri, new_abstract, new_content)

    # =========================================================================
    # Search / Retrieve
    # =========================================================================

    async def probe_memory(
        self,
        query: str,
        *,
        context_type: Optional[ContextType] = None,
        target_uri: str = "",
        target_doc_id: Optional[str] = None,
        session_context: Optional[Dict[str, Any]] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
    ) -> SearchResult:
        """Run the Phase 1 bootstrap probe."""
        self._ensure_init()
        if self._memory_probe is None:
            raise RuntimeError("memory probe is not initialized")
        scope_input = build_probe_scope_input(
            context_type=context_type,
            target_uri=target_uri,
            target_doc_id=target_doc_id,
            session_context=session_context,
        )
        scope_filter = build_scope_filter(
            context_type=context_type,
            target_uri=target_uri,
            target_doc_id=target_doc_id,
            session_context=session_context,
            metadata_filter=metadata_filter,
        )
        return await self._memory_probe.probe(
            query,
            scope_filter=scope_filter,
            scope_input=scope_input,
        )

    def memory_probe_mode(self) -> str:
        """Return the active probe backend."""
        if self._memory_probe is None:
            return "unavailable"
        return self._memory_probe.mode

    def memory_probe_trace(self) -> Dict[str, Any]:
        """Return machine-readable attribution for the last probe call."""
        if self._memory_probe is None:
            return {}
        return self._memory_probe.probe_trace()

    def plan_memory(
        self,
        *,
        query: str,
        probe_result: SearchResult,
        max_items: int,
        recall_mode: str,
        detail_level_override: Optional[str],
        scope_input: Optional[Any] = None,
    ) -> Optional[RetrievalPlan]:
        """Run the Phase 2 evidence-driven planner."""
        return self._recall_planner.semantic_plan(
            query=query,
            probe_result=probe_result,
            max_items=max_items,
            recall_mode=recall_mode,
            detail_level_override=detail_level_override,
            scope_input=scope_input,
        )

    def bind_memory_runtime(
        self,
        *,
        probe_result: SearchResult,
        retrieve_plan: RetrievalPlan,
        max_items: int,
        session_context: Optional[Dict[str, Any]],
        include_knowledge: bool,
    ) -> Dict[str, Any]:
        """Run the Phase 3 runtime binder."""
        tid, uid = get_effective_identity()
        return self._memory_runtime.bind(
            probe_result=probe_result,
            retrieve_plan=retrieve_plan,
            max_items=max_items,
            session_id=(session_context or {}).get("session_id", ""),
            tenant_id=tid,
            user_id=uid,
            project_id=get_effective_project_id(),
            include_knowledge=include_knowledge,
        )

    def _build_search_filter(
        self,
        *,
        category_filter: Optional[List[str]] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build the shared search filter used by probe and retrieval."""
        tid, uid = get_effective_identity()

        staging_exclude = {
            "op": "must_not",
            "field": "context_type",
            "conds": ["staging"],
        }
        scope_filter = {
            "op": "or",
            "conds": [
                {"op": "must", "field": "scope", "conds": ["shared", ""]},
                {
                    "op": "and",
                    "conds": [
                        {"op": "must", "field": "scope", "conds": ["private"]},
                        {"op": "must", "field": "source_user_id", "conds": [uid]},
                    ],
                },
            ],
        }

        combined_conds = [staging_exclude, scope_filter]
        if tid:
            combined_conds.append(
                {
                    "op": "must",
                    "field": "source_tenant_id",
                    "conds": [tid, ""],
                }
            )

        project_id = get_effective_project_id()
        if project_id and project_id != "public":
            combined_conds.append(
                {
                    "op": "or",
                    "conds": [
                        {
                            "op": "must",
                            "field": "project_id",
                            "conds": [project_id, "public"],
                        },
                    ],
                }
            )

        if category_filter:
            combined_conds.append(
                {"op": "must", "field": "category", "conds": list(category_filter)}
            )

        combined_conds.append(
            {"op": "must_not", "field": "meta.superseded", "conds": [True]}
        )

        if metadata_filter:
            return {"op": "and", "conds": [metadata_filter] + combined_conds}
        return {"op": "and", "conds": combined_conds}

    def _build_probe_filter(self) -> Dict[str, Any]:
        """Return the bounded Phase 1 probe filter."""
        return self._build_search_filter()

    def _cone_query_entities(
        self,
        *,
        typed_query: TypedQuery,
        query_anchor_groups: Dict[str, set[str]],
        records: List[Dict[str, Any]],
    ) -> set[str]:
        """Choose bounded query entities for cone expansion and scoring."""
        entities = set(query_anchor_groups.get(QueryAnchorKind.ENTITY.value, set()))
        if entities:
            return set(sorted(entities, key=len, reverse=True)[:6])
        if self._cone_scorer is None:
            return set()
        extracted = self._cone_scorer.extract_query_entities(
            typed_query.query,
            records,
            self._get_collection(),
        )
        if not extracted:
            return set()
        return set(sorted(extracted, key=len, reverse=True)[:6])

    async def _apply_cone_rerank(
        self,
        *,
        typed_query: TypedQuery,
        retrieve_plan: Optional[RetrievalPlan],
        query_anchor_groups: Dict[str, set[str]],
        records: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], bool]:
        """Apply lightweight cone expansion/scoring over the dense candidate pool."""
        if (
            self._cone_scorer is None
            or self._entity_index is None
            or retrieve_plan is None
            or retrieve_plan.search_profile.association_budget <= 0.0
            or not records
        ):
            return records, False

        collection = self._get_collection()
        if not self._entity_index.is_ready(collection):
            await self._entity_index.build_for_collection(
                self._storage, collection
            )
        if not self._entity_index.is_ready(collection):
            return records, False

        query_entities = self._cone_query_entities(
            typed_query=typed_query,
            query_anchor_groups=query_anchor_groups,
            records=records,
        )
        if not query_entities:
            return records, False

        tid, uid = get_effective_identity()
        project_id = get_effective_project_id()
        cone_candidates = [dict(record) for record in records]
        cone_candidates.sort(
            key=lambda record: float(record.get("_score", record.get("score", 0.0)) or 0.0),
            reverse=True,
        )
        cone_candidates = await self._cone_scorer.expand_candidates(
            cone_candidates,
            query_entities,
            self._get_collection(),
            self._storage,
            tenant_id=tid,
            user_id=uid,
            project_id=project_id,
        )
        cone_candidates = self._cone_scorer.compute_cone_scores(
            cone_candidates,
            query_entities,
            self._get_collection(),
        )
        return cone_candidates, True

    async def _embed_retrieval_query(self, query_text: str) -> Optional[List[float]]:
        """Embed one retrieval query for dense search."""
        if not self._embedder or not query_text:
            return None
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                self._embedder.embed_query,
                query_text,
            )
        except Exception:
            return None
        return getattr(result, "dense_vector", None)

    def _score_object_record(
        self,
        *,
        record: Dict[str, Any],
        typed_query: TypedQuery,
        retrieve_plan: Optional[RetrievalPlan],
        query_anchor_groups: Dict[str, set[str]],
        probe_candidate_ranks: Dict[str, int],
        cone_weight: float,
        uri_path_costs: Optional[Dict[str, float]] = None,
    ) -> tuple[float, str]:
        """Fuse URI path score (primary) with object-aware boosts."""
        leaf_uri = str(record.get("uri", "") or "")
        if uri_path_costs is not None and leaf_uri in uri_path_costs:
            # URI path score replaces raw cosine _score as the base. Note this
            # introduces a score offset vs the original vector similarity:
            #   direct path:    score = cosine - URI_DIRECT_PENALTY (-0.15)
            #   anchor path:    score = cosine - URI_HOP_COST       (-0.05)
            #   fp path:        score = cosine - URI_HOP_COST       (-0.05)
            #   fp high-conf:   score = cosine - URI_HOP_COST * 0.5 (-0.025)
            # Callers passing a fixed score_threshold should account for this
            # shift (e.g. a 0.80 cosine threshold becomes ~0.65 for direct hits).
            # See uri_path_scorer.py module docstring for full semantics.
            score = 1.0 - uri_path_costs[leaf_uri]
        else:
            score = float(record.get("_score", record.get("score", 0.0)) or 0.0)
        reasons: List[str] = []
        target_kinds = (
            [kind.value for kind in retrieve_plan.target_memory_kinds]
            if retrieve_plan is not None
            else []
        )
        record_kind = str(record.get("memory_kind", ""))
        if record_kind in target_kinds:
            kind_rank = target_kinds.index(record_kind)
            score += 0.14 * (len(target_kinds) - kind_rank) / max(len(target_kinds), 1)
            reasons.append("kind")

        anchor_bonus, anchor_reasons = anchor_rerank_bonus(
            query_anchor_groups=query_anchor_groups,
            record_anchor_groups=record_anchor_groups(record),
        )
        if anchor_bonus > 0:
            score += anchor_bonus
            reasons.extend(anchor_reasons)

        probe_rank = probe_candidate_ranks.get(str(record.get("uri", "") or ""))
        if probe_rank is not None:
            score += max(0.04, 0.14 - min(probe_rank, 5) * 0.02)
            reasons.append("probe")

        if typed_query.target_directories and any(
            str(record.get("uri", "")).startswith(prefix)
            for prefix in typed_query.target_directories
        ):
            score += 0.06
            reasons.append("scope")

        if typed_query.target_doc_id and (
            str(record.get("source_doc_id", "")) == typed_query.target_doc_id
        ):
            score += 0.08
            reasons.append("doc")

        reward = float(record.get("reward_score", 0.0) or 0.0)
        if reward:
            score += max(min(0.06, reward * 0.03), -0.03)
            reasons.append("reward")

        active_count = int(record.get("active_count", 0) or 0)
        if active_count > 0:
            score += min(0.05, math.log1p(active_count) * 0.01)
            reasons.append("hot")

        cone_bonus = float(record.get("_cone_bonus", 0.0) or 0.0)
        if cone_weight > 0.0 and cone_bonus > 0.0:
            score += min(0.30, cone_weight * min(1.0, cone_bonus))
            reasons.append("cone")

        return score, ",".join(reasons) or "semantic"

    @staticmethod
    def _record_passes_acl(
        record: Dict[str, Any],
        tenant_id: str,
        user_id: str,
        project_id: str,
    ) -> bool:
        """Return True if record passes tenant/scope/project access control."""
        r_tenant = str(record.get("source_tenant_id", "") or "")
        if tenant_id and r_tenant and r_tenant != tenant_id:
            return False
        if record.get("scope") == "private" and record.get("source_user_id") != user_id:
            return False
        r_project = str(record.get("project_id", "") or "")
        if project_id and project_id != "public" and r_project not in (project_id, "public", ""):
            return False
        return True

    @staticmethod
    def _matched_record_anchors(
        *,
        record: Dict[str, Any],
        query_anchor_groups: Dict[str, set[str]],
    ) -> List[str]:
        """Return normalized query anchors that concretely matched this record."""
        if not query_anchor_groups:
            return []
        matched: List[str] = []
        record_groups = record_anchor_groups(record)
        for kind, query_values in query_anchor_groups.items():
            record_values = record_groups.get(kind, set())
            for value in sorted(query_values.intersection(record_values)):
                if value not in matched:
                    matched.append(value)
        return matched[:8]

    async def _records_to_matched_contexts(
        self,
        *,
        candidates: List[Dict[str, Any]],
        context_type: ContextType,
        detail_level: DetailLevel,
    ) -> List[MatchedContext]:
        """Convert raw store records into MatchedContext objects."""

        async def _build_one(record: Dict[str, Any]) -> MatchedContext:
            """Convert a single raw store record into a ``MatchedContext``.

            Reads L2 content from CortexFS when *detail_level* requires it.

            Args:
                record: Raw payload dict from the storage backend.

            Returns:
                A fully populated ``MatchedContext``.
            """
            uri = str(record.get("uri", ""))
            overview = None
            if detail_level in (DetailLevel.L1, DetailLevel.L2):
                overview = str(record.get("overview", "") or "") or None

            content = None
            if detail_level == DetailLevel.L2:
                content = str(record.get("content", "") or "") or None
                if content is None and self._fs:
                    try:
                        content = await self._fs.read_file(f"{uri}/content.md")
                    except Exception:
                        content = None

            effective_type = context_type
            if context_type == ContextType.ANY:
                try:
                    effective_type = ContextType(str(record.get("context_type", "memory")))
                except ValueError:
                    effective_type = ContextType.MEMORY

            return MatchedContext(
                uri=uri,
                context_type=effective_type,
                is_leaf=bool(record.get("is_leaf", False)),
                abstract=str(record.get("abstract", "") or ""),
                overview=overview,
                content=content,
                keywords=str(record.get("keywords", "") or ""),
                category=str(record.get("category", "") or ""),
                score=float(record.get("_final_score", record.get("_score", 0.0)) or 0.0),
                match_reason=str(record.get("_match_reason", "") or ""),
                session_id=str(record.get("session_id", "") or ""),
                source_doc_id=record.get("source_doc_id"),
                source_doc_title=record.get("source_doc_title"),
                source_section_path=record.get("source_section_path"),
                source_uri=(
                    dict(record.get("meta") or {}).get("source_uri")
                    if isinstance(record.get("meta"), dict)
                    else None
                ),
                msg_range=(
                    dict(record.get("meta") or {}).get("msg_range")
                    if isinstance(record.get("meta"), dict)
                    else None
                ),
                recomposition_stage=(
                    dict(record.get("meta") or {}).get("recomposition_stage")
                    if isinstance(record.get("meta"), dict)
                    else None
                ),
                layer=(
                    dict(record.get("meta") or {}).get("layer")
                    if isinstance(record.get("meta"), dict)
                    else None
                ),
                matched_anchors=list(record.get("_matched_anchors", []) or []),
                cone_used=bool(record.get("_cone_used", False)),
                path_source=record.get("_path_source") or None,
                path_cost=(
                    float(record["_path_cost"])
                    if record.get("_path_cost") is not None
                    else None
                ),
                path_breakdown=record.get("_path_breakdown") or None,
                relations=[],
            )

        return list(await asyncio.gather(*[_build_one(record) for record in candidates]))

    async def _execute_object_query(
        self,
        *,
        typed_query: TypedQuery,
        limit: int,
        score_threshold: Optional[float],
        search_filter: Optional[Dict[str, Any]],
        retrieve_plan: Optional[RetrievalPlan],
        probe_result: Optional[SearchResult],
        bound_plan: Optional[Dict[str, Any]] = None,
    ) -> QueryResult:
        """Execute one object-aware retrieval query with three-layer parallel search."""
        started = time.perf_counter()
        embed_started = started
        query_vector = await self._embed_retrieval_query(typed_query.query)
        embed_finished = time.perf_counter()

        kind_filter = None
        if retrieve_plan is not None and retrieve_plan.target_memory_kinds:
            kind_filter = {
                "op": "must",
                "field": "memory_kind",
                "conds": [kind.value for kind in retrieve_plan.target_memory_kinds],
            }

        start_point_filter = build_start_point_filter(
            retrieve_plan=retrieve_plan,
            probe_result=probe_result,
            bound_plan=bound_plan,
        )

        # Scope-aware filter (R3/R6).
        # ADV-002 fix: scope_only_filter (parent_uri / session_id / source_doc_id)
        # must apply to all three surface searches AND the missing_uris batch load,
        # because anchor_projection and fact_point records inherit these fields
        # from their source leaf (see _anchor_projection_records / _fact_point_records).
        # Without this, out-of-scope fact_points or anchors would project leaves back
        # into results, violating CONTAINER_SCOPED / SESSION_ONLY / DOCUMENT_ONLY.
        # is_leaf=True stays only on the leaf search (anchor/fp records are non-leaf).
        scope_only_filter: Optional[Dict[str, Any]] = None
        if retrieve_plan is not None:
            if retrieve_plan.scope_filter:
                scope_only_filter = retrieve_plan.scope_filter
            elif retrieve_plan.scope_level != ScopeLevel.GLOBAL:
                if probe_result and probe_result.starting_points:
                    if retrieve_plan.scope_level == ScopeLevel.CONTAINER_SCOPED:
                        parent_uris = [
                            sp.uri for sp in probe_result.starting_points if sp.uri
                        ]
                        if parent_uris:
                            scope_only_filter = {
                                "op": "must",
                                "field": "parent_uri",
                                "conds": parent_uris,
                            }
                    elif retrieve_plan.scope_level == ScopeLevel.SESSION_ONLY:
                        session_ids = sorted({
                            sp.session_id
                            for sp in probe_result.starting_points
                            if sp.session_id
                        })
                        if session_ids:
                            scope_only_filter = {
                                "op": "must",
                                "field": "session_id",
                                "conds": session_ids,
                            }
                    elif retrieve_plan.scope_level == ScopeLevel.DOCUMENT_ONLY:
                        doc_ids = sorted({
                            sp.source_doc_id
                            for sp in probe_result.starting_points
                            if sp.source_doc_id
                        })
                        if doc_ids:
                            scope_only_filter = {
                                "op": "must",
                                "field": "source_doc_id",
                                "conds": doc_ids,
                            }

        is_leaf_filter = {"op": "must", "field": "is_leaf", "conds": [True]}

        # Leaf filter: scope + kind + is_leaf + start_point
        leaf_filter_merged = merge_filter_clauses(
            search_filter,
            kind_filter,
            scope_only_filter,
            is_leaf_filter,
            start_point_filter,
        )
        # Anchor filter: scope + start_point + retrieval_surface=anchor_projection
        anchor_filter_merged = merge_filter_clauses(
            search_filter,
            start_point_filter,
            scope_only_filter,
            {"op": "must", "field": "retrieval_surface", "conds": ["anchor_projection"]},
        )
        # Fact-point filter: scope + start_point + retrieval_surface=fact_point
        fp_filter_merged = merge_filter_clauses(
            search_filter,
            start_point_filter,
            scope_only_filter,
            {"op": "must", "field": "retrieval_surface", "conds": ["fact_point"]},
        )

        query_anchor_groups = build_query_anchor_groups(retrieve_plan, probe_result)
        rerank_enabled = bool(query_anchor_groups) or bool(
            retrieve_plan is not None and retrieve_plan.search_profile.rerank
        )
        candidate_limit = int((bound_plan or {}).get("raw_candidate_cap") or 0)
        if candidate_limit <= 0:
            recall_budget = (
                retrieve_plan.search_profile.recall_budget
                if retrieve_plan is not None
                else 0.4
            )
            candidate_limit = max(limit, min(64, limit + max(4, int(round(recall_budget * 20)))))
            if rerank_enabled:
                candidate_limit = min(64, candidate_limit + 8)

        # Three-layer parallel search (R14, R20)
        leaf_limit = candidate_limit
        anchor_limit = min(64, candidate_limit * 2)
        fp_limit = min(96, candidate_limit * 3)

        _search_results = await asyncio.gather(
            self._storage.search(
                self._get_collection(),
                query_vector=query_vector,
                filter=leaf_filter_merged,
                limit=leaf_limit,
                text_query=typed_query.query,
            ),
            self._storage.search(
                self._get_collection(),
                query_vector=query_vector,
                filter=anchor_filter_merged,
                limit=anchor_limit,
                text_query=None,  # derived records: no lexical search
            ),
            self._storage.search(
                self._get_collection(),
                query_vector=query_vector,
                filter=fp_filter_merged,
                limit=fp_limit,
                text_query=None,  # derived records: no lexical search
            ),
            return_exceptions=True,
        )
        leaf_hits: List[Dict[str, Any]] = (
            _search_results[0] if not isinstance(_search_results[0], Exception) else []
        )
        anchor_hits: List[Dict[str, Any]] = (
            _search_results[1] if not isinstance(_search_results[1], Exception) else []
        )
        fp_hits: List[Dict[str, Any]] = (
            _search_results[2] if not isinstance(_search_results[2], Exception) else []
        )
        if isinstance(_search_results[0], Exception):
            logger.debug("[Orchestrator] leaf search failed: %s", _search_results[0])
        if isinstance(_search_results[1], Exception):
            logger.debug("[Orchestrator] anchor search failed: %s", _search_results[1])
        if isinstance(_search_results[2], Exception):
            logger.debug("[Orchestrator] fp search failed: %s", _search_results[2])

        search_finished = time.perf_counter()

        # URI projection: collect leaves referenced by anchor/fp hits (R21)
        def _get_target_uri(hit: Dict[str, Any]) -> str:
            """Extract the projection target URI from an anchor or fact-point hit."""
            return str(
                hit.get("projection_target_uri")
                or (hit.get("meta") or {}).get("projection_target_uri", "")
                or ""
            )

        known_leaf_uris = {str(r.get("uri", "")) for r in leaf_hits if r.get("uri")}
        projected_uris = {
            _get_target_uri(h)
            for h in anchor_hits + fp_hits
            if _get_target_uri(h)
        }
        missing_uris = [u for u in projected_uris if u and u not in known_leaf_uris]

        # Batch load missing projected leaves (R22)
        # ADV-002 fix: apply scope_only_filter + is_leaf to the batch load so an
        # out-of-scope fact_point / anchor cannot pull its leaf back in via URI projection.
        if missing_uris:
            try:
                tid, uid = get_effective_identity()
                project_id = get_effective_project_id()
                missing_filter = merge_filter_clauses(
                    search_filter,
                    scope_only_filter,
                    is_leaf_filter,
                    {"op": "must", "field": "uri", "conds": missing_uris},
                )
                loaded = await self._storage.search(
                    self._get_collection(),
                    query_vector=None,
                    filter=missing_filter,
                    limit=len(missing_uris) + 5,
                )
                for r in loaded:
                    if self._record_passes_acl(r, tid, uid, project_id):
                        leaf_hits.append(r)
                        known_leaf_uris.add(str(r.get("uri", "") or ""))
            except Exception as exc:
                logger.debug("[Orchestrator] batch URI load failed: %s", exc)

        # URI path scoring (R12, R15-R19)
        uri_path_costs = compute_uri_path_scores(leaf_hits, anchor_hits, fp_hits)

        # Determine path_source per leaf (for trace)
        from opencortex.retrieve.uri_path_scorer import (
            HIGH_CONFIDENCE_DISCOUNT as _HIGH_CONF_DISCOUNT,
        )
        from opencortex.retrieve.uri_path_scorer import (
            HIGH_CONFIDENCE_THRESHOLD as _HIGH_CONF_THRESHOLD,
        )
        from opencortex.retrieve.uri_path_scorer import (
            URI_HOP_COST as _URI_HOP_COST,
        )

        def _determine_path_source(leaf_uri: str) -> tuple[str, Optional[float]]:
            """Return (path_source, path_cost) for a leaf URI."""
            if leaf_uri not in uri_path_costs:
                return "direct", None
            cost = uri_path_costs[leaf_uri]
            # Check if best path comes from fp
            best_fp_cost = None
            for h in fp_hits:
                t = _get_target_uri(h)
                if t != leaf_uri:
                    continue
                s = max(0.0, min(1.0, float(h.get("_score", 0.0))))
                d = 1.0 - s
                hop = (
                    _URI_HOP_COST * _HIGH_CONF_DISCOUNT
                    if d < _HIGH_CONF_THRESHOLD
                    else _URI_HOP_COST
                )
                fp_c = d + hop
                if best_fp_cost is None or fp_c < best_fp_cost:
                    best_fp_cost = fp_c
            if best_fp_cost is not None and abs(best_fp_cost - cost) < 1e-9:
                return "fact_point", cost
            # Check anchor
            best_anchor_cost = None
            for h in anchor_hits:
                t = _get_target_uri(h)
                if t != leaf_uri:
                    continue
                s = max(0.0, min(1.0, float(h.get("_score", 0.0))))
                anchor_c = (1.0 - s) + _URI_HOP_COST
                if best_anchor_cost is None or anchor_c < best_anchor_cost:
                    best_anchor_cost = anchor_c
            if best_anchor_cost is not None and abs(best_anchor_cost - cost) < 1e-9:
                return "anchor", cost
            return "direct", cost

        rerank_started = search_finished
        frontier_waves = 0
        probe_candidate_ranks = build_probe_candidate_ranks(probe_result)

        # Cone expansion operates on leaf_hits (independent of URI scoring, R391)
        records, cone_used = await self._apply_cone_rerank(
            typed_query=typed_query,
            retrieve_plan=retrieve_plan,
            query_anchor_groups=query_anchor_groups,
            records=leaf_hits,
        )
        if cone_used:
            frontier_waves = 1

        cone_weight = 0.0
        if retrieve_plan is not None and cone_used:
            association_budget = retrieve_plan.search_profile.association_budget
            cone_weight = min(
                0.24,
                self._config.cone_weight * (0.6 + 0.8 * association_budget),
            )

        rescored: List[Dict[str, Any]] = []
        for record in records:
            leaf_uri = str(record.get("uri", "") or "")
            final_score, match_reason = self._score_object_record(
                record=record,
                typed_query=typed_query,
                retrieve_plan=retrieve_plan,
                query_anchor_groups=query_anchor_groups,
                probe_candidate_ranks=probe_candidate_ranks,
                cone_weight=cone_weight,
                uri_path_costs=uri_path_costs,
            )
            if score_threshold is not None and final_score < score_threshold:
                continue
            path_src, path_cst = _determine_path_source(leaf_uri)
            rescored_record = dict(record)
            rescored_record["_final_score"] = final_score
            rescored_record["_match_reason"] = match_reason
            rescored_record["_matched_anchors"] = self._matched_record_anchors(
                record=record,
                query_anchor_groups=query_anchor_groups,
            )
            rescored_record["_cone_used"] = bool(cone_used)
            rescored_record["_path_source"] = path_src
            rescored_record["_path_cost"] = path_cst
            rescored_record["_path_breakdown"] = (
                {"uri_path_cost": path_cst, "path_source": path_src}
                if path_cst is not None else None
            )
            rescored.append(rescored_record)
        rescored.sort(key=lambda record: record.get("_final_score", 0.0), reverse=True)
        rerank_finished = time.perf_counter()

        matched_contexts = await self._records_to_matched_contexts(
            candidates=rescored[:limit],
            context_type=typed_query.context_type,
            detail_level=typed_query.detail_level,
        )
        assembled = time.perf_counter()

        result = QueryResult(
            query=typed_query,
            matched_contexts=matched_contexts,
            searched_directories=list(typed_query.target_directories or []),
            timing_ms={
                "embed": round((embed_finished - embed_started) * 1000, 4),
                "search": round((search_finished - embed_finished) * 1000, 4),
                "rerank": round((rerank_finished - rerank_started) * 1000, 4),
                "assemble": round((assembled - rerank_finished) * 1000, 4),
                "total": round((assembled - started) * 1000, 4),
            },
        )
        result.explain = SearchExplain(
            query_class=typed_query.intent or "",
            path="object_recall",
            intent_ms=0.0,
            embed_ms=(embed_finished - embed_started) * 1000,
            search_ms=(search_finished - embed_finished) * 1000,
            rerank_ms=(rerank_finished - rerank_started) * 1000,
            assemble_ms=(assembled - rerank_finished) * 1000,
            doc_scope_hit=bool(typed_query.target_doc_id),
            time_filter_hit=False,
            candidates_before_rerank=len(records),
            candidates_after_rerank=len(matched_contexts),
            frontier_waves=frontier_waves,
            frontier_budget_exceeded=False,
            total_ms=(assembled - started) * 1000,
        )
        return result

    async def search(
        self,
        query: str,
        context_type: Optional[ContextType] = None,
        target_uri: str = "",
        limit: int = 5,
        score_threshold: Optional[float] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
        detail_level: str = "l1",
        probe_result: Optional[SearchResult] = None,
        retrieve_plan: Optional[RetrievalPlan] = None,
        meta: Optional[Dict[str, Any]] = None,
        session_context: Optional[Dict[str, Any]] = None,
    ) -> FindResult:
        """Delegate to ``MemoryService.search`` (plan 011)."""
        return await self._memory_service.search(
            query=query,
            context_type=context_type,
            target_uri=target_uri,
            limit=limit,
            score_threshold=score_threshold,
            metadata_filter=metadata_filter,
            detail_level=detail_level,
            probe_result=probe_result,
            retrieve_plan=retrieve_plan,
            meta=meta,
            session_context=session_context,
        )

    async def _resolve_memory_owner_ids(self, matches: List[Any]) -> List[str]:
        """Resolve memory owner ids from matched contexts using persisted record ids."""
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
                {"op": "must", "field": "uri", "conds": list(dict.fromkeys(uris))},
                limit=max(len(uris), 1) * 4,
            )
        except Exception as exc:
            logger.debug("[Orchestrator] Failed to resolve memory owner ids: %s", exc)
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

    def _schedule_recall_bookkeeping(
        self,
        *,
        memories: List[Any],
        query: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        """Delegate to BackgroundTaskManager._schedule_recall_bookkeeping."""
        self._background_task_manager._schedule_recall_bookkeeping(
            memories=memories,
            query=query,
            tenant_id=tenant_id,
            user_id=user_id,
        )

    def _recall_bookkeeping_tasks_set(self) -> set[asyncio.Task[Any]]:
        """Delegate to BackgroundTaskManager._recall_bookkeeping_tasks_set."""
        return self._background_task_manager._recall_bookkeeping_tasks_set()

    async def _get_record_by_uri(self, uri: str) -> Optional[Dict[str, Any]]:
        """Look up a single record by its URI.

        Args:
            uri: The record URI to search for.

        Returns:
            The matching record dict, or ``None`` if not found or on error.
        """
        if not uri or not self._storage:
            return None
        try:
            records = await self._storage.filter(
                self._get_collection(),
                {"op": "must", "field": "uri", "conds": [uri]},
                limit=1,
            )
        except Exception as exc:
            logger.debug("[Orchestrator] Failed to load record for uri=%s: %s", uri, exc)
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
        """Bootstrap autophagy state for a new owner (trace, session, etc.).

        Silently no-ops when autophagy is disabled or *owner_id* is empty.

        Args:
            owner_type: Category of the owner entity.
            owner_id: Unique identifier of the owner.
            tenant_id: Tenant scope.
            user_id: User scope.
            project_id: Project scope.
        """
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
                "[Orchestrator] Autophagy owner init failed type=%s owner=%s tenant=%s user=%s: %s",
                owner_type.value,
                owner_id,
                tenant_id,
                user_id,
                exc,
            )

    async def _on_trace_saved(self, trace: "Trace") -> None:
        """Callback invoked after a trace is persisted; bootstraps autophagy state.

        Args:
            trace: The persisted ``Trace`` object.
        """
        await self._initialize_autophagy_owner_state(
            owner_type=OwnerType.TRACE,
            owner_id=str(getattr(trace, "trace_id", "")),
            tenant_id=str(getattr(trace, "tenant_id", "")),
            user_id=str(getattr(trace, "user_id", "")),
            project_id=str(getattr(trace, "project_id", "")) or get_effective_project_id(),
        )

    async def _resolve_and_update_access_stats(self, uris: list) -> None:
        """1 filter + N parallel updates. Old: N filter + N get + N update (serial)."""
        if not uris:
            return
        try:
            recs = await self._storage.filter(
                self._get_collection(),
                {"op": "must", "field": "uri", "conds": uris},
                limit=len(uris),
            )
        except Exception:
            return
        if not recs:
            return
        await self._update_access_stats_batch(recs)

    async def _update_access_stats_batch(self, records: list) -> None:
        """Parallel batch update access_count + accessed_at (no individual get)."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        async def _one(r: dict) -> None:
            """Increment access counters for a single record."""
            rid = r.get("id", "")
            if not rid:
                return
            try:
                await self._storage.update(
                    self._get_collection(),
                    rid,
                    {"active_count": r.get("active_count", 0) + 1, "accessed_at": now},
                )
            except Exception as exc:
                logger.debug(
                    "[Orchestrator] Access stats update failed for %s: %s", rid, exc
                )

        await asyncio.gather(*[_one(r) for r in records], return_exceptions=True)

    async def session_search(
        self,
        query: str,
        messages: Optional[List[Message]] = None,
        session_summary: str = "",
        context_type: Optional[ContextType] = None,
        target_uri: str = "",
        limit: int = 5,
        score_threshold: Optional[float] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
        llm_completion: Optional[LLMCompletionCallable] = None,
    ) -> FindResult:
        """Session-aware search using IntentAnalyzer for query planning.

        Uses conversation history to generate multiple targeted queries,
        then executes them concurrently via hierarchical retrieval.

        Args:
            query: Current user message.
            messages: Recent conversation messages for context.
            session_summary: Compressed session summary.
            context_type: Restrict to a specific type.
            target_uri: Restrict to a directory subtree.
            limit: Maximum results per type.
            score_threshold: Minimum relevance score.
            metadata_filter: Additional filter conditions.
            llm_completion: Override LLM callable for this call.

        Returns:
            FindResult with query_plan attached.

        Raises:
            ValueError: If no LLM callable is configured.
        """
        self._ensure_init()

        completion_fn = llm_completion or self._llm_completion
        if not completion_fn:
            raise ValueError(
                "session_search requires an LLM callable. "
                "Provide one via constructor or llm_completion parameter."
            )

        analyzer = self._analyzer or IntentAnalyzer(llm_completion=completion_fn)

        # Read target abstract if applicable
        target_abstract = ""
        if target_uri:
            try:
                target_abstract = await self._fs.abstract(target_uri)
            except Exception:
                pass

        query_plan = await analyzer.analyze(
            compression_summary=session_summary,
            messages=messages or [],
            current_message=query,
            context_type=context_type,
            target_abstract=target_abstract,
            llm_completion=completion_fn,
        )

        # Set target directories on queries if specified
        if target_uri:
            for tq in query_plan.queries:
                tq.target_directories = [target_uri]

        search_filter = self._build_search_filter(metadata_filter=metadata_filter)

        query_results = await asyncio.gather(
            *[
                self._execute_object_query(
                    typed_query=tq,
                    limit=limit,
                    score_threshold=score_threshold,
                    search_filter=search_filter,
                    retrieve_plan=None,
                    probe_result=None,
                )
                for tq in query_plan.queries
            ]
        )

        result = self._aggregate_results(query_results, limit=limit)
        result.query_plan = query_plan
        result.query_results = list(query_results)
        return result

    # =========================================================================
    # Memory Listing
    # =========================================================================

    async def list_memories(
        self,
        category: Optional[str] = None,
        context_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        include_payload: bool = False,
    ) -> List[Dict[str, Any]]:
        """Delegate to ``MemoryService.list_memories`` (plan 011)."""
        return await self._memory_service.list_memories(
            category=category,
            context_type=context_type,
            limit=limit,
            offset=offset,
            include_payload=include_payload,
        )

    async def memory_index(
        self,
        context_type: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Delegate to ``MemoryService.memory_index`` (plan 011)."""
        return await self._memory_service.memory_index(
            context_type=context_type,
            limit=limit,
        )

    async def list_memories_admin(
        self,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        category: Optional[str] = None,
        context_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Delegate to ``MemoryService.list_memories_admin`` (plan 011)."""
        return await self._memory_service.list_memories_admin(
            tenant_id=tenant_id,
            user_id=user_id,
            category=category,
            context_type=context_type,
            limit=limit,
            offset=offset,
        )

    # =========================================================================
    # Reward-based Feedback Scoring (delegates to MemoryService, plan 011)
    # =========================================================================

    async def feedback(self, uri: str, reward: float) -> None:
        """Delegate to ``MemoryService.feedback`` (plan 011)."""
        return await self._memory_service.feedback(uri, reward)

    async def feedback_batch(self, rewards: List[Dict[str, Any]]) -> None:
        """Delegate to ``MemoryService.feedback_batch`` (plan 011)."""
        return await self._memory_service.feedback_batch(rewards)

    async def decay(self) -> Optional[Dict[str, Any]]:
        """Delegate to ``MemoryService.decay`` (plan 011)."""
        return await self._memory_service.decay()

    async def cleanup_expired_staging(self) -> int:
        """Delegate to ``MemoryService.cleanup_expired_staging`` (plan 011)."""
        return await self._memory_service.cleanup_expired_staging()

    async def protect(self, uri: str, protected: bool = True) -> None:
        """Delegate to ``MemoryService.protect`` (plan 011)."""
        return await self._memory_service.protect(uri, protected)

    async def get_profile(self, uri: str) -> Optional[Dict[str, Any]]:
        """Delegate to ``MemoryService.get_profile`` (plan 011)."""
        return await self._memory_service.get_profile(uri)

    # =========================================================================
    # System Status
    # =========================================================================

    async def system_status(self, status_type: str = "doctor") -> Dict[str, Any]:
        """Unified system status endpoint."""
        return await self._system_status_service.system_status(status_type)

    # =========================================================================
    # Session Management (Observer + Trace Pipeline)
    # =========================================================================

    async def session_begin(
        self,
        session_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Begin a new session.

        Starts Observer recording for the session.

        Args:
            session_id: Unique session identifier.
            meta: Optional metadata.

        Returns:
            Dict with session info.
        """
        self._ensure_init()
        tid, uid = get_effective_identity()
        if self._observer:
            self._observer.begin_session(
                session_id=self._observer_session_id(
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
        """Add a message to an active session.

        Records the message via Observer for later trace splitting.

        Args:
            session_id: Session identifier.
            role: Message role.
            content: Message content.
            meta: Optional metadata.

        Returns:
            Dict with message count.
        """
        self._ensure_init()
        tid, uid = get_effective_identity()
        message_count = 0
        if self._observer:
            observer_session_id = self._observer_session_id(
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
        """Public facade for benchmark-only offline conversation ingest.

        Delegates to :meth:`ContextManager.benchmark_ingest_conversation`.

        Defense-in-depth: by default this method requires the request
        role contextvar to be ``admin`` (REVIEW api-contract-007). The
        HTTP layer's ``_require_admin()`` already gates the only
        production caller, but the facade is now public on the
        orchestrator, so a future internal caller cannot accidentally
        bypass the policy by skipping the HTTP route. Direct in-process
        callers (benchmark CLI runs that pre-seed the role contextvar,
        unit tests, or maintenance scripts) can pass
        ``enforce_admin=False`` to opt out explicitly — the kwarg is
        deliberately verbose so the bypass shows up in code review.
        """
        self._ensure_init()
        if not self._context_manager:
            raise RuntimeError("ContextManager not initialized")
        if enforce_admin:
            from opencortex.http.request_context import is_admin

            if not is_admin():
                raise PermissionError(
                    "benchmark_conversation_ingest requires admin role "
                    "(set request role contextvar to 'admin' or pass "
                    "enforce_admin=False for trusted in-process callers)"
                )
        return await self._context_manager.benchmark_ingest_conversation(
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
        """End a session and trigger trace splitting.

        Flushes Observer transcript, splits into traces via TraceSplitter,
        and persists traces via TraceStore. May trigger Archivist for
        knowledge extraction.

        Args:
            session_id: Session to end.
            quality_score: Session quality (0-1).

        Returns:
            Dict with trace results.
        """
        self._ensure_init()

        alpha_traces_count = 0
        if self._observer:
            tid, uid = get_effective_identity()
            transcript = self._observer.flush(
                self._observer_session_id(
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

                    # Check Archivist trigger (background — non-blocking)
                    if self._archivist and self._trace_store:
                        count = await self._trace_store.count_new_traces(tid)
                        if self._archivist.should_trigger(count):
                            asyncio.create_task(self._knowledge_service.run_archivist(tid, uid))

                except Exception as exc:
                    logger.warning("[Alpha] Trace splitting failed: %s", exc)

            # Skill evaluator trigger — runs independently of trace splitting
            if self._skill_evaluator:
                asyncio.create_task(
                    self._skill_evaluator.evaluate_session(tid, uid, session_id)
                )

        return {
            "session_id": session_id,
            "quality_score": quality_score,
            "alpha_traces": alpha_traces_count,
        }

    async def _run_archivist(self, tenant_id: str, user_id: str) -> Dict[str, int]:
        """Delegate: run archivist via KnowledgeService."""
        return await self._knowledge_service.run_archivist(tenant_id, user_id)

    # =========================================================================
    # Cortex Alpha: Knowledge API
    # =========================================================================

    async def knowledge_search(
        self,
        query: str,
        types: Optional[List[str]] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """Delegate: search the Knowledge Store."""
        return await self._knowledge_service.knowledge_search(query, types, limit)

    async def knowledge_approve(self, knowledge_id: str) -> Dict[str, Any]:
        """Delegate: approve a knowledge candidate."""
        return await self._knowledge_service.knowledge_approve(knowledge_id)

    async def knowledge_reject(self, knowledge_id: str) -> Dict[str, Any]:
        """Delegate: reject a knowledge candidate."""
        return await self._knowledge_service.knowledge_reject(knowledge_id)

    async def knowledge_list_candidates(self) -> Dict[str, Any]:
        """Delegate: list knowledge candidates pending approval."""
        return await self._knowledge_service.knowledge_list_candidates()

    async def archivist_trigger(self) -> Dict[str, Any]:
        """Delegate: manually trigger the Archivist."""
        return await self._knowledge_service.archivist_trigger()

    async def archivist_status(self) -> Dict[str, Any]:
        """Delegate: get Archivist status."""
        return await self._knowledge_service.archivist_status()

    # =========================================================================
    # Batch Import
    # =========================================================================

    async def batch_add(
        self,
        items: List[Dict[str, Any]],
        source_path: str = "",
        scan_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Delegate to ``MemoryService.batch_add`` (plan 011)."""
        return await self._memory_service.batch_add(
            items, source_path=source_path, scan_meta=scan_meta,
        )

    async def promote_to_shared(
        self,
        uris: List[str],
        project_id: str,
    ) -> Dict[str, Any]:
        """Promote private resources to shared project scope.

        Rewrites URIs from user/{uid}/resources/... to resources/{project}/documents/...
        Updates Qdrant scope field and CortexFS paths.
        """
        self._ensure_init()
        tid, uid = get_effective_identity()
        promoted = 0
        errors = []

        for uri in uris:
            try:
                # 1. Get existing record
                results = await self._storage.filter(
                    self._get_collection(),
                    filter={"op": "must", "field": "uri", "conds": [uri]},
                    limit=1,
                )
                if not results:
                    errors.append({"uri": uri, "error": "not found"})
                    continue

                record = results[0]

                # 2. Build new shared URI
                # Extract node name from old URI (last path segment)
                parts = uri.rstrip("/").split("/")
                node_name = parts[-1] if parts else "unnamed"
                new_uri = CortexURI.build_shared(
                    tid, "resources", project_id, "documents", node_name
                )

                # 3. Update record fields
                record["uri"] = new_uri
                record["scope"] = "shared"
                record["project_id"] = project_id
                record["parent_uri"] = CortexURI.build_shared(
                    tid, "resources", project_id, "documents"
                )

                # 4. Upsert with new URI
                await self._storage.upsert(self._get_collection(), record)

                # 5. Delete old record if URI changed
                if new_uri != uri:
                    old_id = record.get("id", "")
                    if old_id:
                        try:
                            await self._storage.delete(self._get_collection(), [old_id])
                        except Exception:
                            pass  # best-effort cleanup

                promoted += 1
            except Exception as exc:
                errors.append({"uri": uri, "error": str(exc)})

        return {
            "status": "ok" if not errors else "partial",
            "promoted": promoted,
            "total": len(uris),
            "errors": errors,
        }

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def close(self) -> None:
        """Close storage and release resources."""
        await self._background_task_manager.close()

        context_manager = getattr(self, "_context_manager", None)
        if context_manager:
            await context_manager.close()

        # Plan 009 / R3 — release pooled httpx clients before storage
        # close. Order: llm_completion -> insights_llm_completion ->
        # rerank_client -> embedder -> storage. Each guarded with
        # try/except so one failed close cannot abort the rest of
        # teardown (matches the existing pattern above for
        # autophagy/recall task cancellation).
        for label, attr in (
            ("llm_completion", "_llm_completion"),
            ("insights_llm_completion", "_insights_llm_completion"),
        ):
            wrapper = getattr(self, attr, None)
            wrapper_aclose = (
                getattr(wrapper, "aclose", None) if wrapper else None
            )
            if wrapper_aclose is None:
                continue
            try:
                await wrapper_aclose()
            except Exception as exc:
                logger.warning(
                    "[MemoryOrchestrator] %s aclose failed: %s "
                    "(continuing teardown — pool socket may linger "
                    "until kernel reclaims)", label, exc,
                )
        rerank_client = getattr(self, "_rerank_client", None)
        if rerank_client is not None:
            try:
                await rerank_client.aclose()
            except Exception as exc:
                logger.warning(
                    "[MemoryOrchestrator] rerank_client aclose failed: %s "
                    "(continuing teardown)", exc,
                )

        immediate_fallback_embedder = getattr(
            self, "_immediate_fallback_embedder", None
        )
        if immediate_fallback_embedder:
            immediate_fallback_embedder.close()
        self._immediate_fallback_embedder = None
        self._immediate_fallback_embedder_attempted = False
        storage = getattr(self, "_storage", None)
        if storage:
            await storage.close()
        self._initialized = False
        logger.info("[MemoryOrchestrator] Closed")

    async def health_check(self) -> Dict[str, Any]:
        """Check health of all components."""
        return await self._system_status_service.health_check()

    async def stats(self) -> Dict[str, Any]:
        """Get orchestrator statistics."""
        return await self._system_status_service.stats()

    # =========================================================================
    # Internal helpers
    # =========================================================================

    # Valid user memory categories
    _USER_MEMORY_CATEGORIES = {"profile", "preferences", "entities", "events"}

    async def _generate_abstract_overview(self, content: str, file_path: str) -> tuple:
        """Use LLM to generate abstract (L0) and overview (L1) from content."""
        fallback_overview = smart_truncate(content, 500)

        if not self._llm_completion:
            return file_path, fallback_overview

        if len(content) > 3000:
            try:
                result = await chunked_llm_derive(
                    content=content,
                    prompt_builder=lambda chunk: build_doc_summarization_prompt(
                        file_path, chunk
                    ),
                    llm_fn=self._llm_completion,
                    parse_fn=parse_json_from_response,
                    merge_policy="abstract_overview",
                    max_chars_per_chunk=3000,
                )
                return result.get("abstract", file_path), result.get(
                    "overview", fallback_overview
                )
            except Exception:
                pass
            return file_path, fallback_overview

        prompt = build_doc_summarization_prompt(file_path, content)
        try:
            response = await self._llm_completion(prompt)
            data = parse_json_from_response(response)
            if isinstance(data, dict):
                return data.get("abstract", file_path), data.get(
                    "overview", fallback_overview
                )
        except Exception:
            pass

        return file_path, fallback_overview

    def _auto_uri(self, context_type: str, category: str, abstract: str = "") -> str:
        """Generate a URI based on context type, category, and abstract text.

        Uses semantic node names (deterministic) instead of random UUIDs
        when an abstract is provided. Falls back to uuid4 hex otherwise.

        Routing table:
          memory  + category  -> user/{uid}/memories/{category}/{nid}
          memory  + (empty)   -> user/{uid}/memories/events/{nid}
          case    + *         -> shared/cases/{nid}
          pattern + *         -> shared/patterns/{nid}
          skill   + section   -> shared/skills/{section}/{nid}
          skill   + (empty)   -> shared/skills/general/{nid}
          resource+ category  -> resources/{project}/{category}/{nid}
          staging + *         -> user/{uid}/staging/{nid}
        """
        from opencortex.utils.semantic_name import semantic_node_name

        tid, uid = get_effective_identity()
        node_name = semantic_node_name(abstract) if abstract else uuid4().hex

        if context_type == "memory":
            cat = category if category in self._USER_MEMORY_CATEGORIES else "events"
            return CortexURI.build_private(tid, uid, "memories", cat, node_name)

        if context_type == "case":
            return CortexURI.build_shared(tid, "shared", "cases", node_name)

        if context_type == "pattern":
            return CortexURI.build_shared(tid, "shared", "patterns", node_name)

        if context_type == "resource":
            project = get_effective_project_id()  # e.g. "OpenCortex" or "public"
            if category:
                return CortexURI.build_shared(
                    tid, "resources", project, category, node_name
                )
            return CortexURI.build_shared(tid, "resources", project, node_name)

        if context_type == "staging":
            return CortexURI.build_private(tid, uid, "staging", node_name)

        # Fallback: treat as user memory event
        return CortexURI.build_private(tid, uid, "memories", "events", node_name)

    async def _uri_exists(self, uri: str) -> bool:
        """Check if a URI already exists in the context collection."""
        try:
            results = await self._storage.filter(
                self._get_collection(),
                {"op": "must", "field": "uri", "conds": [uri]},
                limit=1,
            )
            return len(results) > 0
        except Exception:
            return False

    async def _resolve_unique_uri(self, uri: str, max_attempts: int = 100) -> str:
        """Ensure URI is unique, appending _1, _2, ... if needed."""
        if not await self._uri_exists(uri):
            return uri
        for i in range(1, max_attempts + 1):
            candidate = f"{uri}_{i}"
            if not await self._uri_exists(candidate):
                return candidate
        raise ValueError(
            f"URI conflict unresolved after {max_attempts} attempts: {uri}"
        )

    @staticmethod
    def _extract_category_from_uri(uri: str) -> str:
        """Extract category from URI path. E.g. /memories/preferences/abc -> preferences.

        For resources the path is resources/{project}/{category}/{nid},
        so the category is two segments after "resources".
        """
        parts = uri.split("/")
        # Look for known parent segments, return next part
        for parent in (
            "memories",
            "cases",
            "patterns",
            "skills",
            "staging",
            "resources",
        ):
            if parent in parts:
                idx = parts.index(parent)
                if parent in ("cases", "patterns"):
                    return parent
                if parent == "resources":
                    # resources/{project}/{category}/{nid} — skip project
                    cat_idx = idx + 2
                    if cat_idx < len(parts):
                        candidate = parts[cat_idx]
                        if len(candidate) != 12:
                            return candidate
                    continue
                if idx + 1 < len(parts):
                    candidate = parts[idx + 1]
                    # Skip node_id (12-char hex)
                    if len(candidate) != 12:
                        return candidate
        return ""

    @staticmethod
    def _enrich_abstract(abstract: str, content: str) -> str:
        """Append missing hard keywords when the abstract has poor term coverage."""
        if not content.strip():
            return abstract

        term_pattern = re.compile(
            r"[a-z]+[A-Z][a-zA-Z0-9]*|\b[A-Z]{2,}\b|[a-zA-Z0-9]+[_./-][a-zA-Z0-9]+"
        )
        candidates: list[str] = []
        seen: set[str] = set()
        for match in term_pattern.finditer(content):
            term = match.group(0)
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(term)

        if not candidates:
            return abstract

        abstract_lower = abstract.lower()
        covered = [term for term in candidates if term.lower() in abstract_lower]
        if len(covered) / len(candidates) >= 0.6:
            return abstract

        missing = [term for term in candidates if term.lower() not in abstract_lower][:10]
        if not missing:
            return abstract
        return f"{abstract} [{', '.join(missing)}]"

    def _derive_parent_uri(self, uri: str) -> str:
        """Derive parent URI by removing the last path segment."""
        try:
            parsed = CortexURI(uri)
            parent = parsed.parent
            return str(parent) if parent else ""
        except ValueError:
            return ""

    def _aggregate_results(
        self,
        query_results: List[QueryResult],
        *,
        limit: Optional[int] = None,
    ) -> FindResult:
        """Aggregate multiple QueryResults into a single FindResult (deduped by URI)."""
        ranked_contexts = []
        seen_uris: set = set()

        for result in query_results:
            for ctx in result.matched_contexts:
                if ctx.uri in seen_uris:
                    continue
                seen_uris.add(ctx.uri)
                ranked_contexts.append(ctx)

        ranked_contexts.sort(
            key=lambda ctx: float(getattr(ctx, "score", 0.0) or 0.0),
            reverse=True,
        )
        if limit is not None:
            ranked_contexts = ranked_contexts[: max(limit, 0)]

        memories, resources, skills = [], [], []
        for ctx in ranked_contexts:
            if ctx.context_type in (
                ContextType.MEMORY,
                ContextType.CASE,
                ContextType.PATTERN,
            ):
                memories.append(ctx)
            elif ctx.context_type == ContextType.RESOURCE:
                resources.append(ctx)
            elif ctx.context_type == ContextType.SKILL:
                skills.append(ctx)
            else:
                memories.append(ctx)

        return FindResult(
            memories=memories,
            resources=resources,
            skills=skills,
        )

    async def get_user_memory_stats(
        self,
        tenant_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """Get memory statistics for a user (admin/insights use).

        Returns:
            Dict with keys:
            - created_in_session: Dict[session_id, count]
            - total_memories: int
            - total_positive_feedback: int
            - total_negative_feedback: int
        """
        self._ensure_init()

        conds: List[Dict[str, Any]] = [
            {"op": "must_not", "field": "context_type", "conds": ["staging"]},
            {"op": "must", "field": "source_tenant_id", "conds": [tenant_id]},
            {"op": "must", "field": "source_user_id", "conds": [user_id]},
        ]
        filter_expr: Dict[str, Any] = {"op": "and", "conds": conds}

        memories = await self._storage.filter(
            self._get_collection(),
            filter_expr,
            limit=10000,
        )

        created_in_session: Dict[str, int] = {}
        total_positive = 0
        total_negative = 0

        for mem in memories:
            session_id = mem.get("session_id", "unknown")
            created_in_session[session_id] = created_in_session.get(session_id, 0) + 1
            total_positive += mem.get("positive_feedback_count", 0) or 0
            total_negative += mem.get("negative_feedback_count", 0) or 0

        return {
            "created_in_session": created_in_session,
            "total_memories": len(memories),
            "total_positive_feedback": total_positive,
            "total_negative_feedback": total_negative,
        }
