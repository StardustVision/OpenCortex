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
import logging
import math
import re
import time
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
from opencortex.models.embedder.base import EmbedderBase
from opencortex.prompts import (
    build_doc_summarization_prompt,
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
from opencortex.services.derivation_service import (
    _merge_unique_strings,
    _split_keyword_string,
)
from opencortex.utils.json_parse import parse_json_from_response
from opencortex.utils.text import chunked_llm_derive, smart_truncate
from opencortex.utils.uri import CortexURI

logger = logging.getLogger(__name__)

# Default collection name for all context types
_CONTEXT_COLLECTION = "context"

_IMMEDIATE_EMBED_TIMEOUT_SECONDS = 8.0
_IMMEDIATE_LOCAL_FALLBACK_MODEL = "BAAI/bge-m3"


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
        self._derivation_service_instance: Optional[Any] = None
        self._retrieval_service_instance: Optional[Any] = None
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
        self._derive_queue: asyncio.Queue[Optional[Any]] = asyncio.Queue()
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
        """Delegate to DerivationService._derive_parent_summary."""
        return await self._derivation_service._derive_parent_summary(
            doc_title, children_abstracts
        )

    async def _derive_layers(
        self,
        user_abstract: str,
        content: str,
        user_overview: str = "",
    ) -> Dict[str, Any]:
        """Delegate to DerivationService._derive_layers."""
        return await self._derivation_service._derive_layers(
            user_abstract=user_abstract,
            content=content,
            user_overview=user_overview,
        )

    @staticmethod
    def _coerce_derived_string(value: str) -> str:
        """Delegate to DerivationService._coerce_derived_string."""
        from opencortex.services.derivation_service import DerivationService

        return DerivationService._coerce_derived_string(value)

    @staticmethod
    def _coerce_derived_list(
        value: Any,
        *,
        limit: int,
        lowercase: bool = False,
    ) -> List[str]:
        """Delegate to DerivationService._coerce_derived_list."""
        from opencortex.services.derivation_service import DerivationService

        return DerivationService._coerce_derived_list(
            value, limit=limit, lowercase=lowercase
        )

    async def _derive_layers_split_fields(
        self,
        *,
        user_abstract: str,
        content: str,
        user_overview: str,
    ) -> Dict[str, Any]:
        """Delegate to DerivationService._derive_layers_split_fields."""
        return await self._derivation_service._derive_layers_split_fields(
            user_abstract=user_abstract,
            content=content,
            user_overview=user_overview,
        )

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
        """Delegate to DerivationService._complete_deferred_derive."""
        await self._derivation_service._complete_deferred_derive(
            uri=uri,
            content=content,
            abstract=abstract,
            overview=overview,
            session_id=session_id,
            meta=meta,
            context_type=context_type,
            raise_on_error=raise_on_error,
        )

    @staticmethod
    def _fallback_overview_from_content(
        *,
        user_overview: str,
        content: str,
    ) -> str:
        """Delegate to DerivationService._fallback_overview_from_content."""
        from opencortex.services.derivation_service import DerivationService

        return DerivationService._fallback_overview_from_content(
            user_overview=user_overview, content=content
        )

    @staticmethod
    def _is_retryable_layer_derivation_error(exc: Exception) -> bool:
        """Delegate to DerivationService._is_retryable_layer_derivation_error."""
        from opencortex.services.derivation_service import DerivationService

        return DerivationService._is_retryable_layer_derivation_error(exc)

    async def _derive_layers_llm_completion(self, prompt: str) -> str:
        """Delegate to DerivationService._derive_layers_llm_completion."""
        return await self._derivation_service._derive_layers_llm_completion(prompt)

    @staticmethod
    def _derive_abstract_from_overview(
        *,
        user_abstract: str,
        overview: str,
        content: str,
    ) -> str:
        """Delegate to DerivationService._derive_abstract_from_overview."""
        from opencortex.services.derivation_service import DerivationService

        return DerivationService._derive_abstract_from_overview(
            user_abstract=user_abstract, overview=overview, content=content
        )

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
        """Delegate to DerivationService._build_abstract_json."""
        return self._derivation_service._build_abstract_json(
            uri=uri,
            context_type=context_type,
            category=category,
            abstract=abstract,
            overview=overview,
            content=content,
            entities=entities,
            meta=meta,
            keywords=keywords,
            parent_uri=parent_uri,
            session_id=session_id,
        )

    @staticmethod
    def _memory_object_payload(
        abstract_json: Dict[str, Any],
        *,
        is_leaf: bool,
    ) -> Dict[str, Any]:
        """Delegate to DerivationService._memory_object_payload."""
        from opencortex.services.derivation_service import DerivationService

        return DerivationService._memory_object_payload(
            abstract_json, is_leaf=is_leaf
        )

    @staticmethod
    def _anchor_projection_prefix(uri: str) -> str:
        """Delegate to DerivationService._anchor_projection_prefix."""
        from opencortex.services.derivation_service import DerivationService

        return DerivationService._anchor_projection_prefix(uri)

    @staticmethod
    def _fact_point_prefix(uri: str) -> str:
        """Delegate to DerivationService._fact_point_prefix."""
        from opencortex.services.derivation_service import DerivationService

        return DerivationService._fact_point_prefix(uri)

    @staticmethod
    def _is_valid_fact_point(text: str) -> bool:
        """Delegate to DerivationService._is_valid_fact_point."""
        from opencortex.services.derivation_service import DerivationService

        return DerivationService._is_valid_fact_point(text)

    def _fact_point_records(
        self,
        *,
        source_record: Dict[str, Any],
        fact_points_list: List[str],
    ) -> List[Dict[str, Any]]:
        """Delegate to DerivationService._fact_point_records."""
        return self._derivation_service._fact_point_records(
            source_record=source_record, fact_points_list=fact_points_list
        )

    def _anchor_projection_records(
        self,
        *,
        source_record: Dict[str, Any],
        abstract_json: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Delegate to DerivationService._anchor_projection_records."""
        return self._derivation_service._anchor_projection_records(
            source_record=source_record, abstract_json=abstract_json
        )

    async def _delete_derived_stale(
        self,
        collection: str,
        prefix: str,
        keep_uris: set,
    ) -> None:
        """Delegate to DerivationService._delete_derived_stale."""
        await self._derivation_service._delete_derived_stale(
            collection, prefix, keep_uris
        )

    async def _sync_anchor_projection_records(
        self,
        *,
        source_record: Dict[str, Any],
        abstract_json: Dict[str, Any],
    ) -> None:
        """Delegate to DerivationService._sync_anchor_projection_records."""
        await self._derivation_service._sync_anchor_projection_records(
            source_record=source_record, abstract_json=abstract_json
        )

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
    def _derivation_service(self) -> "DerivationService":
        """Lazy-built DerivationService for derive-domain methods."""
        from opencortex.services.derivation_service import DerivationService

        cached = getattr(self, "_derivation_service_instance", None)
        if cached is None:
            cached = DerivationService(self)
            self._derivation_service_instance = cached
        return cached

    @property
    def _retrieval_service(self) -> "RetrievalService":
        """Lazy-built RetrievalService for search/retrieve-domain methods."""
        from opencortex.services.retrieval_service import RetrievalService

        cached = getattr(self, "_retrieval_service_instance", None)
        if cached is None:
            cached = RetrievalService(self)
            self._retrieval_service_instance = cached
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
        """Delegate to RetrievalService.probe_memory."""
        return await self._retrieval_service.probe_memory(
            query,
            context_type=context_type,
            target_uri=target_uri,
            target_doc_id=target_doc_id,
            session_context=session_context,
            metadata_filter=metadata_filter,
        )

    def memory_probe_mode(self) -> str:
        """Delegate to RetrievalService.memory_probe_mode."""
        return self._retrieval_service.memory_probe_mode()

    def memory_probe_trace(self) -> Dict[str, Any]:
        """Delegate to RetrievalService.memory_probe_trace."""
        return self._retrieval_service.memory_probe_trace()

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
        """Delegate to RetrievalService.plan_memory."""
        return self._retrieval_service.plan_memory(
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
        """Delegate to RetrievalService.bind_memory_runtime."""
        return self._retrieval_service.bind_memory_runtime(
            probe_result=probe_result,
            retrieve_plan=retrieve_plan,
            max_items=max_items,
            session_context=session_context,
            include_knowledge=include_knowledge,
        )

    def _build_search_filter(
        self,
        *,
        category_filter: Optional[List[str]] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Delegate to RetrievalService._build_search_filter."""
        return self._retrieval_service._build_search_filter(
            category_filter=category_filter,
            metadata_filter=metadata_filter,
        )

    def _build_probe_filter(self) -> Dict[str, Any]:
        """Delegate to RetrievalService._build_probe_filter."""
        return self._retrieval_service._build_probe_filter()

    def _cone_query_entities(
        self,
        *,
        typed_query: TypedQuery,
        query_anchor_groups: Dict[str, set[str]],
        records: List[Dict[str, Any]],
    ) -> set[str]:
        """Delegate to RetrievalService._cone_query_entities."""
        return self._retrieval_service._cone_query_entities(
            typed_query=typed_query,
            query_anchor_groups=query_anchor_groups,
            records=records,
        )

    async def _apply_cone_rerank(
        self,
        *,
        typed_query: TypedQuery,
        retrieve_plan: Optional[RetrievalPlan],
        query_anchor_groups: Dict[str, set[str]],
        records: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], bool]:
        """Delegate to RetrievalService._apply_cone_rerank."""
        return await self._retrieval_service._apply_cone_rerank(
            typed_query=typed_query,
            retrieve_plan=retrieve_plan,
            query_anchor_groups=query_anchor_groups,
            records=records,
        )

    async def _embed_retrieval_query(self, query_text: str) -> Optional[List[float]]:
        """Delegate to RetrievalService._embed_retrieval_query."""
        return await self._retrieval_service._embed_retrieval_query(query_text)

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
        """Delegate to RetrievalService._score_object_record."""
        return self._retrieval_service._score_object_record(
            record=record,
            typed_query=typed_query,
            retrieve_plan=retrieve_plan,
            query_anchor_groups=query_anchor_groups,
            probe_candidate_ranks=probe_candidate_ranks,
            cone_weight=cone_weight,
            uri_path_costs=uri_path_costs,
        )

    @staticmethod
    def _record_passes_acl(
        record: Dict[str, Any],
        tenant_id: str,
        user_id: str,
        project_id: str,
    ) -> bool:
        """Delegate to RetrievalService._record_passes_acl."""
        from opencortex.services.retrieval_service import RetrievalService

        return RetrievalService._record_passes_acl(
            record, tenant_id, user_id, project_id
        )

    @staticmethod
    def _matched_record_anchors(
        *,
        record: Dict[str, Any],
        query_anchor_groups: Dict[str, set[str]],
    ) -> List[str]:
        """Delegate to RetrievalService._matched_record_anchors."""
        from opencortex.services.retrieval_service import RetrievalService

        return RetrievalService._matched_record_anchors(
            record=record,
            query_anchor_groups=query_anchor_groups,
        )

    async def _records_to_matched_contexts(
        self,
        *,
        candidates: List[Dict[str, Any]],
        context_type: ContextType,
        detail_level: DetailLevel,
    ) -> List[MatchedContext]:
        """Delegate to RetrievalService._records_to_matched_contexts."""
        return await self._retrieval_service._records_to_matched_contexts(
            candidates=candidates,
            context_type=context_type,
            detail_level=detail_level,
        )

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
        """Delegate to RetrievalService._execute_object_query."""
        return await self._retrieval_service._execute_object_query(
            typed_query=typed_query,
            limit=limit,
            score_threshold=score_threshold,
            search_filter=search_filter,
            retrieve_plan=retrieve_plan,
            probe_result=probe_result,
            bound_plan=bound_plan,
        )

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
        """Look up a single record by its URI."""
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
        """Bootstrap autophagy state for a new owner when autophagy is enabled."""
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
                "[Orchestrator] Autophagy owner init failed type=%s owner=%s "
                "tenant=%s user=%s: %s",
                owner_type.value,
                owner_id,
                tenant_id,
                user_id,
                exc,
            )

    async def _on_trace_saved(self, trace: "Trace") -> None:
        """Callback invoked after a trace is persisted."""
        await self._initialize_autophagy_owner_state(
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
                {"op": "must", "field": "uri", "conds": uris},
                limit=len(uris),
            )
        except Exception:
            return
        if not recs:
            return
        await self._update_access_stats_batch(recs)

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
                    "[Orchestrator] Access stats update failed for %s: %s",
                    record_id,
                    exc,
                )

        await asyncio.gather(*[_one(record) for record in records], return_exceptions=True)

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
        """Delegate to RetrievalService.session_search."""
        return await self._retrieval_service.session_search(
            query=query,
            messages=messages,
            session_summary=session_summary,
            context_type=context_type,
            target_uri=target_uri,
            limit=limit,
            score_threshold=score_threshold,
            metadata_filter=metadata_filter,
            llm_completion=llm_completion,
        )

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
        """Delegate to RetrievalService._aggregate_results."""
        return self._retrieval_service._aggregate_results(
            query_results,
            limit=limit,
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
