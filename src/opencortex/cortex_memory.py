# SPDX-License-Identifier: Apache-2.0
"""CortexMemory for OpenCortex.

The CortexMemory facade is the primary user-facing API that wires together all
internal components:

- CortexConfig: tenant/user isolation
- CortexFS: three-layer (L0/L1/L2) filesystem abstraction
- StorageInterface: vector storage (Qdrant-backed)
- Object-aware retrieval executor over canonical memory records
- EmbedderBase: pluggable embedding

Typical usage::

    from opencortex.cortex_memory import CortexMemory

    memory = CortexMemory(embedder=my_embedder)
    await memory.init()

    # Add a memory
    await memory.add(
        abstract="User prefers dark theme in all editors",
        category="preferences",
    )

    # Search
    results = await memory.search("What theme does the user prefer?")

    # Feedback (reinforcement)
    await memory.feedback(uri=results.memories[0].uri, reward=1.0)
"""

import asyncio
import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
)

from opencortex.cognition.state_types import OwnerType
from opencortex.config import CortexConfig, get_config
from opencortex.core.context import Context
from opencortex.core.user_id import UserIdentifier
from opencortex.http.request_context import (
    get_effective_identity,
)
from opencortex.intent import (
    MemoryExecutor,
    RecallPlanner,
    RetrievalPlan,
    SearchResult,
)
from opencortex.models.embedder.base import EmbedderBase
from opencortex.retrieve.rerank_client import RerankClient
from opencortex.retrieve.rerank_config import RerankConfig
from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
    FindResult,
    MatchedContext,
    QueryResult,
    TypedQuery,
)
from opencortex.services.memory_signals import MemorySignalBus
from opencortex.storage.cortex_fs import CortexFS
from opencortex.storage.storage_interface import StorageInterface

if TYPE_CHECKING:
    from opencortex.alpha.types import Trace
    from opencortex.lifecycle.background_tasks import BackgroundTaskManager
    from opencortex.lifecycle.bootstrapper import SubsystemBootstrapper
    from opencortex.services.derivation_service import DerivationService
    from opencortex.services.knowledge_service import KnowledgeService
    from opencortex.services.memory_admin_stats_service import (
        MemoryAdminStatsService,
    )
    from opencortex.services.memory_record_service import MemoryRecordService
    from opencortex.services.memory_service import MemoryService
    from opencortex.services.memory_sharing_service import MemorySharingService
    from opencortex.services.model_runtime_service import ModelRuntimeService
    from opencortex.services.cortex_memory_services import CortexMemoryServices
    from opencortex.services.retrieval_service import RetrievalService
    from opencortex.services.session_lifecycle_service import (
        SessionLifecycleService,
    )
    from opencortex.services.system_status_service import SystemStatusService

logger = logging.getLogger(__name__)

# Default collection name for all context types
_CONTEXT_COLLECTION = "context"
LLMCompletionCallable = Callable[[str], Awaitable[str]]


class CortexMemory:
    """Top-level memory facade for OpenCortex operations.

    Wires together storage, filesystem, retrieval, embedding, and
    reward-based feedback scoring into a single coherent API.

    Args:
        config: CortexConfig instance. Uses global config if not provided.
        storage: StorageInterface backend. Must be provided (Qdrant-backed).
        embedder: Embedding model. Required for add/search operations.
        rerank_config: Rerank configuration for retrieval scoring.
        llm_completion: Async callable for LLM-backed optional services.
    """

    def __init__(
        self,
        config: Optional[CortexConfig] = None,
        storage: Optional[StorageInterface] = None,
        embedder: Optional[EmbedderBase] = None,
        rerank_config: Optional[RerankConfig] = None,
        llm_completion: Optional[LLMCompletionCallable] = None,
    ) -> None:
        self._config = config or get_config()
        self._storage = storage
        self._embedder = embedder
        self._rerank_config = rerank_config or RerankConfig()
        self._llm_completion = llm_completion
        # Plan 009 / RR-01 (PERF-02 follow-up) — process-lifetime
        # RerankClient singleton owned by the memory facade. Pre-fix
        # ``admin_search_debug`` constructed a new RerankClient per
        # request and never closed it, leaking one TCP connection per
        # admin call. Lifted here so ``CortexMemory.close()`` can
        # call ``aclose()`` exactly once on shutdown.
        self._rerank_client: Optional["RerankClient"] = None
        self._services_instance: Optional[Any] = None
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
        self._memory_signal_bus = MemorySignalBus()

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
    # Bootstrap and model runtime facade
    # =========================================================================

    async def init(self) -> "CortexMemory":
        """Delegate to SubsystemBootstrapper.init().

        Initializes all internal components via the bootstrapper.
        See ``SubsystemBootstrapper.init`` for the full 11-step
        boot sequence.

        Returns:
            self (for chaining)
        """
        return await self._bootstrapper.init()

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
        return self._model_runtime_service._is_retryable_immediate_embed_exception(exc)

    def _create_immediate_fallback_embedder(self) -> Optional[EmbedderBase]:
        """Create a local fallback embedder for immediate-write remote failures."""
        return self._model_runtime_service._create_immediate_fallback_embedder()

    def _get_immediate_fallback_embedder(self) -> Optional[EmbedderBase]:
        """Return cached immediate local fallback embedder if available."""
        return self._model_runtime_service._get_immediate_fallback_embedder()

    def _wrap_with_hybrid(self, embedder: EmbedderBase) -> EmbedderBase:
        """Wrap dense embedder with BM25 sparse for hybrid search.

        No-op if embedder is already hybrid.
        """
        return self._model_runtime_service._wrap_with_hybrid(embedder)

    def _wrap_with_cache(self, embedder: EmbedderBase) -> EmbedderBase:
        """Wrap an embedder with LRU cache."""
        return self._model_runtime_service._wrap_with_cache(embedder)

    def _get_or_create_rerank_client(self) -> RerankClient:
        """Return the process-lifetime RerankClient singleton (lazy).

        Plan 009: Constructed on first access so the memory facade's
        normal init path stays cheap (eager construction would fire
        ``_init_local_reranker`` -> fastembed model download in every
        test). Once built, the same instance serves every caller for
        the process lifetime — closes the per-request leak that
        triggered the original CLOSE_WAIT incident.
        """
        return self._model_runtime_service._get_or_create_rerank_client()

    def _build_rerank_config(self) -> RerankConfig:
        """Build RerankConfig from explicit and configured rerank fields.

        Priority: explicit rerank_config > CortexConfig rerank_* fields > defaults.
        """
        return self._model_runtime_service._build_rerank_config()

    # =========================================================================
    # Background task and system facade
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

    async def _write_immediate(
        self,
        session_id: str,
        msg_index: int,
        text: str,
        tool_calls: Optional[list] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Delegate to SessionLifecycleService._write_immediate."""
        return await self._session_lifecycle_service._write_immediate(
            session_id=session_id,
            msg_index=msg_index,
            text=text,
            tool_calls=tool_calls,
            meta=meta,
        )

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
        """Delegate to MemoryRecordService._build_abstract_json."""
        return self._memory_record_service._build_abstract_json(
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
        """Delegate to MemoryRecordService._memory_object_payload."""
        from opencortex.services.memory_record_service import MemoryRecordService

        return MemoryRecordService._memory_object_payload(
            abstract_json, is_leaf=is_leaf
        )

    @staticmethod
    def _anchor_projection_prefix(uri: str) -> str:
        """Delegate to MemoryRecordService._anchor_projection_prefix."""
        from opencortex.services.memory_record_service import MemoryRecordService

        return MemoryRecordService._anchor_projection_prefix(uri)

    @staticmethod
    def _fact_point_prefix(uri: str) -> str:
        """Delegate to MemoryRecordService._fact_point_prefix."""
        from opencortex.services.memory_record_service import MemoryRecordService

        return MemoryRecordService._fact_point_prefix(uri)

    @staticmethod
    def _is_valid_fact_point(text: str) -> bool:
        """Delegate to MemoryRecordService._is_valid_fact_point."""
        from opencortex.services.memory_record_service import MemoryRecordService

        return MemoryRecordService._is_valid_fact_point(text)

    def _fact_point_records(
        self,
        *,
        source_record: Dict[str, Any],
        fact_points_list: List[str],
    ) -> List[Dict[str, Any]]:
        """Delegate to MemoryRecordService._fact_point_records."""
        return self._memory_record_service._fact_point_records(
            source_record=source_record, fact_points_list=fact_points_list
        )

    def _anchor_projection_records(
        self,
        *,
        source_record: Dict[str, Any],
        abstract_json: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Delegate to MemoryRecordService._anchor_projection_records."""
        return self._memory_record_service._anchor_projection_records(
            source_record=source_record, abstract_json=abstract_json
        )

    async def _delete_derived_stale(
        self,
        collection: str,
        prefix: str,
        keep_uris: set,
    ) -> None:
        """Delegate to MemoryRecordService._delete_derived_stale."""
        await self._memory_record_service._delete_derived_stale(
            collection, prefix, keep_uris
        )

    async def _sync_anchor_projection_records(
        self,
        *,
        source_record: Dict[str, Any],
        abstract_json: Dict[str, Any],
    ) -> None:
        """Delegate to MemoryRecordService._sync_anchor_projection_records."""
        await self._memory_record_service._sync_anchor_projection_records(
            source_record=source_record, abstract_json=abstract_json
        )

    def _ensure_init(self) -> None:
        """Raise if not initialized."""
        if not self._initialized:
            raise RuntimeError(
                "CortexMemory not initialized. Call `await orch.init()` first."
            )

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def config(self) -> CortexConfig:
        """Active CortexConfig for this memory facade."""
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
    def _services(self) -> "CortexMemoryServices":
        """Lazy service registry for memory-facade-owned collaborators."""
        from opencortex.services.cortex_memory_services import (
            CortexMemoryServices,
        )

        cached = getattr(self, "_services_instance", None)
        if cached is None:
            cached = CortexMemoryServices(self)
            self._services_instance = cached
        return cached

    @property
    def _memory_service(self) -> "MemoryService":
        """Lazy-built MemoryService for delegated CRUD/query/scoring methods."""
        return self._services.memory_service

    @property
    def _derivation_service(self) -> "DerivationService":
        """Lazy-built DerivationService for derive-domain methods."""
        return self._services.derivation_service

    @property
    def _retrieval_service(self) -> "RetrievalService":
        """Lazy-built RetrievalService for search/retrieve-domain methods."""
        return self._services.retrieval_service

    @property
    def _session_lifecycle_service(self) -> "SessionLifecycleService":
        """Lazy-built SessionLifecycleService for session/trace lifecycle methods."""
        return self._services.session_lifecycle_service

    @property
    def _memory_record_service(self) -> "MemoryRecordService":
        """Lazy-built MemoryRecordService for record/projection/URI helpers."""
        return self._services.memory_record_service

    @property
    def _model_runtime_service(self) -> "ModelRuntimeService":
        """Lazy-built ModelRuntimeService for embedder/rerank runtime helpers."""
        return self._services.model_runtime_service

    @property
    def _memory_sharing_service(self) -> "MemorySharingService":
        """Lazy-built MemorySharingService for sharing/admin mutations."""
        return self._services.memory_sharing_service

    @property
    def _memory_admin_stats_service(self) -> "MemoryAdminStatsService":
        """Lazy-built MemoryAdminStatsService for admin memory statistics."""
        return self._services.memory_admin_stats_service

    @property
    def _knowledge_service(self) -> "KnowledgeService":
        """Lazy-built KnowledgeService for delegated knowledge methods."""
        return self._services.knowledge_service

    @property
    def _system_status_service(self) -> "SystemStatusService":
        """Lazy-built SystemStatusService for delegated status methods."""
        return self._services.system_status_service

    @property
    def _background_task_manager(self) -> "BackgroundTaskManager":
        """Lazy-built BackgroundTaskManager for delegated lifecycle methods."""
        return self._services.background_task_manager

    @property
    def _bootstrapper(self) -> "SubsystemBootstrapper":
        """Lazy-built SubsystemBootstrapper for subsystem creation and wiring."""
        return self._services.bootstrapper

    # =========================================================================
    # Memory write/mutation facade
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
        """Delegate to MemoryRecordService._ttl_from_hours."""
        return self._memory_record_service._ttl_from_hours(hours)

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
        return await self._memory_service._merge_into(
            existing_uri, new_abstract, new_content
        )

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
        """Return the bounded Phase 1 probe filter."""
        return self._retrieval_service._build_search_filter()

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
        """Delegate to SessionLifecycleService._resolve_memory_owner_ids."""
        return await self._session_lifecycle_service._resolve_memory_owner_ids(matches)

    async def _get_record_by_uri(self, uri: str) -> Optional[Dict[str, Any]]:
        """Delegate to SessionLifecycleService._get_record_by_uri."""
        return await self._session_lifecycle_service._get_record_by_uri(uri)

    async def _initialize_autophagy_owner_state(
        self,
        *,
        owner_type: OwnerType,
        owner_id: str,
        tenant_id: str,
        user_id: str,
        project_id: str,
    ) -> None:
        """Delegate to SessionLifecycleService._initialize_autophagy_owner_state."""
        await self._session_lifecycle_service._initialize_autophagy_owner_state(
            owner_type=owner_type,
            owner_id=owner_id,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
        )

    async def _on_trace_saved(self, trace: "Trace") -> None:
        """Delegate to SessionLifecycleService._on_trace_saved."""
        await self._session_lifecycle_service._on_trace_saved(trace)

    async def _resolve_and_update_access_stats(self, uris: list) -> None:
        """Delegate to SessionLifecycleService._resolve_and_update_access_stats."""
        await self._session_lifecycle_service._resolve_and_update_access_stats(uris)

    async def _update_access_stats_batch(self, records: list) -> None:
        """Delegate to SessionLifecycleService._update_access_stats_batch."""
        await self._session_lifecycle_service._update_access_stats_batch(records)

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
        """Delegate to SessionLifecycleService.session_begin."""
        return await self._session_lifecycle_service.session_begin(
            session_id=session_id,
            meta=meta,
        )

    async def session_message(
        self,
        session_id: str,
        role: str,
        content: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Delegate to SessionLifecycleService.session_message."""
        return await self._session_lifecycle_service.session_message(
            session_id=session_id,
            role=role,
            content=content,
            meta=meta,
        )

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
        """Delegate to SessionLifecycleService.benchmark_conversation_ingest."""
        return await self._session_lifecycle_service.benchmark_conversation_ingest(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            segments=segments,
            include_session_summary=include_session_summary,
            ingest_shape=ingest_shape,
            enforce_admin=enforce_admin,
        )

    async def session_end(
        self,
        session_id: str,
        quality_score: float = 0.5,
    ) -> Dict[str, Any]:
        """Delegate to SessionLifecycleService.session_end."""
        return await self._session_lifecycle_service.session_end(
            session_id=session_id,
            quality_score=quality_score,
        )

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
    # Batch import and sharing/admin facade
    # =========================================================================

    async def batch_add(
        self,
        items: List[Dict[str, Any]],
        source_path: str = "",
        scan_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Delegate to ``MemoryService.batch_add`` (plan 011)."""
        return await self._memory_service.batch_add(
            items,
            source_path=source_path,
            scan_meta=scan_meta,
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
        return await self._memory_sharing_service.promote_to_shared(
            uris=uris,
            project_id=project_id,
        )

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
        # autophagy/signal task cancellation).
        for label, attr in (
            ("llm_completion", "_llm_completion"),
            ("insights_llm_completion", "_insights_llm_completion"),
        ):
            wrapper = getattr(self, attr, None)
            wrapper_aclose = getattr(wrapper, "aclose", None) if wrapper else None
            if wrapper_aclose is None:
                continue
            try:
                await wrapper_aclose()
            except Exception as exc:
                logger.warning(
                    "[CortexMemory] %s aclose failed: %s "
                    "(continuing teardown — pool socket may linger "
                    "until kernel reclaims)",
                    label,
                    exc,
                )
        rerank_client = getattr(self, "_rerank_client", None)
        if rerank_client is not None:
            try:
                await rerank_client.aclose()
            except Exception as exc:
                logger.warning(
                    "[CortexMemory] rerank_client aclose failed: %s "
                    "(continuing teardown)",
                    exc,
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
        logger.info("[CortexMemory] Closed")

    async def health_check(self) -> Dict[str, Any]:
        """Check health of all components."""
        return await self._system_status_service.health_check()

    async def stats(self) -> Dict[str, Any]:
        """Get memory facade statistics."""
        return await self._system_status_service.stats()

    # =========================================================================
    # Internal helpers
    # =========================================================================

    # Valid user memory categories
    _USER_MEMORY_CATEGORIES = {"profile", "preferences", "entities", "events"}

    async def _generate_abstract_overview(self, content: str, file_path: str) -> tuple:
        """Delegate to MemoryService._generate_abstract_overview."""
        return await self._memory_service._generate_abstract_overview(
            content,
            file_path,
        )

    def _auto_uri(self, context_type: str, category: str, abstract: str = "") -> str:
        """Delegate to MemoryRecordService._auto_uri."""
        return self._memory_record_service._auto_uri(
            context_type=context_type,
            category=category,
            abstract=abstract,
        )

    async def _uri_exists(self, uri: str) -> bool:
        """Delegate to MemoryRecordService._uri_exists."""
        return await self._memory_record_service._uri_exists(uri)

    async def _resolve_unique_uri(self, uri: str, max_attempts: int = 100) -> str:
        """Delegate to MemoryRecordService._resolve_unique_uri."""
        return await self._memory_record_service._resolve_unique_uri(
            uri,
            max_attempts=max_attempts,
        )

    @staticmethod
    def _extract_category_from_uri(uri: str) -> str:
        """Delegate to MemoryRecordService._extract_category_from_uri."""
        from opencortex.services.memory_record_service import MemoryRecordService

        return MemoryRecordService._extract_category_from_uri(uri)

    @staticmethod
    def _enrich_abstract(abstract: str, content: str) -> str:
        """Delegate to MemoryRecordService._enrich_abstract."""
        from opencortex.services.memory_record_service import MemoryRecordService

        return MemoryRecordService._enrich_abstract(abstract, content)

    def _derive_parent_uri(self, uri: str) -> str:
        """Delegate to MemoryRecordService._derive_parent_uri."""
        from opencortex.services.memory_record_service import MemoryRecordService

        return MemoryRecordService._derive_parent_uri(uri)

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
        return await self._memory_admin_stats_service.get_user_memory_stats(
            tenant_id=tenant_id,
            user_id=user_id,
        )
