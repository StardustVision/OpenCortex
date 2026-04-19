# SPDX-License-Identifier: Apache-2.0
"""
Memory Orchestrator for OpenCortex.

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
from contextlib import suppress
from dataclasses import dataclass, field, replace
import hashlib
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union
from uuid import uuid4

from opencortex.cognition import (
    AutophagyKernel,
    CandidateStore,
    ConsolidationGate,
    CognitiveMetabolismController,
    CognitiveStateStore,
    RecallMutationEngine,
)
from opencortex.config import CortexConfig, get_config
from opencortex.prompts import (
    build_doc_summarization_prompt,
    build_layer_derivation_prompt,
    build_parent_summarization_prompt,
)
from opencortex.http.request_context import (
    get_effective_identity,
    get_effective_project_id,
)
from opencortex.intent import (
    MemoryBootstrapProbe,
    MemoryExecutor,
    QueryAnchorKind,
    RecallPlanner,
    RetrievalDepth,
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
    probe_candidate_ranks as build_probe_candidate_ranks,
    query_anchor_groups as build_query_anchor_groups,
    record_anchor_groups,
)
from opencortex.intent.types import probe_confidence
from opencortex.intent.timing import (
    StageTimingCollector,
    measure_async,
    measure_sync,
)
from opencortex.memory import (
    MemoryKind,
    memory_abstract_from_record,
    memory_anchor_hits_from_abstract,
    memory_kind_policy,
    memory_merge_signature_from_abstract,
)
from opencortex.utils.json_parse import parse_json_from_response
from opencortex.utils.text import smart_truncate, chunked_llm_derive
from opencortex.core.context import Context, ContextType as CoreContextType
from opencortex.core.message import Message
from opencortex.core.user_id import UserIdentifier
from opencortex.models.embedder.base import EmbedderBase
from opencortex.retrieve.intent_analyzer import IntentAnalyzer, LLMCompletionCallable
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
from opencortex.storage.collection_schemas import init_context_collection
from opencortex.storage.cortex_fs import CortexFS, init_cortex_fs
from opencortex.storage.storage_interface import StorageInterface
from opencortex.utils.uri import CortexURI
from opencortex.cognition.state_types import OwnerType

logger = logging.getLogger(__name__)

# Default collection name for all context types
_CONTEXT_COLLECTION = "context"

# Maximum number of batch_add items processed concurrently
_BATCH_ADD_CONCURRENCY = 8


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
    """
    Top-level orchestrator for OpenCortex memory operations.

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
        """
        Initialize all internal components.

        Creates the storage backend (if not provided), initializes CortexFS,
        sets up the context collection, and wires up the retriever.

        Returns:
            self (for chaining)
        """
        if self._initialized:
            return self

        # 1. Storage backend (auto-create QdrantStorageAdapter if not provided)
        if self._storage is None:
            from opencortex.storage.qdrant import QdrantStorageAdapter

            db_path = str(Path(self._config.data_root) / ".qdrant")
            qdrant_url = getattr(self._config, "qdrant_url", "") or ""
            self._storage = QdrantStorageAdapter(
                path=db_path,
                embedding_dim=self._config.embedding_dimension,
                url=qdrant_url,
            )
            logger.info(
                "[MemoryOrchestrator] Auto-created QdrantStorageAdapter at %s",
                qdrant_url or db_path,
            )

        # 1b. Embedder auto-creation
        if self._embedder is None:
            self._embedder = self._create_default_embedder()

        # 2. User identity (default; overridden per-request via HTTP headers)
        self._user = UserIdentifier("default", "default")

        # 3. CortexFS
        self._fs = init_cortex_fs(
            data_root=self._config.data_root,
            query_embedder=self._embedder,
            rerank_config=self._rerank_config,
            vector_store=self._storage,
        )

        # 4. Create context collection if needed
        await init_context_collection(
            self._storage,
            self._get_collection(),
            self._config.embedding_dimension,
        )

        # 5. Intent analyzer: use provided callable or auto-create from config
        if self._llm_completion is None:
            try:
                from opencortex.models.llm_factory import create_llm_completion

                self._llm_completion = create_llm_completion(self._config)
            except Exception as exc:
                logger.warning(
                    "[MemoryOrchestrator] Could not create LLM completion from config: %s",
                    exc,
                )

        if self._llm_completion:
            self._analyzer = IntentAnalyzer(llm_completion=self._llm_completion)

        # 6. Cone Retrieval: entity index + scorer (BEFORE retriever, so retriever gets live reference)
        if self._config.cone_retrieval_enabled:
            from opencortex.retrieve.entity_index import EntityIndex
            from opencortex.retrieve.cone_scorer import ConeScorer
            self._entity_index = EntityIndex()
            self._cone_scorer = ConeScorer(self._entity_index)
            asyncio.create_task(self._entity_index.build_for_collection(
                self._storage, self._get_collection()
            ))

        # 7. Memory bootstrap probe
        self._memory_probe = MemoryBootstrapProbe(
            storage=self._storage,
            embedder=self._embedder,
            collection_resolver=self._get_collection,
            filter_builder=self._build_probe_filter,
            top_k=6,
        )

        # 8. Background maintenance: text indexes, migrations, re-embed, recovery
        asyncio.create_task(self._startup_maintenance())

        # 8b. Document derive worker
        self._start_derive_worker()

        # 9. Autophagy cognition components
        await self._init_cognition()
        self._start_autophagy_sweeper()

        # 10. Cortex Alpha components
        await self._init_alpha()

        # 11. Skill Engine
        await self._init_skill_engine()

        self._initialized = True
        logger.info(
            "[MemoryOrchestrator] Initialized (data_root=%s)", self._config.data_root
        )
        return self

    async def _init_cognition(self) -> None:
        """Initialize cognition-layer stores/controllers/kernel."""
        if not self._storage:
            return

        self._cognitive_state_store = CognitiveStateStore(self._storage)
        await self._cognitive_state_store.init()

        self._candidate_store = CandidateStore(self._storage)
        await self._candidate_store.init()

        self._recall_mutation_engine = RecallMutationEngine()
        self._consolidation_gate = ConsolidationGate(
            candidate_store=self._candidate_store,
        )
        self._cognitive_metabolism_controller = CognitiveMetabolismController()
        self._autophagy_kernel = AutophagyKernel(
            state_store=self._cognitive_state_store,
            mutation_engine=self._recall_mutation_engine,
            consolidation_gate=self._consolidation_gate,
            candidate_store=self._candidate_store,
            metabolism_controller=self._cognitive_metabolism_controller,
        )

    def _start_autophagy_sweeper(self) -> None:
        """Start autophagy metabolism sweeps (startup + periodic) in background."""
        kernel = getattr(self, "_autophagy_kernel", None)
        if kernel is None:
            return

        # Be resilient to unit tests that bypass __init__ via __new__.
        if not hasattr(self, "_autophagy_sweep_cursor"):
            self._autophagy_sweep_cursor = None
        if not hasattr(self, "_autophagy_sweep_task"):
            self._autophagy_sweep_task = None
        if not hasattr(self, "_autophagy_startup_sweep_task"):
            self._autophagy_startup_sweep_task = None
        if not hasattr(self, "_autophagy_sweep_cursors"):
            self._autophagy_sweep_cursors = {
                OwnerType.MEMORY: None,
                OwnerType.TRACE: None,
            }
        if not hasattr(self, "_autophagy_sweep_guard"):
            self._autophagy_sweep_guard = asyncio.Lock()

        if self._autophagy_sweep_task is not None and not self._autophagy_sweep_task.done():
            return

        # Startup: one immediate batch (fire-and-forget) for crash recovery / backlog drain.
        self._autophagy_startup_sweep_task = asyncio.create_task(
            self._run_autophagy_sweep_once(),
            name="opencortex.autophagy.startup_sweep",
        )

        # Periodic: one bounded page per interval, cursor carried across ticks.
        self._autophagy_sweep_task = asyncio.create_task(
            self._autophagy_sweep_loop(),
            name="opencortex.autophagy.periodic_sweep",
        )

    async def _run_autophagy_sweep_once(self) -> None:
        kernel = getattr(self, "_autophagy_kernel", None)
        if kernel is None:
            return

        # Be resilient to unit tests that bypass __init__ via __new__.
        if not hasattr(self, "_autophagy_sweep_guard") or self._autophagy_sweep_guard is None:
            self._autophagy_sweep_guard = asyncio.Lock()
        if not hasattr(self, "_autophagy_sweep_cursors") or self._autophagy_sweep_cursors is None:
            self._autophagy_sweep_cursors = {
                OwnerType.MEMORY: None,
                OwnerType.TRACE: None,
            }

        async with self._autophagy_sweep_guard:
            limit = int(getattr(self._config, "autophagy_sweep_batch_size", 200))
            for owner_type in (OwnerType.MEMORY, OwnerType.TRACE):
                try:
                    cursor = self._autophagy_sweep_cursors.get(owner_type)
                    result = await kernel.sweep_metabolism(
                        owner_type=owner_type,
                        limit=limit,
                        cursor=cursor,
                    )
                    # Reset to None when exhausted, so subsequent sweeps restart cleanly.
                    self._autophagy_sweep_cursors[owner_type] = getattr(
                        result, "next_cursor", None
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "[Orchestrator] Autophagy metabolism sweep failed (owner_type=%s): %s",
                        owner_type.value,
                        exc,
                    )
                    continue

    async def _autophagy_sweep_loop(self) -> None:
        interval = float(getattr(self._config, "autophagy_sweep_interval_seconds", 900))
        if interval <= 0:
            interval = 0.01  # allow fast unit tests; never busy-loop.
        try:
            while True:
                await asyncio.sleep(interval)
                await self._run_autophagy_sweep_once()
        except asyncio.CancelledError:
            raise

    async def _init_alpha(self) -> None:
        """Initialize Cortex Alpha components if enabled."""
        alpha_cfg = self._config.cortex_alpha

        # Observer — always initialized (lightweight in-memory)
        from opencortex.alpha.observer import Observer

        self._observer = Observer()

        if self._storage and self._embedder and alpha_cfg.trace_splitter_enabled:
            # TraceStore
            from opencortex.alpha.trace_store import TraceStore

            self._trace_store = TraceStore(
                storage=self._storage,
                embedder=self._embedder,
                cortex_fs=self._fs,
                collection_name=alpha_cfg.trace_collection_name,
                embedding_dim=self._config.embedding_dimension,
                on_trace_saved=self._on_trace_saved if self._autophagy_kernel else None,
            )
            await self._trace_store.init()

        if self._storage and self._embedder and alpha_cfg.archivist_enabled:
            # KnowledgeStore
            from opencortex.alpha.knowledge_store import KnowledgeStore

            self._knowledge_store = KnowledgeStore(
                storage=self._storage,
                embedder=self._embedder,
                cortex_fs=self._fs,
                collection_name=alpha_cfg.knowledge_collection_name,
                embedding_dim=self._config.embedding_dimension,
            )
            await self._knowledge_store.init()

        # TraceSplitter (needs LLM)
        if self._llm_completion and alpha_cfg.trace_splitter_enabled:
            from opencortex.alpha.trace_splitter import TraceSplitter

            self._trace_splitter = TraceSplitter(
                llm_fn=self._llm_completion,
                max_context_tokens=alpha_cfg.trace_splitter_max_context_tokens,
            )

        # Archivist (needs LLM)
        if self._llm_completion and alpha_cfg.archivist_enabled:
            from opencortex.alpha.archivist import Archivist

            self._archivist = Archivist(
                llm_fn=self._llm_completion,
                embedder=self._embedder,
                trigger_threshold=alpha_cfg.archivist_trigger_threshold,
                trigger_mode=alpha_cfg.archivist_trigger_mode,
            )

        # ContextManager — three-phase lifecycle for memory_context protocol
        from opencortex.context import ContextManager

        self._context_manager = ContextManager(
            orchestrator=self,
            observer=self._observer,
        )
        await self._context_manager.start()

        logger.info("[MemoryOrchestrator] Cortex Alpha initialized")

    async def _init_skill_engine(self) -> None:
        """Initialize Skill Engine if storage and embedder are available."""
        if not self._storage or not self._embedder:
            return
        try:
            from opencortex.skill_engine.adapters.storage_adapter import SkillStorageAdapter
            from opencortex.skill_engine.adapters.llm_adapter import LLMCompletionAdapter
            from opencortex.skill_engine.store import SkillStore
            from opencortex.skill_engine.skill_manager import SkillManager
            from opencortex.skill_engine.http_routes import set_skill_manager

            storage_adapter = SkillStorageAdapter(
                storage=self._storage,
                embedder=self._embedder,
                embedding_dim=self._config.embedding_dimension,
            )
            await storage_adapter.initialize()
            store = SkillStore(storage_adapter)

            analyzer = None
            evolver = None
            llm_adapter = None

            if self._llm_completion:
                llm_adapter = LLMCompletionAdapter(self._llm_completion)

                from opencortex.skill_engine.evolver import SkillEvolver
                evolver = SkillEvolver(llm=llm_adapter, store=store)

                # SourceAdapter + Analyzer (for extraction pipeline)
                from opencortex.skill_engine.adapters.source_adapter import QdrantSourceAdapter
                from opencortex.skill_engine.analyzer import SkillAnalyzer
                source_adapter = QdrantSourceAdapter(
                    storage=self._storage, embedder=self._embedder,
                )
                analyzer = SkillAnalyzer(
                    source=source_adapter, llm=llm_adapter, store=store,
                )

            # Quality Gate (Phase A)
            quality_gate = None
            if llm_adapter:
                from opencortex.skill_engine.quality_gate import QualityGate
                quality_gate = QualityGate(llm=llm_adapter)

            # Sandbox TDD (Phase B — default OFF)
            sandbox_tdd = None
            if self._config.cortex_alpha.sandbox_tdd_enabled and llm_adapter:
                from opencortex.skill_engine.sandbox_tdd import SandboxTDD
                sandbox_tdd = SandboxTDD(
                    llm=llm_adapter,
                    max_llm_calls=self._config.cortex_alpha.sandbox_tdd_max_llm_calls,
                )

            self._skill_manager = SkillManager(
                store=store, analyzer=analyzer, evolver=evolver,
                quality_gate=quality_gate, sandbox_tdd=sandbox_tdd,
            )
            set_skill_manager(self._skill_manager)

            # SkillEventStore + Evaluator (Phase C)
            from opencortex.skill_engine.event_store import SkillEventStore
            from opencortex.skill_engine.evaluator import SkillEvaluator

            self._skill_event_store = SkillEventStore(storage=self._storage)
            await self._skill_event_store.init()

            self._skill_evaluator = SkillEvaluator(
                event_store=self._skill_event_store,
                skill_store=store,
                trace_store=self._trace_store,
                skill_storage=storage_adapter,
                llm=llm_adapter,
            )

            # Startup sweeper for crash recovery (fire-and-forget, all tenants)
            if self._skill_evaluator:
                asyncio.create_task(self._skill_evaluator.sweep_unevaluated())

            logger.info("[MemoryOrchestrator] Skill Engine initialized")
        except Exception as exc:
            logger.info("[MemoryOrchestrator] Skill Engine not available: %s", exc)

    def _create_default_embedder(self) -> Optional[EmbedderBase]:
        """
        Auto-create an embedder based on CortexConfig.

        Resolution order:
        1. If ``embedding_provider == "local"``, create a
           :class:`LocalEmbedder` using FastEmbed ONNX inference.
        2. If ``embedding_provider == "openai"``, create an
           :class:`OpenAIDenseEmbedder` (works with any OpenAI-compatible API).
        3. If nothing works, log a warning and return ``None``.

        All embedders are wrapped with BM25 sparse (hybrid search) then LRU cache.

        Returns:
            An :class:`EmbedderBase` instance, or ``None`` if creation fails.
        """
        import os

        provider = (self._config.embedding_provider or "").strip().lower()

        # Explicitly disabled — no embedding, degraded to filter/scroll search
        if provider in ("none", "disabled", "off"):
            logger.info(
                "[MemoryOrchestrator] embedding_provider='%s' — "
                "running without embedder (filter/scroll search only).",
                provider,
            )
            return None

        if provider == "local":
            return self._create_local_embedder()

        if provider == "volcengine":
            logger.warning(
                "[MemoryOrchestrator] embedding_provider='volcengine' is deprecated. "
                "Use 'openai' with the same API key/base URL."
            )
            return None

        if provider == "openai":
            try:
                from opencortex.models.embedder.openai_embedder import (
                    OpenAIDenseEmbedder,
                )

                api_key = self._config.embedding_api_key or os.environ.get(
                    "OPENCORTEX_EMBEDDING_API_KEY", ""
                )
                if not api_key:
                    logger.warning(
                        "[MemoryOrchestrator] embedding_provider='openai' but no "
                        "api_key found in config or OPENCORTEX_EMBEDDING_API_KEY env var. "
                        "Skipping auto-embedder creation."
                    )
                    return None

                model_name = self._config.embedding_model
                if not model_name:
                    logger.warning(
                        "[MemoryOrchestrator] embedding_provider='openai' but "
                        "embedding_model is not set. Skipping auto-embedder creation."
                    )
                    return None

                embedder = OpenAIDenseEmbedder(
                    model_name=model_name,
                    api_key=api_key,
                    api_base=self._config.embedding_api_base
                    or "https://api.openai.com/v1",
                    dimension=self._config.embedding_dimension or None,
                )
                logger.info(
                    "[MemoryOrchestrator] Auto-created OpenAIDenseEmbedder (model=%s)",
                    model_name,
                )
                return self._wrap_with_cache(self._wrap_with_hybrid(embedder))
            except ImportError as exc:
                logger.warning(
                    "[MemoryOrchestrator] Cannot create OpenAI embedder — "
                    "httpx not installed: %s",
                    exc,
                )
                return None
            except Exception as exc:
                logger.warning(
                    "[MemoryOrchestrator] Failed to create OpenAI embedder: %s",
                    exc,
                )
                return None

        # No provider configured — run without embedder (filter/scroll only)
        if not provider:
            logger.info(
                "[MemoryOrchestrator] No embedding_provider configured. "
                "Running without embedder (filter/scroll search only)."
            )
            return None

        # Unknown / unsupported provider
        logger.warning(
            "[MemoryOrchestrator] Unknown embedding_provider='%s'. "
            "No embedder will be auto-created.",
            provider,
        )
        return None

    def _create_local_embedder(self) -> Optional[EmbedderBase]:
        """Create a local FastEmbed embedder with BM25 sparse + LRU cache."""
        try:
            from opencortex.models.embedder.local_embedder import (
                DEFAULT_LOCAL_EMBEDDING_MODEL,
                LocalEmbedder,
            )

            model_name = (
                self._config.embedding_model or DEFAULT_LOCAL_EMBEDDING_MODEL
            )
            local_config = {"onnx_intra_op_threads": self._config.onnx_intra_op_threads}
            embedder = LocalEmbedder(model_name=model_name, config=local_config)
            if not embedder.is_available:
                logger.warning(
                    "[MemoryOrchestrator] LocalEmbedder failed to load '%s'. "
                    "Install with: uv add fastembed",
                    model_name,
                )
                return None

            # Update dimension from detected model
            detected_dim = embedder.get_dimension()
            if detected_dim and detected_dim != self._config.embedding_dimension:
                logger.info(
                    "[MemoryOrchestrator] Updating embedding_dimension %d → %d "
                    "from local model",
                    self._config.embedding_dimension,
                    detected_dim,
                )
                self._config.embedding_dimension = detected_dim

            logger.info(
                "[MemoryOrchestrator] Auto-created LocalEmbedder (model=%s, dim=%d)",
                model_name,
                detected_dim,
            )

            return self._wrap_with_cache(self._wrap_with_hybrid(embedder))

        except ImportError as exc:
            logger.warning(
                "[MemoryOrchestrator] Cannot create local embedder — "
                "fastembed not installed: %s",
                exc,
            )
            return None
        except Exception as exc:
            logger.warning(
                "[MemoryOrchestrator] Failed to create local embedder: %s",
                exc,
            )
            return None

    def _wrap_with_hybrid(self, embedder):
        """Wrap dense embedder with BM25 sparse for hybrid search.

        No-op if embedder is already hybrid.
        """
        from opencortex.models.embedder.base import HybridEmbedderBase

        if isinstance(embedder, HybridEmbedderBase):
            return embedder
        from opencortex.models.embedder.sparse import BM25SparseEmbedder
        from opencortex.models.embedder.base import CompositeHybridEmbedder

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

    async def _check_and_reembed(self) -> None:
        """Auto re-embed if the embedding model has changed since last run."""
        current_model = getattr(self._embedder, "model_name", "")
        if not current_model or not self._embedder:
            return

        marker = Path(self._config.data_root) / ".embedding_model"
        previous_model = marker.read_text().strip() if marker.exists() else ""

        if previous_model == current_model:
            return

        # Only re-embed if there are existing records
        try:
            count = await self._storage.count(self._get_collection())
        except Exception:
            count = 0

        if count == 0:
            # No data yet — just write the marker
            marker.write_text(current_model)
            return

        logger.info(
            "[Orchestrator] Embedding model changed: %s → %s. "
            "Re-embedding %d records ...",
            previous_model or "(none)",
            current_model,
            count,
        )
        from opencortex.migration.v040_reembed import reembed_all as _reembed_all

        updated = await _reembed_all(
            self._storage,
            self._get_collection(),
            self._embedder,
        )
        logger.info(
            "[Orchestrator] Re-embedded %d records (model: %s → %s)",
            updated,
            previous_model or "(none)",
            current_model,
        )
        marker.write_text(current_model)

    async def _startup_maintenance(self) -> None:
        """Background: text indexes, migrations, re-embed. Runs after init() returns."""
        if hasattr(self._storage, "ensure_text_indexes"):
            try:
                await self._storage.ensure_text_indexes()
            except Exception as exc:
                logger.warning("[Orchestrator] Text index setup failed: %s", exc)

        try:
            from opencortex.migration.v030_path_redesign import (
                backfill_new_fields,
                cleanup_root_junk,
            )

            await cleanup_root_junk(self._storage, self._fs, self._get_collection())
            await backfill_new_fields(self._storage, self._get_collection())
        except Exception as exc:
            logger.warning("[Orchestrator] Migration v0.3 skipped: %s", exc)

        try:
            from opencortex.migration.v040_project_backfill import backfill_project_id

            await backfill_project_id(self._storage, self._get_collection())
        except Exception as exc:
            logger.warning("[Orchestrator] Migration v0.4 skipped: %s", exc)

        try:
            await self._check_and_reembed()
        except Exception as exc:
            logger.warning("[Orchestrator] Auto re-embed skipped: %s", exc)

        await self._recover_pending_derives()

    # =========================================================================
    # Document Derive Worker
    # =========================================================================

    def _start_derive_worker(self) -> None:
        """Launch the background derive worker coroutine."""
        if self._derive_worker_task is None or self._derive_worker_task.done():
            self._derive_worker_task = asyncio.create_task(self._derive_worker())

    async def _derive_worker(self) -> None:
        """Consume _DeriveTask items from the queue. Stops on None sentinel."""
        while True:
            task = await self._derive_queue.get()
            if task is None:
                self._derive_queue.task_done()
                break
            try:
                await self._process_derive_task(task)
            except Exception as exc:
                logger.error(
                    "[DeriveWorker] Failed to process %s: %s",
                    task.parent_uri,
                    exc,
                )
            finally:
                self._derive_queue.task_done()

    async def _process_derive_task(self, task: _DeriveTask) -> None:
        """Process a single document derive task (Phase B).

        Creates parent record, derives chunks level-by-level, runs bottom-up
        summarization, then deletes the .derive_pending marker.
        """
        from opencortex.http.request_context import (
            set_request_identity,
            reset_request_identity,
        )

        tokens = set_request_identity(task.tenant_id, task.user_id)
        try:
            # 1. Create parent record in Qdrant (is_leaf=False, skips _derive_layers)
            parent_ctx = await self.add(
                abstract=task.abstract,
                content=task.content,
                category=task.category,
                uri=task.parent_uri,
                is_leaf=False,
                context_type=task.context_type,
                meta={
                    **task.meta,
                    "ingest_mode": "memory",
                    "source_doc_id": task.source_doc_id,
                    "source_doc_title": task.source_doc_title,
                    "source_section_path": "",
                    "chunk_role": "document",
                },
                session_id=task.session_id,
            )
            doc_parent_uri = parent_ctx.uri

            chunks = task.chunks
            # 2. Precompute topology
            is_dir_chunk = [
                any(c.parent_index == idx for c in chunks[idx + 1:])
                for idx in range(len(chunks))
            ]
            levels: Dict[int, List[int]] = {}
            for idx, chunk in enumerate(chunks):
                if chunk.parent_index < 0:
                    level = 0
                else:
                    parent_level = next(
                        (lv for lv, idxs in levels.items() if chunk.parent_index in idxs),
                        0,
                    )
                    level = parent_level + 1
                levels.setdefault(level, []).append(idx)

            chunk_results: List[Optional[Any]] = [None] * len(chunks)
            sem = asyncio.Semaphore(self._config.document_derive_concurrency)

            async def _process_chunk(idx: int) -> None:
                chunk = chunks[idx]
                chunk_parent = doc_parent_uri
                if chunk.parent_index >= 0:
                    parent_result = chunk_results[chunk.parent_index]
                    if parent_result is not None and not parent_result.is_leaf:
                        chunk_parent = parent_result.uri

                chunk_role = "section" if is_dir_chunk[idx] else "leaf"
                sp = chunk.meta.get("source_section_path", "") or chunk.meta.get(
                    "section_path", ""
                )
                if is_dir_chunk[idx]:
                    heading = sp.split(" > ")[-1].strip() if sp else chunk.content[:80].strip()
                    chunk_abstract = heading
                else:
                    chunk_abstract = ""

                embed_text = ""
                if self._config.context_flattening_enabled:
                    parts = []
                    if task.source_doc_title:
                        parts.append(f"[{task.source_doc_title}]")
                    if sp:
                        parts.append(f"[{sp}]")
                    if chunk_abstract:
                        parts.append(chunk_abstract)
                    embed_text = " ".join(parts)

                async with sem:
                    try:
                        ctx = await self.add(
                            abstract=chunk_abstract,
                            content=chunk.content,
                            category=task.category,
                            parent_uri=chunk_parent,
                            is_leaf=not is_dir_chunk[idx],
                            context_type=task.context_type,
                            meta={
                                **task.meta,
                                "ingest_mode": "memory",
                                "chunk_index": idx,
                                "source_doc_id": task.source_doc_id,
                                "source_doc_title": task.source_doc_title,
                                "source_section_path": sp,
                                "chunk_role": chunk_role,
                            },
                            session_id=task.session_id,
                            embed_text=embed_text,
                        )
                        chunk_results[idx] = ctx
                    except Exception as exc:
                        logger.warning(
                            "[DeriveWorker] chunk %d/%d failed: %s",
                            idx + 1, len(chunks), exc,
                        )

            # 3. Level-by-level concurrent derive
            for level in sorted(levels.keys()):
                level_tasks = [_process_chunk(idx) for idx in levels[level]]
                await asyncio.gather(*level_tasks)

            # 4. Bottom-up summarization
            for level in sorted(levels.keys(), reverse=True):
                for si in [i for i in levels[level] if is_dir_chunk[i]]:
                    if chunk_results[si] is None:
                        continue
                    child_indices = [
                        j for j in range(len(chunks)) if chunks[j].parent_index == si
                    ]
                    available = [
                        chunk_results[j].abstract
                        for j in child_indices
                        if chunk_results[j] is not None
                    ]
                    if not available:
                        continue
                    if len(available) < len(child_indices) / 2:
                        logger.warning(
                            "[DeriveWorker] section %d: >50%% children failed, skipping bottom-up",
                            si,
                        )
                        continue
                    summary = await self._derive_parent_summary(task.abstract, available)
                    if summary.get("abstract"):
                        try:
                            await self.update(
                                chunk_results[si].uri,
                                abstract=summary["abstract"],
                                overview=summary["overview"],
                                meta={"topics": summary.get("keywords", [])},
                            )
                            chunk_results[si].abstract = summary["abstract"]
                            chunk_results[si].overview = summary["overview"]
                        except Exception as exc:
                            logger.warning(
                                "[DeriveWorker] section %d bottom-up failed: %s", si, exc,
                            )

            # 5. Parent summary from top-level children
            top_children = [
                chunk_results[i].abstract
                for i in range(len(chunks))
                if chunks[i].parent_index < 0 and chunk_results[i] is not None
            ]
            if top_children:
                summary = await self._derive_parent_summary(task.abstract, top_children)
                if summary.get("abstract"):
                    try:
                        await self.update(
                            doc_parent_uri,
                            abstract=summary["abstract"],
                            overview=summary["overview"],
                            meta={"topics": summary.get("keywords", [])},
                        )
                    except Exception as exc:
                        logger.warning(
                            "[DeriveWorker] parent bottom-up failed: %s", exc,
                        )

            # 6. Delete .derive_pending marker on success
            try:
                fs_path = self._fs._uri_to_path(task.parent_uri)
                self._fs.agfs.rm(f"{fs_path}/.derive_pending")
            except Exception:
                pass

            logger.info(
                "[DeriveWorker] Completed %s (%d chunks)",
                task.parent_uri, len(chunks),
            )
        finally:
            self._inflight_derive_uris.discard(task.parent_uri)
            reset_request_identity(tokens)

    async def _recover_pending_derives(self) -> None:
        """Scan for .derive_pending markers and re-enqueue incomplete derives."""
        import json as _json
        from pathlib import Path

        data_root = Path(self._config.data_root).resolve()
        markers = list(data_root.rglob(".derive_pending"))
        if not markers:
            return

        if self._parser_registry is None:
            from opencortex.parse.registry import ParserRegistry
            self._parser_registry = ParserRegistry()

        recovered = 0
        for marker_path in markers:
            try:
                marker_data = _json.loads(marker_path.read_bytes())
                parent_uri = marker_data["parent_uri"]

                if parent_uri in self._inflight_derive_uris:
                    continue

                content_path = marker_path.parent / "content.md"
                if not content_path.exists():
                    logger.warning("[DeriveRecovery] Stale marker (no content.md) at %s — removing", marker_path)
                    marker_path.unlink(missing_ok=True)
                    continue

                content = content_path.read_text(encoding="utf-8")
                source_path = marker_data.get("source_path", "")
                if source_path:
                    parser = self._parser_registry.get_parser_for_file(source_path)
                else:
                    parser = None

                if parser:
                    chunks = await parser.parse_content(content, source_path=source_path)
                else:
                    chunks = await self._parser_registry.parse_content(content, source_format="markdown")

                task = _DeriveTask(
                    parent_uri=parent_uri,
                    content=content,
                    abstract=marker_data.get("source_doc_title", "") or (Path(source_path).stem if source_path else "Document"),
                    chunks=chunks,
                    category=marker_data.get("category", ""),
                    context_type=marker_data.get("context_type", "resource"),
                    meta=marker_data.get("meta", {}),
                    session_id=None,
                    source_path=source_path,
                    source_doc_id=marker_data.get("source_doc_id", ""),
                    source_doc_title=marker_data.get("source_doc_title", ""),
                    tenant_id=marker_data.get("tenant_id", ""),
                    user_id=marker_data.get("user_id", ""),
                )
                self._inflight_derive_uris.add(parent_uri)
                await self._derive_queue.put(task)
                recovered += 1
            except Exception as exc:
                logger.error("[DeriveRecovery] Failed to recover %s: %s", marker_path, exc)

        if recovered:
            logger.info("[DeriveRecovery] Re-enqueued %d pending derive task(s)", recovered)

    async def _drain_derive_queue(self) -> None:
        """Wait for all pending derive tasks to complete. Test-only."""
        await self._derive_queue.join()

    async def derive_status(self, uri: str) -> Dict[str, Any]:
        """Check the async derive status for a document URI.

        Returns dict with 'status' key: 'pending', 'completed', or 'not_found'.
        """
        if uri in self._inflight_derive_uris:
            return {"uri": uri, "status": "pending"}

        fs_path = self._fs._uri_to_path(uri)
        try:
            self._fs.agfs.read(f"{fs_path}/.derive_pending")
            return {"uri": uri, "status": "pending"}
        except (FileNotFoundError, Exception):
            pass

        records = await self._storage.filter(
            self._get_collection(),
            {"conds": [{"field": "uri", "op": "must", "value": uri}]},
            limit=1,
        )
        if records:
            return {"uri": uri, "status": "completed"}

        return {"uri": uri, "status": "not_found"}

    async def reembed_all(self) -> int:
        """Re-embed all records with the current embedder.

        Can be called manually or via the admin HTTP endpoint.

        Returns:
            Number of records updated.
        """
        from opencortex.migration.v040_reembed import reembed_all as _reembed_all

        count = await _reembed_all(
            self._storage,
            self._get_collection(),
            self._embedder,
        )
        # Update model marker
        marker = Path(self._config.data_root) / ".embedding_model"
        marker.write_text(getattr(self._embedder, "model_name", ""))
        return count

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
        from opencortex.http.request_context import (
            get_effective_identity,
            get_effective_project_id,
        )
        from opencortex.utils.uri import CortexURI
        from uuid import uuid4

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
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._embedder.embed, embed_input),
                timeout=2.0,
            )
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
            logger.warning("[MemoryOrchestrator] Immediate CortexFS write failed for %s: %s", uri, exc)
        return uri

    async def _add_document(
        self,
        content,
        abstract,
        overview,
        category,
        parent_uri,
        context_type,
        meta,
        session_id,
        source_path,
    ) -> "Context":
        """Document mode: parse content into chunks, write each to CortexFS + Qdrant."""
        if self._parser_registry is None:
            from opencortex.parse.registry import ParserRegistry

            self._parser_registry = ParserRegistry()
        registry = self._parser_registry
        if source_path:
            parser = registry.get_parser_for_file(source_path)
        else:
            parser = None

        if parser:
            chunks = await parser.parse_content(content, source_path=source_path)
        else:
            chunks = await registry.parse_content(content, source_format="markdown")

        # --- v0.6: Generate source_doc_id for document scoped search ---
        _effective_source_path = (
            source_path
            or (meta or {}).get("source_path", "")
            or (meta or {}).get("file_path", "")
        )
        if _effective_source_path:
            source_doc_id = hashlib.sha256(_effective_source_path.encode()).hexdigest()[
                :16
            ]
        else:
            source_doc_id = uuid4().hex[:16]
        source_doc_title = (meta or {}).get("title", "")
        if not source_doc_title and _effective_source_path:
            source_doc_title = os.path.basename(_effective_source_path)

        # Single chunk or no chunks → fall through to memory mode
        if len(chunks) <= 1:
            single_content = chunks[0].content if chunks else content
            embed_text = ""
            if self._config.context_flattening_enabled:
                parts = []
                if source_doc_title:
                    parts.append(f"[{source_doc_title}]")
                sp = chunks[0].meta.get("section_path", "") if chunks else ""
                if sp:
                    parts.append(f"[{sp}]")
                parts.append(abstract)
                embed_text = " ".join(parts)
            return await self.add(
                abstract=abstract,
                content=single_content,
                category=category,
                parent_uri=parent_uri,
                context_type=context_type,
                meta={
                    **(meta or {}),
                    "ingest_mode": "memory",
                    "source_doc_id": source_doc_id,
                    "source_doc_title": source_doc_title,
                    "source_section_path": chunks[0].meta.get("section_path", "")
                    if chunks
                    else "",
                    "chunk_role": "document",
                },
                session_id=session_id,
                embed_text=embed_text,
            )

        # Multi-chunk: async derive — return immediately, process in background
        doc_title = (
            Path(source_path).stem
            if source_path
            else abstract
            if abstract
            else "Document"
        )

        # Phase A: generate URI, write CortexFS, enqueue, return
        import json as _json

        parent_uri_candidate = self._auto_uri(
            context_type or "resource", category, abstract=doc_title
        )
        parent_uri_candidate = await self._resolve_unique_uri(parent_uri_candidate)
        while parent_uri_candidate in self._inflight_derive_uris:
            parent_uri_candidate = await self._resolve_unique_uri(
                parent_uri_candidate + "_"
            )
        self._inflight_derive_uris.add(parent_uri_candidate)

        tid, uid = get_effective_identity()

        # Write .derive_pending marker first (recovery signal)
        marker_data = _json.dumps({
            "parent_uri": parent_uri_candidate,
            "category": category,
            "context_type": context_type or "resource",
            "source_path": source_path or "",
            "source_doc_id": source_doc_id,
            "source_doc_title": source_doc_title,
            "meta": meta or {},
            "tenant_id": tid,
            "user_id": uid,
        }).encode("utf-8")
        fs_path = self._fs._uri_to_path(parent_uri_candidate)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: (
            self._fs.agfs.mkdir(fs_path),
            self._fs.agfs.write(f"{fs_path}/.derive_pending", marker_data),
        ))

        # Write L2 content to CortexFS
        await self._fs.write_context(
            uri=parent_uri_candidate, content=content
        )

        # Enqueue derive task
        task = _DeriveTask(
            parent_uri=parent_uri_candidate,
            content=content,
            abstract=doc_title,
            chunks=chunks,
            category=category,
            context_type=context_type or "resource",
            meta=meta or {},
            session_id=session_id,
            source_path=source_path or "",
            source_doc_id=source_doc_id,
            source_doc_title=source_doc_title,
            tenant_id=tid,
            user_id=uid,
        )
        await self._derive_queue.put(task)

        logger.info(
            "[MemoryOrchestrator] Document enqueued for async derive: %s (%d chunks)",
            parent_uri_candidate,
            len(chunks),
        )

        from opencortex.core.context import Context

        return Context(
            uri=parent_uri_candidate,
            abstract=doc_title,
            context_type=context_type or "resource",
            category=category,
            is_leaf=False,
            meta={**(meta or {}), "dedup_action": "created", "derive_pending": True},
            session_id=session_id,
        )

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
        """Derive L0/L1/keywords from L2 in a single LLM call.

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
                    llm_abstract = str(result.get("abstract") or "").strip()
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
                        user_abstract=user_abstract or llm_abstract,
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
            prompt = build_layer_derivation_prompt(content, user_abstract)
            try:
                response = await self._derive_layers_llm_completion(prompt)
                data = parse_json_from_response(response)
                if isinstance(data, dict):
                    llm_abstract = (data.get("abstract") or "").strip()
                    llm_overview = (data.get("overview") or "").strip()
                    keywords_list = data.get("keywords") or []
                    if isinstance(keywords_list, list):
                        keywords = ", ".join(str(k) for k in keywords_list if k)
                    else:
                        keywords = str(keywords_list)
                    entities_list = data.get("entities", [])
                    if isinstance(entities_list, list):
                        entities = [str(e).strip().lower() for e in entities_list if e][:20]
                    else:
                        entities = []
                    anchor_handles_list = data.get("anchor_handles", [])
                    if isinstance(anchor_handles_list, list):
                        anchor_handles = [
                            str(handle).strip()
                            for handle in anchor_handles_list
                            if str(handle).strip()
                        ][:6]
                    else:
                        anchor_handles = []
                    fact_points_list = data.get("fact_points", [])
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
                        user_abstract=user_abstract or llm_abstract,
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

    async def _complete_deferred_derive(
        self,
        uri: str,
        content: str,
        abstract: str = "",
        overview: str = "",
        session_id: str = "",
        meta: Optional[Dict[str, Any]] = None,
        context_type: str = "memory",
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

            from opencortex.core.context import Vectorize, Context

            vectorize_text = f"{new_abstract} {keywords_str}".strip() if keywords_str else new_abstract

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
        finally:
            self._deferred_derive_count -= 1

    async def wait_deferred_derives(self, poll_interval: float = 1.0) -> None:
        """Wait until all in-flight deferred derives complete."""
        while self._deferred_derive_count > 0:
            logger.info(
                "[Orchestrator] waiting for %d deferred derives...",
                self._deferred_derive_count,
            )
            await asyncio.sleep(poll_interval)

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
        """Derive a short abstract from a richer overview."""
        if user_abstract:
            return user_abstract

        overview_text = str(overview or "").strip()
        if overview_text:
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
        return memory_abstract_from_record(record).to_dict()

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
                "overview": anchor_text if len(anchor_text) >= 15 else f"{anchor_type}: {anchor_text}",
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
        return self._config

    @property
    def storage(self) -> StorageInterface:
        self._ensure_init()
        return self._storage

    @property
    def fs(self) -> CortexFS:
        self._ensure_init()
        return self._fs

    @property
    def user(self) -> UserIdentifier:
        self._ensure_init()
        return self._user

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
        """
        Add a new context (memory, resource, or skill).

        Performs the full pipeline: build URI -> embed -> store vector ->
        write filesystem (L0/L1).

        Args:
            abstract: Short summary (L0). Used as the vectorization text
                unless *embed_text* is provided.
            content: Full content (L2). Stored on filesystem.
            category: Category hint (e.g. "preferences", "entities", "patterns").
            parent_uri: Explicit parent URI. Auto-derived if not provided.
            uri: Explicit URI. Auto-generated if not provided.
            context_type: Explicit context type ("memory", "resource", "case", "pattern").
                          Auto-derived from URI if not provided.
            is_leaf: Whether this is a leaf node (default True).
            meta: Additional metadata dict.
            related_uri: List of related context URIs.
            session_id: Session identifier.
            dedup: If True, check for semantically similar records before
                inserting. Mergeable memory kinds update the stable object;
                non-mergeable kinds always append as new objects. Set False
                for bulk import.
            dedup_threshold: Minimum similarity score to consider a duplicate
                (default 0.82).
            embed_text: Optional text used for embedding instead of abstract.
                Useful when the display text (abstract) differs from the
                optimal search text (e.g., omitting date prefixes).

        Returns:
            The created Context object. ``meta["dedup_action"]`` indicates
            what happened: ``"created"`` (new) or ``"merged"``.
        """
        self._ensure_init()
        meta = dict(meta or {})
        explicit_entities = _merge_unique_strings(meta.get("entities"))
        explicit_topics = _merge_unique_strings(meta.get("topics"))

        # Determine ingestion mode
        from opencortex.ingest.resolver import IngestModeResolver

        ingest_mode = IngestModeResolver.resolve(
            content=content,
            meta=meta,
            source_path=meta.get("source_path", ""),
            session_id=session_id or "",
        )

        # Document mode: parse → chunks → write each with hierarchy
        if ingest_mode == "document" and content and is_leaf:
            return await self._add_document(
                content=content,
                abstract=abstract,
                overview=overview,
                category=category,
                parent_uri=parent_uri,
                context_type=context_type or "resource",
                meta=meta,
                session_id=session_id,
                source_path=meta.get("source_path", ""),
            )

        add_started = asyncio.get_running_loop().time()
        derive_layers_ms = 0
        embed_ms = 0
        dedup_ms = 0
        upsert_ms = 0
        fs_write_ms = 0

        # Build URI if not provided
        if not uri:
            uri = self._auto_uri(context_type or "memory", category, abstract=abstract)
            uri = await self._resolve_unique_uri(uri)
            existing_record = None
        else:
            existing_record = await self._get_record_by_uri(uri)

        # Build parent URI if not provided
        if not parent_uri:
            parent_uri = self._derive_parent_uri(uri)

        # Derive L0/L1/keywords from L2 in a single structured LLM call
        keywords = ""
        layers = {}
        if content and is_leaf and not defer_derive:
            derive_started = asyncio.get_running_loop().time()
            layers = await self._derive_layers(
                user_abstract=abstract,
                content=content,
                user_overview=overview,
            )
            derive_layers_ms = int(
                (asyncio.get_running_loop().time() - derive_started) * 1000,
            )
            if not abstract:
                abstract = layers["abstract"]
            if not overview:
                overview = layers["overview"]
            keywords = layers["keywords"]
            entities = _merge_unique_strings(
                layers.get("entities", []),
                explicit_entities,
            )
        elif content and is_leaf and defer_derive:
            # Deferred derive: use deterministic truncation as placeholder
            if not overview:
                overview = self._fallback_overview_from_content(
                    user_overview=overview, content=content,
                )
            if not abstract:
                abstract = self._derive_abstract_from_overview(
                    user_abstract=abstract, overview=overview, content=content,
                )
            entities = explicit_entities
        else:
            entities = explicit_entities

        keywords_list = _merge_unique_strings(
            _split_keyword_string(keywords),
            explicit_topics,
        )
        if keywords_list:
            meta["topics"] = _merge_unique_strings(meta.get("topics"), keywords_list)
        anchor_handles = _merge_unique_strings(
            meta.get("anchor_handles"),
            (layers.get("anchor_handles", []) if content and is_leaf else []),
        )
        if anchor_handles:
            meta["anchor_handles"] = anchor_handles
        keywords = ", ".join(keywords_list)

        # Build effective user identity (per-request or config default)
        tid, uid = get_effective_identity()
        effective_user = UserIdentifier(tid, uid)

        # Create context object
        ctx = Context(
            uri=uri,
            parent_uri=parent_uri,
            is_leaf=is_leaf,
            abstract=abstract,
            overview=overview,
            context_type=context_type,
            category=category,
            related_uri=related_uri or [],
            meta=meta,
            session_id=session_id,
            user=effective_user,
            id=(
                str(existing_record.get("id", "") or "")
                if existing_record is not None
                else None
            ),
        )

        # Override vectorization text.
        # Priority: embed_text > abstract+keywords > abstract (default from Context)
        from opencortex.core.context import Vectorize

        if embed_text:
            base_text = embed_text
        else:
            base_text = abstract
        if keywords:
            ctx.vectorize = Vectorize(f"{base_text} {keywords}")
        elif embed_text:
            ctx.vectorize = Vectorize(embed_text)

        effective_category = category or self._extract_category_from_uri(uri)
        abstract_json = self._build_abstract_json(
            uri=uri,
            context_type=context_type or "",
            category=effective_category,
            abstract=abstract,
            overview=overview,
            content=content,
            entities=entities,
            meta=meta,
            keywords=keywords_list,
            parent_uri=parent_uri,
            session_id=session_id,
        )
        # Inject fact_points from LLM derivation so _sync_anchor_projection_records
        # can generate fact_point records. Only present when content+is_leaf path ran.
        if content and is_leaf:
            abstract_json["fact_points"] = layers.get("fact_points", [])
        object_payload = self._memory_object_payload(abstract_json, is_leaf=is_leaf)
        memory_kind = MemoryKind(object_payload["memory_kind"])
        merge_signature = str(object_payload["merge_signature"])
        mergeable = bool(object_payload["mergeable"])

        # Embed (offload sync embedder to thread so we don't block the loop)
        result = None
        if self._embedder:
            loop = asyncio.get_event_loop()
            embed_started = asyncio.get_running_loop().time()
            result = await loop.run_in_executor(
                None, self._embedder.embed, ctx.get_vectorization_text()
            )
            embed_ms = int((asyncio.get_running_loop().time() - embed_started) * 1000)
            ctx.vector = result.dense_vector

        # --- Write-time semantic dedup ---
        if dedup and ctx.vector and is_leaf and mergeable:
            dedup_started = asyncio.get_running_loop().time()
            dup = await self._check_duplicate(
                vector=ctx.vector,
                memory_kind=memory_kind.value,
                merge_signature=merge_signature,
                threshold=dedup_threshold,
                tid=tid,
                uid=uid,
            )
            dedup_ms = int((asyncio.get_running_loop().time() - dedup_started) * 1000)
            if dup:
                existing_uri, existing_score = dup
                total_ms = int((asyncio.get_running_loop().time() - add_started) * 1000)
                existing_record = await self._get_record_by_uri(existing_uri)
                persisted_owner_id = ""
                persisted_project_id = get_effective_project_id()
                if existing_record:
                    persisted_owner_id = str(existing_record.get("id", ""))
                    persisted_project_id = str(
                        existing_record.get("project_id", persisted_project_id)
                    )
                await self._merge_into(existing_uri, abstract, content)
                await self._initialize_autophagy_owner_state(
                    owner_type=OwnerType.MEMORY,
                    owner_id=persisted_owner_id,
                    tenant_id=tid,
                    user_id=uid,
                    project_id=persisted_project_id,
                )
                logger.info(
                    "[MemoryOrchestrator] add tenant=%s user=%s uri=%s "
                    "dedup_action=merged dedup_target=%s score=%.3f "
                    "timing_ms(total=%d derive_layers=%d embed=%d dedup=%d upsert=%d fs_write=%d)",
                    tid,
                    uid,
                    uri,
                    existing_uri,
                    existing_score,
                    total_ms,
                    derive_layers_ms,
                    embed_ms,
                    dedup_ms,
                    upsert_ms,
                    fs_write_ms,
                )
                ctx.uri = existing_uri
                ctx.meta["dedup_action"] = "merged"
                ctx.meta["dedup_score"] = round(existing_score, 4)
                return ctx

        # Ensure parent directory records exist in vector DB
        if is_leaf and parent_uri:
            await self._ensure_parent_records(parent_uri)

        # Store in vector DB
        record = ctx.to_dict()
        if ctx.vector:
            record["vector"] = ctx.vector
        if self._embedder and result.sparse_vector:
            record["sparse_vector"] = result.sparse_vector

        # Populate scope/category/source fields for path-redesign
        inferred_scope = "private" if CortexURI(uri).is_private else "shared"
        record["scope"] = inferred_scope
        record["category"] = effective_category
        record["source_user_id"] = uid
        record["session_id"] = session_id or ""
        record["ttl_expires_at"] = ""
        record["project_id"] = get_effective_project_id()
        record["source_tenant_id"] = tid
        record["keywords"] = keywords
        record["entities"] = entities
        record.update(object_payload)
        record["abstract_json"] = abstract_json

        # v0.6: Flatten doc/conversation enrichment fields to top-level payload
        record["source_doc_id"] = (meta or {}).get("source_doc_id", "")
        record["source_doc_title"] = (meta or {}).get("source_doc_title", "")
        record["source_section_path"] = (meta or {}).get("source_section_path", "")
        record["chunk_role"] = (meta or {}).get("chunk_role", "")
        record["speaker"] = (meta or {}).get("speaker", "")
        record["event_date"] = (meta or {}).get("event_date")

        # Set TTL for short-lived record types
        if context_type == "staging":
            record["ttl_expires_at"] = self._ttl_from_hours(
                self._config.immediate_event_ttl_hours
            )
        elif (
            (context_type or "memory") == "memory"
            and effective_category == "events"
            and (meta or {}).get("layer") == "merged"
        ):
            record["ttl_expires_at"] = self._ttl_from_hours(
                self._config.merged_event_ttl_hours
            )

        upsert_started = asyncio.get_running_loop().time()
        await self._storage.upsert(self._get_collection(), record)
        upsert_ms = int((asyncio.get_running_loop().time() - upsert_started) * 1000)
        await self._sync_anchor_projection_records(
            source_record=record,
            abstract_json=abstract_json,
        )

        if (context_type or ctx.context_type or "memory") == "memory":
            await self._initialize_autophagy_owner_state(
                owner_type=OwnerType.MEMORY,
                owner_id=str(record["id"]),
                tenant_id=tid,
                user_id=uid,
                project_id=record["project_id"],
            )

        # Sync EntityIndex (if available)
        _entity_idx = getattr(self, '_entity_index', None)
        if _entity_idx and entities:
            _entity_idx.add(self._get_collection(), str(record["id"]), entities)

        # CortexFS write — fire-and-forget (Qdrant upsert is the synchronous path)
        def _on_fs_done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.warning(
                    "[Orchestrator] CortexFS write failed for %s: %s", uri, exc
                )

        _fs_task = asyncio.create_task(
            self._fs.write_context(
                uri=uri,
                content=content,
                abstract=abstract,
                abstract_json=abstract_json,
                overview=overview,
                is_leaf=is_leaf,
            )
        )
        _fs_task.add_done_callback(_on_fs_done)
        fs_write_ms = 0  # Non-blocking

        ctx.meta["dedup_action"] = "created"
        total_ms = int((asyncio.get_running_loop().time() - add_started) * 1000)
        logger.info(
            "[MemoryOrchestrator] add tenant=%s user=%s uri=%s dedup_action=created "
            "timing_ms(total=%d derive_layers=%d embed=%d dedup=%d upsert=%d fs_write=%d)",
            tid,
            uid,
            uri,
            total_ms,
            derive_layers_ms,
            embed_ms,
            dedup_ms,
            upsert_ms,
            fs_write_ms,
        )
        return ctx

    def _ttl_from_hours(self, hours: int) -> str:
        """Return RFC3339 UTC expiry string. Non-positive values disable TTL."""
        if hours <= 0:
            return ""
        expires = datetime.now(timezone.utc) + timedelta(hours=hours)
        return expires.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ------------------------------------------------------------------
    # Write-time dedup helpers
    # ------------------------------------------------------------------

    async def _check_duplicate(
        self,
        vector: list,
        memory_kind: str,
        merge_signature: str,
        threshold: float,
        tid: str,
        uid: str,
    ) -> Optional[tuple]:
        """Return ``(existing_uri, score)`` if a duplicate is found, else None."""
        try:
            # Build scope-aware filter: same tenant, same category, leaf only
            conds: list = [
                {"op": "must", "field": "source_tenant_id", "conds": [tid]},
                {"op": "must", "field": "is_leaf", "conds": [True]},
            ]
            if memory_kind:
                conds.append(
                    {"op": "must", "field": "memory_kind", "conds": [memory_kind]}
                )
            if merge_signature:
                conds.append(
                    {
                        "op": "must",
                        "field": "merge_signature",
                        "conds": [merge_signature],
                    }
                )
            # Scope: shared OR (private AND own user)
            conds.append(
                {
                    "op": "or",
                    "conds": [
                        {"op": "must", "field": "scope", "conds": ["shared"]},
                        {
                            "op": "and",
                            "conds": [
                                {"op": "must", "field": "scope", "conds": ["private"]},
                                {
                                    "op": "must",
                                    "field": "source_user_id",
                                    "conds": [uid],
                                },
                            ],
                        },
                    ],
                }
            )
            # Project isolation: only dedup within same project
            project_id = get_effective_project_id()
            if project_id:
                conds.append(
                    {"op": "must", "field": "project_id", "conds": [project_id]}
                )

            dedup_filter = {"op": "and", "conds": conds}

            results = await self._storage.search(
                self._get_collection(),
                query_vector=vector,
                filter=dedup_filter,
                limit=1,
                output_fields=["uri", "abstract"],
            )
            if results:
                score = results[0].get("_score", results[0].get("score", 0.0))
                if score >= threshold:
                    return (results[0]["uri"], score)
        except Exception as exc:
            logger.debug("[MemoryOrchestrator] Dedup check failed: %s", exc)
        return None

    async def _merge_into(
        self, existing_uri: str, new_abstract: str, new_content: str
    ) -> None:
        """Merge new content into an existing record and reinforce it."""
        records = await self._storage.filter(
            self._get_collection(),
            {"op": "must", "field": "uri", "conds": [existing_uri]},
            limit=1,
            output_fields=["abstract", "overview"],
        )
        existing_content = ""
        if records:
            # Read existing L2 content from filesystem
            try:
                existing_content = await self._fs.read_file(existing_uri)
            except Exception:
                existing_content = ""

        merged_content = (
            f"{existing_content}\n---\n{new_content}".strip()
            if new_content
            else existing_content
        )
        await self.update(existing_uri, abstract=new_abstract, content=merged_content)
        # Positive reinforcement for the merged record
        await self.feedback(existing_uri, 0.5)

    async def update(
        self,
        uri: str,
        abstract: Optional[str] = None,
        content: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        overview: Optional[str] = None,
    ) -> bool:
        """
        Update an existing context.

        Re-embeds if abstract changes, updates vector DB and filesystem.

        Args:
            uri: URI of the context to update.
            abstract: New abstract (re-embeds if changed).
            content: New full content.
            meta: Metadata fields to merge.
            overview: New L1 overview. When provided together with abstract,
                _derive_layers fast-path is used (no extra LLM call).

        Returns:
            True if updated successfully, False if not found.
        """
        self._ensure_init()

        # Find existing record
        records = await self._storage.filter(
            self._get_collection(),
            {"op": "must", "field": "uri", "conds": [uri]},
            limit=1,
        )
        if not records:
            logger.warning("[MemoryOrchestrator] Context not found: %s", uri)
            return False

        record = records[0]
        record_id = record.get("id", "")

        update_data: Dict[str, Any] = {}
        next_meta = record.get("meta", {})
        if isinstance(next_meta, str):
            import json

            try:
                next_meta = json.loads(next_meta)
            except (json.JSONDecodeError, TypeError):
                next_meta = {}
        elif not isinstance(next_meta, dict):
            next_meta = {}

        if meta:
            next_meta.update(meta)
            update_data["meta"] = next_meta
        if abstract is not None:
            update_data["abstract"] = abstract

        next_abstract = abstract if abstract is not None else record.get("abstract", "")
        next_content = content if content is not None else record.get("content", "")
        next_overview = overview if overview is not None else record.get("overview", "")
        next_entities = _merge_unique_strings(
            record.get("entities") or [],
            next_meta.get("entities"),
        )
        next_keywords_list = _merge_unique_strings(
            next_meta.get("topics"),
            _split_keyword_string(record.get("keywords", "")),
        )
        derived_fact_points: Optional[List[str]] = None
        if next_content and (abstract is not None or content is not None):
            # When content changed, force full LLM re-derivation so fact_points
            # are regenerated (ADV-001). Passing non-empty user_overview would
            # hit _derive_layers' fast-path which returns empty fact_points.
            derive_user_overview = "" if content is not None else next_overview
            derive_result = await self._derive_layers(
                user_abstract=next_abstract,
                content=next_content,
                user_overview=derive_user_overview,
            )
            next_entities = _merge_unique_strings(
                derive_result.get("entities", []),
                next_entities,
            )
            next_keywords_list = _merge_unique_strings(
                next_keywords_list,
                _split_keyword_string(derive_result.get("keywords", "")),
            )
            next_anchor_handles = _merge_unique_strings(
                next_meta.get("anchor_handles"),
                derive_result.get("anchor_handles", []),
            )
            if next_anchor_handles:
                next_meta["anchor_handles"] = next_anchor_handles
            # ADV-001 fix: capture fact_points so _sync_anchor_projection_records
            # regenerates fp records instead of wiping them.
            raw_fps = derive_result.get("fact_points", [])
            derived_fact_points = [str(fp) for fp in raw_fps] if isinstance(raw_fps, list) else []
        if next_keywords_list:
            next_meta["topics"] = _merge_unique_strings(
                next_meta.get("topics"),
                next_keywords_list,
            )
            update_data["keywords"] = ", ".join(next_keywords_list)
        if next_entities:
            update_data["entities"] = next_entities
        if update_data.get("meta") is not None or next_meta:
            update_data["meta"] = next_meta
        if self._embedder and (abstract is not None or content is not None):
            loop = asyncio.get_event_loop()
            embed_input = next_abstract
            if next_keywords_list:
                embed_input = f"{embed_input} {', '.join(next_keywords_list)}".strip()
            result = await loop.run_in_executor(
                None, self._embedder.embed, embed_input
            )
            update_data["vector"] = result.dense_vector
            if result.sparse_vector:
                update_data["sparse_vector"] = result.sparse_vector
        abstract_json = self._build_abstract_json(
            uri=uri,
            context_type=record.get("context_type", ""),
            category=record.get("category", ""),
            abstract=next_abstract,
            overview=next_overview,
            content=next_content,
            entities=next_entities,
            meta=next_meta,
            keywords=next_keywords_list,
            parent_uri=record.get("parent_uri", ""),
            session_id=record.get("session_id", ""),
        )
        # ADV-001 fix: inject fact_points symmetric to add() (line ~2013-2016).
        # If _derive_layers ran, use its fact_points. Otherwise (fast path),
        # preserve existing fact_points from the stored abstract_json so
        # _sync_anchor_projection_records does not wipe them.
        if derived_fact_points is not None:
            abstract_json["fact_points"] = derived_fact_points
        else:
            prior_abstract_json = record.get("abstract_json")
            if isinstance(prior_abstract_json, dict):
                prior_fps = prior_abstract_json.get("fact_points") or []
                if isinstance(prior_fps, list):
                    abstract_json["fact_points"] = [str(fp) for fp in prior_fps]
        update_data.update(
            self._memory_object_payload(
                abstract_json,
                is_leaf=bool(record.get("is_leaf", False)),
            )
        )
        update_data["abstract_json"] = abstract_json

        if update_data:
            await self._storage.update(self._get_collection(), record_id, update_data)
            updated_record = dict(record)
            updated_record.update(update_data)
            await self._sync_anchor_projection_records(
                source_record=updated_record,
                abstract_json=abstract_json,
            )

        # Update filesystem
        if abstract is not None or content is not None or overview is not None:
            await self._fs.write_context(
                uri=uri,
                content=next_content,
                abstract=next_abstract,
                overview=next_overview,
                abstract_json=abstract_json,
            )

        # Sync entity index if content/abstract changed (skip for non-leaf nodes)
        if (
            getattr(self, '_entity_index', None)
            and (abstract is not None or content is not None)
            and record.get("is_leaf") is not False
        ):
            try:
                text_for_entities = content or abstract or ""
                if text_for_entities and self._llm_completion:
                    derive_result = await self._derive_layers(
                        user_abstract=abstract or record.get("abstract", ""),
                        content=text_for_entities,
                    )
                    new_entities = derive_result.get("entities", [])
                else:
                    new_entities = []
                self._entity_index.update(self._get_collection(), str(record_id), new_entities)
                if new_entities:
                    await self._storage.update(
                        self._get_collection(), record_id, {"entities": new_entities}
                    )
            except Exception as exc:
                logger.warning("[MemoryOrchestrator] Entity sync on update failed: %s", exc)

        logger.info("[MemoryOrchestrator] Updated context: %s", uri)
        return True

    async def remove(self, uri: str, recursive: bool = True) -> int:
        """
        Remove a context from both vector DB and filesystem.

        Args:
            uri: URI of the context to remove.
            recursive: If True, removes all descendants (for directories).

        Returns:
            Number of records removed from vector DB.
        """
        self._ensure_init()

        # Pre-delete: get affected record IDs for entity index sync
        affected_ids_for_entity = []
        if getattr(self, '_entity_index', None):
            try:
                collection = self._get_collection()
                # Use prefix match to catch recursive descendants
                # (remove_by_uri uses MatchText which is prefix-like)
                affected = await self._storage.filter(
                    collection,
                    {"op": "prefix", "field": "uri", "prefix": uri},
                    limit=10000,
                )
                affected_ids_for_entity = [str(r["id"]) for r in affected]
            except Exception:
                pass

        # Remove from vector DB
        count = await self._storage.remove_by_uri(self._get_collection(), uri)

        # Post-delete: sync entity index
        if getattr(self, '_entity_index', None) and affected_ids_for_entity:
            self._entity_index.remove_batch(self._get_collection(), affected_ids_for_entity)

        # Remove from filesystem
        try:
            await self._fs.rm(uri, recursive=recursive)
        except Exception as e:
            logger.warning("[MemoryOrchestrator] FS removal failed for %s: %s", uri, e)

        logger.info("[MemoryOrchestrator] Removed %d records for: %s", count, uri)
        return count

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

    def _build_typed_queries(
        self,
        *,
        query: str,
        context_type: Optional[ContextType],
        target_uri: str,
        retrieve_plan: RetrievalPlan,
        runtime_bound_plan: Dict[str, Any],
    ) -> List[TypedQuery]:
        """Project planner posture into concrete retrieval queries."""
        if context_type:
            types_to_search = [context_type]
        elif target_uri:
            types_to_search = [self._infer_context_type(target_uri)]
        else:
            raw_context_types = runtime_bound_plan.get("context_types") or ["memory"]
            if len(raw_context_types) > 1:
                types_to_search = [ContextType.ANY]
            else:
                types_to_search = [
                    self._context_type_from_value(raw_value)
                    for raw_value in raw_context_types
                ]

        return [
            TypedQuery(
                query=query,
                context_type=ct,
                intent="memory",
                priority=1,
                target_directories=[target_uri] if target_uri else [],
                detail_level=self._detail_level_from_retrieval_depth(
                    retrieve_plan.retrieval_depth
                ),
            )
            for ct in types_to_search
        ]

    @staticmethod
    def _context_type_from_value(raw_value: str) -> ContextType:
        try:
            return ContextType(raw_value)
        except ValueError:
            return ContextType.ANY

    @staticmethod
    def _detail_level_from_retrieval_depth(
        retrieval_depth: RetrievalDepth,
    ) -> DetailLevel:
        return DetailLevel(retrieval_depth.value)

    @staticmethod
    def _summarize_retrieve_breakdown(
        query_results: List[QueryResult],
    ) -> Dict[str, float]:
        """Aggregate per-query retrieval timings into a request-level breakdown."""
        keys = ("embed", "search", "rerank", "assemble", "total")
        if not query_results:
            return {key: 0.0 for key in keys}

        summary: Dict[str, float] = {}
        for key in keys:
            values = [
                float((query_result.timing_ms or {}).get(key, 0.0))
                for query_result in query_results
            ]
            summary[key] = round(max(values, default=0.0), 4)
        return summary

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
            or not self._entity_index.is_ready(self._get_collection())
        ):
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
            URI_HOP_COST as _URI_HOP_COST,
            HIGH_CONFIDENCE_THRESHOLD as _HIGH_CONF_THRESHOLD,
            HIGH_CONFIDENCE_DISCOUNT as _HIGH_CONF_DISCOUNT,
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
                hop = _URI_HOP_COST * _HIGH_CONF_DISCOUNT if d < _HIGH_CONF_THRESHOLD else _URI_HOP_COST
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
        """
        Search for relevant contexts.

        Uses probe -> planner -> runtime to determine retrieval posture.

        Args:
            query: Natural language query.
            context_type: Restrict to a specific type (memory/resource/skill).
            target_uri: Restrict search to a directory subtree.
            limit: Maximum results per type.
            score_threshold: Minimum relevance score.
            metadata_filter: Additional filter conditions.
            detail_level: Fallback detail level if planner does not override.
            meta: Optional metadata dict.
            session_context: Optional session context for runtime scope.

        Returns:
            FindResult with memories, resources, and skills.
        """
        self._ensure_init()
        search_started = asyncio.get_running_loop().time()
        tid, uid = get_effective_identity()
        stage_timings = StageTimingCollector()

        target_doc_id = None
        if isinstance(meta, dict):
            target_doc_id = meta.get("target_doc_id")

        detail_level_value = (
            detail_level.value if isinstance(detail_level, DetailLevel) else detail_level
        )
        detail_level_override = (
            detail_level_value if detail_level_value != DetailLevel.L1.value else None
        )
        scope_filter = build_scope_filter(
            context_type=context_type,
            target_uri=target_uri,
            target_doc_id=target_doc_id,
            session_context=session_context,
            metadata_filter=metadata_filter,
        )

        if probe_result is None:
            probe_result = await measure_async(
                stage_timings,
                "probe",
                self.probe_memory,
                query,
                context_type=context_type,
                target_uri=target_uri,
                target_doc_id=target_doc_id,
                session_context=session_context,
                metadata_filter=metadata_filter,
            )
        else:
            stage_timings.record_ms("probe", 0)
        if retrieve_plan is None:
            scope_input = build_probe_scope_input(
                context_type=context_type,
                target_uri=target_uri,
                target_doc_id=target_doc_id,
                session_context=session_context,
            )
            retrieve_plan = measure_sync(
                stage_timings,
                "plan",
                self.plan_memory,
                query=query,
                probe_result=probe_result,
                max_items=limit,
                recall_mode="auto",
                detail_level_override=detail_level_override,
                scope_input=scope_input,
            )
        else:
            stage_timings.record_ms("plan", 0)
        intent_ms = (
            stage_timings.snapshot()["probe"] + stage_timings.snapshot()["plan"]
        )

        if retrieve_plan is None:
            total_ms = int((asyncio.get_running_loop().time() - search_started) * 1000)
            logger.debug(
                "[search] should_recall=False tenant=%s user=%s total_ms=%d",
                tid,
                uid,
                total_ms,
            )
            return FindResult(
                memories=[],
                resources=[],
                skills=[],
                probe_result=probe_result,
            )

        runtime_bound_plan = measure_sync(
            stage_timings,
            "bind",
            self.bind_memory_runtime,
            probe_result=probe_result,
            retrieve_plan=retrieve_plan,
            max_items=limit,
            session_context=session_context,
            include_knowledge=False,
        )
        effective_limit = runtime_bound_plan["memory_limit"]
        detail_level = runtime_bound_plan["effective_depth"]
        typed_queries = self._build_typed_queries(
            query=query,
            context_type=context_type,
            target_uri=target_uri,
            retrieve_plan=retrieve_plan,
            runtime_bound_plan=runtime_bound_plan,
        )
        if target_doc_id:
            for typed_query in typed_queries:
                typed_query.target_doc_id = target_doc_id

        # Set target directories on queries if specified
        if target_uri:
            for tq in typed_queries:
                if not tq.target_directories:
                    tq.target_directories = [target_uri]

        search_filter = self._build_search_filter(
            metadata_filter=scope_filter,
        )

        # Build retrieval coroutines
        retrieval_coros = [
            self._execute_object_query(
                typed_query=tq,
                limit=effective_limit,
                score_threshold=score_threshold,
                search_filter=search_filter,
                retrieve_plan=retrieve_plan,
                probe_result=probe_result,
                bound_plan=runtime_bound_plan,
            )
            for tq in typed_queries
        ]

        query_results = await measure_async(
            stage_timings,
            "retrieve",
            asyncio.gather,
            *retrieval_coros,
        )
        query_results = list(query_results)
        hydration_actions: List[Dict[str, Any]] = []

        aggregate_started = asyncio.get_running_loop().time()
        result = self._aggregate_results(query_results, limit=limit)
        result.probe_result = probe_result
        result.retrieve_plan = retrieve_plan
        retrieve_breakdown_ms = self._summarize_retrieve_breakdown(query_results)

        # Filter out directory nodes (is_leaf=False) — they exist for
        # hierarchical traversal but have no abstract/content of their own.
        result.memories = [m for m in result.memories if m.is_leaf]
        result.resources = [m for m in result.resources if m.is_leaf]
        result.skills = [m for m in result.skills if m.is_leaf]

        # Fire-and-forget: resolve URIs → record IDs → update access stats
        all_matched = result.memories + result.resources + result.skills

        stage_timings.record_elapsed("aggregate", aggregate_started)
        total_ms = int((asyncio.get_running_loop().time() - search_started) * 1000)
        stage_timings.record_ms("total", total_ms)
        timing_snapshot = stage_timings.snapshot()
        retrieval_latency_ms = (
            max(timing_snapshot["retrieve"], 0)
            + max(timing_snapshot.get("hydrate", 0), 0)
        )
        overhead_ms = timing_snapshot["overhead"]
        if runtime_bound_plan is not None:
            runtime_items = [
                {
                    "uri": mc.uri,
                    "context_type": mc.context_type.value,
                    "score": mc.score,
                }
                for mc in all_matched
            ]
            result.runtime_result = self._memory_runtime.finalize(
                bound_plan=runtime_bound_plan,
                items=runtime_items,
                latency_ms=retrieval_latency_ms,
                stage_timing_ms=timing_snapshot,
                retrieve_breakdown_ms=retrieve_breakdown_ms,
                hydration_actions=hydration_actions,
            )
        logger.info(
            "[search] tenant=%s user=%s probe_candidates=%d queries=%d results=%d "
            "timing_ms(total=%d intent=%d retrieval=%d overhead=%d)",
            tid,
            uid,
            probe_result.evidence.candidate_count,
            len(typed_queries),
            len(all_matched),
            total_ms,
            intent_ms,
            retrieval_latency_ms,
            overhead_ms,
        )

        # v0.6: Build SearchExplainSummary
        if getattr(self._config, "explain_enabled", True) and query_results:
            from opencortex.retrieve.types import SearchExplainSummary

            primary = query_results[0]
            result.explain_summary = SearchExplainSummary(
                total_ms=float(total_ms),
                query_count=len(query_results),
                primary_query_class=primary.explain.query_class
                if primary.explain
                else "",
                primary_path=primary.explain.path if primary.explain else "",
                doc_scope_hit=any(
                    qr.explain and qr.explain.doc_scope_hit for qr in query_results
                ),
                time_filter_hit=any(
                    qr.explain and qr.explain.time_filter_hit for qr in query_results
                ),
                rerank_triggered=any(
                    qr.explain and qr.explain.rerank_ms > 0 for qr in query_results
                ),
            )

        # Skill Engine: search active skills and merge into FindResult.skills
        if self._skill_manager:
            try:
                from opencortex.retrieve.types import MatchedContext
                skill_results = await self._skill_manager.search(
                    query, tid, uid, top_k=3,
                )
                for sr in skill_results:
                    result.skills.append(MatchedContext(
                        uri=sr.uri,
                        context_type=ContextType.SKILL,
                        is_leaf=True,
                        abstract=sr.abstract,
                        overview=sr.overview,
                        content=sr.content,
                        category=sr.category.value,
                        score=0.0,
                        session_id="",
                    ))
            except Exception as exc:
                logger.debug("[search] Skill search failed: %s", exc)

        result.total = len(result.memories) + len(result.resources) + len(result.skills)
        return result

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
        """Run recall-side autophagy bookkeeping off the request hot path."""
        if not memories or getattr(self, "_autophagy_kernel", None) is None:
            return
        task = asyncio.create_task(
            self._run_recall_bookkeeping(
                memories=memories,
                query=query,
                tenant_id=tenant_id,
                user_id=user_id,
            ),
            name="opencortex.autophagy.recall_bookkeeping",
        )
        self._recall_bookkeeping_tasks_set().add(task)
        task.add_done_callback(self._recall_bookkeeping_tasks_set().discard)

    def _recall_bookkeeping_tasks_set(self) -> set[asyncio.Task[Any]]:
        """Return the tracked background recall bookkeeping task set."""
        if not hasattr(self, "_recall_bookkeeping_tasks"):
            self._recall_bookkeeping_tasks = set()
        return self._recall_bookkeeping_tasks

    async def _run_recall_bookkeeping(
        self,
        *,
        memories: List[Any],
        query: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        """Apply autophagy recall bookkeeping without blocking retrieval."""
        recalled_owner_ids: List[str] = []
        try:
            recalled_owner_ids = await self._resolve_memory_owner_ids(memories)
            if not recalled_owner_ids or self._autophagy_kernel is None:
                return
            await self._autophagy_kernel.apply_recall_outcome(
                owner_ids=recalled_owner_ids,
                query=query,
                recall_outcome={"selected_results": recalled_owner_ids},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[search] Autophagy recall application failed tenant=%s user=%s owners=%d: %s",
                tenant_id,
                user_id,
                len(recalled_owner_ids),
                exc,
            )

    async def _get_record_by_uri(self, uri: str) -> Optional[Dict[str, Any]]:
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

    async def _on_trace_saved(self, trace: Any) -> None:
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
        """
        Session-aware search using IntentAnalyzer for query planning.

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
        """List user's accessible memories with readable content.

        Returns private (own) + shared memories, ordered by updated_at desc.
        """
        self._ensure_init()
        tid, uid = get_effective_identity()

        # Same scope filter as search(): private own + shared
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

        conds: List[Dict[str, Any]] = [
            {"op": "must_not", "field": "context_type", "conds": ["staging"]},
            scope_filter,
        ]
        if tid:
            conds.append(
                {"op": "must", "field": "source_tenant_id", "conds": [tid, ""]}
            )
        if category:
            conds.append({"op": "must", "field": "category", "conds": [category]})
        if context_type:
            conds.append(
                {"op": "must", "field": "context_type", "conds": [context_type]}
            )

        # Project filter: strict isolation
        project_id = get_effective_project_id()
        if project_id and project_id != "public":
            conds.append(
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

        combined: Dict[str, Any] = {"op": "and", "conds": conds}

        records = await self._storage.filter(
            self._get_collection(),
            combined,
            limit=limit,
            offset=offset,
            order_by="updated_at",
            order_desc=True,
        )

        items: List[Dict[str, Any]] = []
        for record in records:
            if not record.get("abstract"):
                continue
            item = {
                "uri": record.get("uri", ""),
                "abstract": record.get("abstract", ""),
                "category": record.get("category", ""),
                "context_type": record.get("context_type", ""),
                "scope": record.get("scope", ""),
                "project_id": record.get("project_id", ""),
                "updated_at": record.get("updated_at", ""),
                "created_at": record.get("created_at", ""),
            }
            if include_payload:
                meta = dict(record.get("meta") or {})
                item.update(
                    {
                        "meta": record.get("meta", {}),
                        "abstract_json": record.get("abstract_json", {}),
                        "session_id": record.get("session_id", ""),
                        "speaker": record.get("speaker", ""),
                        "event_date": record.get("event_date", ""),
                        "overview": record.get("overview", ""),
                        "msg_range": meta.get("msg_range"),
                        "recomposition_stage": meta.get("recomposition_stage"),
                        "source_uri": meta.get("source_uri"),
                    }
                )
            items.append(item)
        return items

    async def memory_index(
        self,
        context_type: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Return a lightweight index of all memories, grouped by context_type."""
        self._ensure_init()
        tid, uid = get_effective_identity()

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

        conds: List[Dict[str, Any]] = [
            {"op": "must_not", "field": "context_type", "conds": ["staging"]},
            {"op": "must", "field": "is_leaf", "conds": [True]},
            scope_filter,
        ]
        if tid:
            conds.append(
                {"op": "must", "field": "source_tenant_id", "conds": [tid, ""]}
            )

        if context_type:
            types = [t.strip() for t in context_type.split(",") if t.strip()]
            conds.append({"op": "must", "field": "context_type", "conds": types})

        project_id = get_effective_project_id()
        if project_id and project_id != "public":
            conds.append(
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

        records = await self._storage.filter(
            self._get_collection(),
            {"op": "and", "conds": conds},
            limit=limit,
            offset=0,
            order_by="created_at",
            order_desc=True,
        )

        index: Dict[str, list] = {}
        for r in records:
            abstract = r.get("abstract", "")
            if not abstract:
                continue
            ct = r.get("context_type", "memory")
            if ct not in index:
                index[ct] = []
            index[ct].append(
                {
                    "uri": r.get("uri", ""),
                    "abstract": abstract[:150],
                    "context_type": ct,
                    "category": r.get("category", ""),
                    "created_at": r.get("created_at", ""),
                }
            )

        total = sum(len(v) for v in index.values())
        return {"index": index, "total": total}

    async def list_memories_admin(
        self,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        category: Optional[str] = None,
        context_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List memories across all users (admin only). No scope isolation."""
        self._ensure_init()

        conds: List[Dict[str, Any]] = [
            {"op": "must_not", "field": "context_type", "conds": ["staging"]},
        ]
        if tenant_id:
            conds.append(
                {"op": "must", "field": "source_tenant_id", "conds": [tenant_id]}
            )
        if user_id:
            conds.append({"op": "must", "field": "source_user_id", "conds": [user_id]})
        if category:
            conds.append({"op": "must", "field": "category", "conds": [category]})
        if context_type:
            conds.append(
                {"op": "must", "field": "context_type", "conds": [context_type]}
            )

        combined: Dict[str, Any] = {"op": "and", "conds": conds}

        records = await self._storage.filter(
            self._get_collection(),
            combined,
            limit=limit,
            offset=offset,
            order_by="updated_at",
            order_desc=True,
        )

        return [
            {
                "uri": r.get("uri", ""),
                "abstract": r.get("abstract", ""),
                "category": r.get("category", ""),
                "context_type": r.get("context_type", ""),
                "scope": r.get("scope", ""),
                "project_id": r.get("project_id", ""),
                "source_tenant_id": r.get("source_tenant_id", ""),
                "source_user_id": r.get("source_user_id", ""),
                "updated_at": r.get("updated_at", ""),
                "created_at": r.get("created_at", ""),
            }
            for r in records
            if r.get("abstract")  # skip directory nodes (empty abstract)
        ]

    # =========================================================================
    # Reward-based Feedback Scoring
    # =========================================================================

    async def feedback(self, uri: str, reward: float) -> None:
        """
        Submit a reward signal for a context.

        Positive rewards reinforce retrieval; negative rewards penalize it.
        The reinforced score formula:
            reinforced_score = similarity * (1 + alpha * reward_factor) * decay_factor

        Args:
            uri: URI of the context.
            reward: Scalar reward value (positive = good, negative = bad).
        """
        self._ensure_init()

        # Find the record ID for this URI in context collection
        records = await self._storage.filter(
            self._get_collection(),
            {"op": "must", "field": "uri", "conds": [uri]},
            limit=1,
        )
        if not records:
            logger.warning("[MemoryOrchestrator] feedback: URI not found: %s", uri)
            return

        record_id = records[0].get("id", "")
        if not record_id:
            return

        # Send reward via storage adapter
        if hasattr(self._storage, "update_reward"):
            await self._storage.update_reward(self._get_collection(), record_id, reward)
            logger.info(
                "[MemoryOrchestrator] Feedback sent: uri=%s, reward=%s",
                uri,
                reward,
            )
        else:
            logger.debug(
                "[MemoryOrchestrator] Storage backend does not support rewards"
            )

        # Also update activity count
        ctx_data = records[0]
        active_count = ctx_data.get("active_count", 0)
        await self._storage.update(
            self._get_collection(),
            record_id,
            {"active_count": active_count + 1},
        )

    async def feedback_batch(self, rewards: List[Dict[str, Any]]) -> None:
        """
        Submit batch reward signals.

        Args:
            rewards: List of {"uri": str, "reward": float} dicts.
        """
        self._ensure_init()

        for item in rewards:
            await self.feedback(item["uri"], item["reward"])

    async def decay(self) -> Optional[Dict[str, Any]]:
        """
        Trigger time-decay across all records.

        Normal nodes decay at rate=0.95, protected nodes at rate=0.99.
        Records below threshold (0.01) may be archived.

        Returns:
            Decay summary dict, or None if backend doesn't support decay.
        """
        self._ensure_init()

        if hasattr(self._storage, "apply_decay"):
            result = await self._storage.apply_decay()
            logger.info("[MemoryOrchestrator] Decay applied: %s", result)
            decay_result = {
                "records_processed": result.records_processed,
                "records_decayed": result.records_decayed,
                "records_below_threshold": result.records_below_threshold,
                "records_archived": result.records_archived,
            }

            # Piggyback staging cleanup on decay
            try:
                cleaned = await self.cleanup_expired_staging()
                if cleaned:
                    decay_result["staging_cleaned"] = cleaned
            except Exception as exc:
                logger.warning("[Orchestrator] Staging cleanup failed: %s", exc)

            return decay_result
        logger.debug("[MemoryOrchestrator] Storage backend does not support decay")
        return None

    async def cleanup_expired_staging(self) -> int:
        """Delete records past their TTL (staging + immediate + any with TTL)."""
        self._ensure_init()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Scan all records with non-empty ttl_expires_at
        expired = await self._storage.filter(
            self._get_collection(),
            {"op": "must_not", "field": "ttl_expires_at", "conds": [""]},
            limit=1000,
        )
        cleaned = 0
        to_delete = []
        for record in expired:
            ttl = record.get("ttl_expires_at", "")
            if ttl and ttl < now:
                rid = record.get("id", "")
                if rid:
                    to_delete.append(rid)
                uri = record.get("uri", "")
                if uri:
                    try:
                        await self._fs.delete_temp(uri)
                    except Exception:
                        pass
                cleaned += 1
        if to_delete:
            await self._storage.delete(self._get_collection(), to_delete)
        if cleaned:
            logger.info("[Orchestrator] Cleaned %d expired records", cleaned)
        return cleaned

    async def protect(self, uri: str, protected: bool = True) -> None:
        """
        Mark a context as protected (slower decay).

        Protected memories decay at rate=0.99 instead of 0.95, preserving
        important knowledge for longer.

        Args:
            uri: URI of the context.
            protected: True to protect, False to unprotect.
        """
        self._ensure_init()

        records = await self._storage.filter(
            self._get_collection(),
            {"op": "must", "field": "uri", "conds": [uri]},
            limit=1,
        )
        if not records:
            logger.warning("[MemoryOrchestrator] protect: URI not found: %s", uri)
            return

        record_id = records[0].get("id", "")
        if hasattr(self._storage, "set_protected"):
            await self._storage.set_protected(
                self._get_collection(), record_id, protected
            )
            logger.info("[MemoryOrchestrator] Set protected=%s for: %s", protected, uri)

    async def get_profile(self, uri: str) -> Optional[Dict[str, Any]]:
        """
        Get the feedback scoring profile for a context.

        Returns:
            Profile dict with reward_score, retrieval_count, feedback counts,
            effective_score, is_protected. None if not found.
        """
        self._ensure_init()

        records = await self._storage.filter(
            self._get_collection(),
            {"op": "must", "field": "uri", "conds": [uri]},
            limit=1,
        )
        if not records:
            return None

        record_id = records[0].get("id", "")
        if hasattr(self._storage, "get_profile"):
            profile = await self._storage.get_profile(self._get_collection(), record_id)
            if profile:
                return {
                    "id": profile.id,
                    "reward_score": profile.reward_score,
                    "retrieval_count": profile.retrieval_count,
                    "positive_feedback_count": profile.positive_feedback_count,
                    "negative_feedback_count": profile.negative_feedback_count,
                    "effective_score": profile.effective_score,
                    "is_protected": profile.is_protected,
                }
        return None

    # =========================================================================
    # System Status
    # =========================================================================

    async def system_status(self, status_type: str = "doctor") -> Dict[str, Any]:
        """Unified system status endpoint.

        Args:
            status_type: "health" | "stats" | "doctor"
        """
        if status_type == "health":
            return await self.health_check()
        elif status_type == "stats":
            return await self.stats()
        else:  # doctor
            health = await self.health_check()
            st = await self.stats()
            issues = []
            if not health.get("storage"):
                issues.append("Storage unavailable")
            if not health.get("embedder"):
                issues.append("Embedder unavailable")
            if not health.get("llm"):
                issues.append(
                    "No LLM configured — intent analysis and session extraction disabled"
                )
            return {**health, **st, "issues": issues}

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
                            asyncio.create_task(self._run_archivist(tid, uid))

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
        """Run Archivist in background to extract knowledge from traces.

        Safety invariants:
        - Only traces whose derived knowledge ALL saved successfully
          are marked processed. Failed traces remain unprocessed for retry.
        - If archivist.run() returns [] (e.g. concurrent run already active),
          no traces are marked processed.
        - Per-knowledge errors are isolated — one failure doesn't block others.
        """
        stats: Dict[str, int] = {"knowledge_candidates": 0, "knowledge_active": 0}
        if not self._archivist or not self._trace_store or not self._knowledge_store:
            return stats
        try:
            from opencortex.alpha.types import KnowledgeScope, KnowledgeStatus
            from opencortex.alpha.sandbox import evaluate as sandbox_evaluate

            traces = await self._trace_store.list_unprocessed(tenant_id)
            if not traces:
                return stats

            knowledge_items = await self._archivist.run(
                traces, tenant_id, user_id, KnowledgeScope.USER,
            )

            # Guard: if archivist returned nothing (concurrent run or no
            # patterns found), do NOT mark traces — leave for retry.
            if not knowledge_items:
                return stats

            alpha_cfg = self._config.cortex_alpha
            succeeded_trace_ids: set = set()
            failed_trace_ids: set = set()

            for k in knowledge_items:
                source_ids = set(k.source_trace_ids) if k.source_trace_ids else set()
                try:
                    evidence_traces = [
                        t for t in traces
                        if t.get("trace_id", t.get("id", "")) in source_ids
                    ]

                    # Run Sandbox evaluation
                    if evidence_traces and self._llm_completion:
                        eval_result = await sandbox_evaluate(
                            knowledge_dict=k.to_dict(),
                            traces=evidence_traces,
                            llm_fn=self._llm_completion,
                            min_traces=alpha_cfg.sandbox_min_traces,
                            min_success_rate=alpha_cfg.sandbox_min_success_rate,
                            min_source_users=alpha_cfg.sandbox_min_source_users,
                            min_source_users_private=alpha_cfg.sandbox_min_source_users_private,
                            llm_sample_size=alpha_cfg.sandbox_llm_sample_size,
                            llm_min_pass_rate=alpha_cfg.sandbox_llm_min_pass_rate,
                            require_human_approval=alpha_cfg.sandbox_require_human_approval,
                            user_auto_approve_confidence=alpha_cfg.user_auto_approve_confidence,
                        )
                        status_map = {
                            "needs_more_traces": KnowledgeStatus.CANDIDATE,
                            "needs_improvement": KnowledgeStatus.CANDIDATE,
                            "verified": KnowledgeStatus.VERIFIED,
                            "active": KnowledgeStatus.ACTIVE,
                        }
                        k.status = status_map.get(eval_result.status, KnowledgeStatus.CANDIDATE)

                    await self._knowledge_store.save(k)
                    succeeded_trace_ids.update(source_ids)

                    if k.status == KnowledgeStatus.ACTIVE:
                        stats["knowledge_active"] += 1
                    else:
                        stats["knowledge_candidates"] += 1
                except Exception as exc:
                    failed_trace_ids.update(source_ids)
                    logger.warning(
                        "[Alpha] Sandbox/save failed for knowledge %s: %s",
                        k.knowledge_id, exc,
                    )

            # Only mark traces whose knowledge all saved successfully.
            # Traces linked to failed knowledge stay unprocessed for retry.
            safe_ids = succeeded_trace_ids - failed_trace_ids
            if safe_ids:
                await self._trace_store.mark_processed(list(safe_ids))

            logger.info(
                "[Alpha] Archivist: %d candidates, %d active from %d traces "
                "(%d traces marked processed, %d retained for retry)",
                stats["knowledge_candidates"], stats["knowledge_active"],
                len(traces), len(safe_ids), len(failed_trace_ids),
            )
        except Exception as exc:
            logger.warning("[Alpha] Archivist failed: %s", exc)
        return stats

    # =========================================================================
    # Cortex Alpha: Knowledge API
    # =========================================================================

    async def knowledge_search(
        self,
        query: str,
        types: Optional[List[str]] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """Search the Knowledge Store."""
        self._ensure_init()
        if not self._knowledge_store:
            return {"results": [], "error": "Knowledge store not initialized"}
        tid, uid = get_effective_identity()
        results = await self._knowledge_store.search(
            query,
            tid,
            uid,
            types=types,
            limit=limit,
        )
        return {"results": results, "count": len(results)}

    async def knowledge_approve(self, knowledge_id: str) -> Dict[str, Any]:
        """Approve a knowledge candidate (move to active)."""
        self._ensure_init()
        if not self._knowledge_store:
            return {"ok": False, "error": "Knowledge store not initialized"}
        ok = await self._knowledge_store.approve(knowledge_id)
        return {
            "ok": ok,
            "knowledge_id": knowledge_id,
            "status": "active" if ok else "not_found",
        }

    async def knowledge_reject(self, knowledge_id: str) -> Dict[str, Any]:
        """Reject a knowledge candidate (deprecate)."""
        self._ensure_init()
        if not self._knowledge_store:
            return {"ok": False, "error": "Knowledge store not initialized"}
        ok = await self._knowledge_store.reject(knowledge_id)
        return {
            "ok": ok,
            "knowledge_id": knowledge_id,
            "status": "deprecated" if ok else "not_found",
        }

    async def knowledge_list_candidates(self) -> Dict[str, Any]:
        """List knowledge candidates pending approval."""
        self._ensure_init()
        if not self._knowledge_store:
            return {"candidates": [], "error": "Knowledge store not initialized"}
        tid, uid = get_effective_identity()
        candidates = await self._knowledge_store.list_candidates(tid, uid)
        return {"candidates": candidates, "count": len(candidates)}

    async def archivist_trigger(self) -> Dict[str, Any]:
        """Manually trigger the Archivist."""
        self._ensure_init()
        if not self._archivist:
            return {"ok": False, "error": "Archivist not initialized"}
        tid, uid = get_effective_identity()
        asyncio.create_task(self._run_archivist(tid, uid))
        return {"ok": True, "status": "triggered"}

    async def archivist_status(self) -> Dict[str, Any]:
        """Get Archivist status."""
        if not self._archivist:
            return {"enabled": False}
        return {"enabled": True, **self._archivist.status}

    # =========================================================================
    # Batch Import
    # =========================================================================

    async def batch_add(
        self,
        items: List[Dict[str, Any]],
        source_path: str = "",
        scan_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Batch add documents. LLM generates abstract + overview per item.

        When scan_meta is present, builds directory hierarchy from
        meta.file_path values.
        """
        self._ensure_init()

        imported = 0
        errors = []
        uris = []

        # Hierarchical tree building when scan_meta present
        dir_uris: Dict[str, str] = {}
        if scan_meta:
            from pathlib import PurePosixPath

            # Collect unique directories
            all_dirs: set = set()
            for item in items:
                fp = (item.get("meta") or {}).get("file_path", "")
                if fp:
                    parts = PurePosixPath(fp).parts
                    for j in range(1, len(parts)):
                        all_dirs.add("/".join(parts[:j]))

            # Create directory nodes bottom-up (sorted by depth)
            for d in sorted(all_dirs, key=lambda x: x.count("/")):
                parent_dir = str(PurePosixPath(d).parent)
                parent_uri = dir_uris.get(parent_dir) if parent_dir != "." else None
                try:
                    dir_ctx = await self.add(
                        abstract=PurePosixPath(d).name,
                        content="",
                        category="documents",
                        parent_uri=parent_uri,
                        is_leaf=False,
                        context_type="resource",
                        meta={
                            "source": "batch:scan",
                            "dir_path": d,
                            "ingest_mode": "memory",
                        },
                        dedup=False,
                    )
                    dir_uris[d] = dir_ctx.uri
                    uris.append(dir_ctx.uri)
                except Exception as exc:
                    logger.warning("[batch_add] Dir node failed for %s: %s", d, exc)

        sem = asyncio.Semaphore(_BATCH_ADD_CONCURRENCY)

        async def _process_one(i: int, item: dict) -> dict:
            async with sem:
                content = item.get("content", "")
                file_path = (item.get("meta") or {}).get("file_path", f"item_{i}")
                abstract, overview = await self._generate_abstract_overview(
                    content, file_path
                )

                item_meta = dict(item.get("meta") or {})
                item_meta.setdefault("source", "batch:scan")
                item_meta["ingest_mode"] = "memory"

                parent_uri = None
                if scan_meta and file_path:
                    from pathlib import PurePosixPath

                    parent_dir = str(PurePosixPath(file_path).parent)
                    parent_uri = dir_uris.get(parent_dir)

                embed_text = ""
                if self._config.context_flattening_enabled:
                    fp = item_meta.get("file_path", "")
                    if fp:
                        embed_text = f"[{fp}] {abstract}"

                try:
                    result = await self.add(
                        abstract=abstract,
                        content=content,
                        overview=overview,
                        category=item.get("category", "documents"),
                        parent_uri=parent_uri,
                        context_type=item.get("context_type", "resource"),
                        meta=item_meta,
                        dedup=False,
                        embed_text=embed_text,
                    )
                    return {"uri": result.uri, "index": i}
                except Exception as exc:
                    return {"error": str(exc), "index": i}

        outcomes = await asyncio.gather(
            *[_process_one(i, item) for i, item in enumerate(items)],
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                errors.append({"error": str(outcome)})
            elif isinstance(outcome, dict) and "error" in outcome:
                errors.append({"index": outcome["index"], "error": outcome["error"]})
            else:
                uris.append(outcome["uri"])
                imported += 1

        has_git = (scan_meta or {}).get("has_git", False)
        project_id = (scan_meta or {}).get("project_id", "public")

        return {
            "status": "ok" if not errors else "partial",
            "total": len(items),
            "imported": imported,
            "errors": errors,
            "has_git_project": has_git and project_id != "public",
            "project_id": project_id,
            "uris": uris,
        }

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
        startup_task = getattr(self, "_autophagy_startup_sweep_task", None)
        if startup_task is not None and not startup_task.done():
            startup_task.cancel()
        periodic_task = getattr(self, "_autophagy_sweep_task", None)
        if periodic_task is not None and not periodic_task.done():
            periodic_task.cancel()
        for task in (startup_task, periodic_task):
            if task is None:
                continue
            with suppress(asyncio.CancelledError):
                await task
        self._autophagy_startup_sweep_task = None
        self._autophagy_sweep_task = None
        recall_tasks = list(self._recall_bookkeeping_tasks_set())
        for task in recall_tasks:
            if not task.done():
                task.cancel()
        for task in recall_tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._recall_bookkeeping_tasks_set().clear()

        if self._derive_worker_task and not self._derive_worker_task.done():
            await self._derive_queue.put(None)
            try:
                await asyncio.wait_for(self._derive_worker_task, timeout=30.0)
            except asyncio.TimeoutError:
                self._derive_worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._derive_worker_task

        if self._context_manager:
            await self._context_manager.close()
        if self._storage:
            await self._storage.close()
        self._initialized = False
        logger.info("[MemoryOrchestrator] Closed")

    async def health_check(self) -> Dict[str, Any]:
        """
        Check health of all components.

        Returns:
            Dict with component health status.
        """
        result = {
            "initialized": self._initialized,
            "storage": False,
            "embedder": self._embedder is not None,
            "llm": self._llm_completion is not None,
        }
        if self._initialized and self._storage:
            result["storage"] = await self._storage.health_check()
        return result

    async def stats(self) -> Dict[str, Any]:
        """
        Get orchestrator statistics.

        Returns:
            Dict with storage stats, config info, and component status.
        """
        self._ensure_init()

        storage_stats = await self._storage.get_stats()
        rerank_info = {
            "enabled": False,
            "mode": "disabled",
            "model": None,
            "fusion_beta": 0.0,
        }
        rerank_cfg = self._build_rerank_config()
        if rerank_cfg.is_available():
            rerank_info = {
                "enabled": True,
                "mode": rerank_cfg.provider,
                "model": self._config.rerank_model or None,
                "fusion_beta": rerank_cfg.fusion_beta,
            }
        tid, uid = get_effective_identity()
        return {
            "tenant_id": tid,
            "user_id": uid,
            "storage": storage_stats,
            "embedder": self._embedder.model_name if self._embedder else None,
            "has_llm": self._llm_completion is not None,
            "rerank": rerank_info,
        }

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

        elif context_type == "case":
            return CortexURI.build_shared(tid, "shared", "cases", node_name)

        elif context_type == "pattern":
            return CortexURI.build_shared(tid, "shared", "patterns", node_name)

        elif context_type == "resource":
            project = get_effective_project_id()  # e.g. "OpenCortex" or "public"
            if category:
                return CortexURI.build_shared(
                    tid, "resources", project, category, node_name
                )
            return CortexURI.build_shared(tid, "resources", project, node_name)

        elif context_type == "staging":
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

    def _infer_context_type(self, uri: str) -> ContextType:
        """Infer ContextType from URI path segments."""
        if "/staging/" in uri:
            return ContextType.STAGING
        elif "/memories/" in uri:
            return ContextType.MEMORY
        elif "/shared/cases/" in uri:
            return ContextType.CASE
        elif "/shared/patterns/" in uri:
            return ContextType.PATTERN
        elif "/skills/" in uri:
            return ContextType.SKILL
        return ContextType.RESOURCE

    async def _ensure_parent_records(self, parent_uri: str) -> None:
        """
        Ensure all ancestor directory records exist in the vector store.

        Retrieval still relies on parent_uri lineage for scoped navigation and
        document hierarchy. For leaves to remain discoverable under those
        scopes, every intermediate directory must have a vector-store record
        (`is_leaf=False`).

        This walks upward from parent_uri to the tenant root, creating any
        missing directory records along the way.
        """
        uri = parent_uri
        to_create = []

        # Walk up the URI tree, collecting missing directories
        while uri:
            try:
                parsed = CortexURI(uri)
            except ValueError:
                break

            # Check if this directory record already exists
            existing = await self._storage.filter(
                self._get_collection(),
                {"op": "must", "field": "uri", "conds": [uri]},
                limit=1,
            )
            if existing:
                break  # This level and all above already exist

            to_create.append(uri)

            parent = parsed.parent
            if parent is None:
                break
            uri = str(parent)

        # Build effective user identity (per-request or config default)
        tid, uid = get_effective_identity()
        effective_user = UserIdentifier(tid, uid)

        # Create directory records from top down (so parent_uri links are valid)
        for dir_uri in reversed(to_create):
            dir_parent = self._derive_parent_uri(dir_uri)
            dir_ctx = Context(
                uri=dir_uri,
                parent_uri=dir_parent,
                is_leaf=False,
                abstract="",
                user=effective_user,
            )

            # Embed the directory name as a minimal vector
            dir_name = dir_uri.rstrip("/").rsplit("/", 1)[-1]
            embed_result = None
            if self._embedder and dir_name:
                loop = asyncio.get_event_loop()
                embed_result = await loop.run_in_executor(
                    None, self._embedder.embed, dir_name
                )
                dir_ctx.vector = embed_result.dense_vector

            record = dir_ctx.to_dict()
            if dir_ctx.vector:
                record["vector"] = dir_ctx.vector
            if embed_result and embed_result.sparse_vector:
                record["sparse_vector"] = embed_result.sparse_vector
            # Populate scope fields so directory records pass scope filters
            record["scope"] = "private" if CortexURI(dir_uri).is_private else "shared"
            record["source_user_id"] = uid
            record["source_tenant_id"] = tid
            record["category"] = ""
            record["mergeable"] = False
            record["session_id"] = ""
            record["ttl_expires_at"] = ""
            await self._storage.upsert(self._get_collection(), record)
            logger.debug("[MemoryOrchestrator] Created directory record: %s", dir_uri)

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
        """
        Get memory statistics for a user (admin/insights use).

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
