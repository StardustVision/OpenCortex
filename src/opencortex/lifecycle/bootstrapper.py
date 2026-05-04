# SPDX-License-Identifier: Apache-2.0
"""Subsystem boot sequencing service extracted from CortexMemory.

The 11-step ``init()`` boot sequence and its helper methods have been
extracted from ``CortexMemory`` as part of plan 015 (Phase 5 of
the God Object decomposition). This module owns the creation and wiring
of every subsystem that CortexMemory depends on.

Boundary
--------
``SubsystemBootstrapper`` is responsible for:
- The full ``init()`` boot sequence (storage, embedder, CortexFS,
  collections, intent analyzer, cone retrieval, memory probe,
  background maintenance, cognition, alpha pipeline, skill engine)
- Helper methods: ``_init_cognition``, ``_init_alpha``,
  ``_init_skill_engine``, ``_create_default_embedder``,
  ``_create_local_embedder``, ``_startup_maintenance``,
  ``_check_and_reembed``

It is explicitly NOT responsible for:
- Memory record CRUD — owned by ``MemoryService``
- Knowledge lifecycle — owned by ``KnowledgeService``
- Background task lifecycle — owned by ``BackgroundTaskManager``
- System status reporting — owned by ``SystemStatusService``
- Embedder wrapping and rerank runtime helpers — owned by
  ``ModelRuntimeService`` and exposed through memory facade compatibility
  wrappers such as ``self._orch._wrap_with_*()``.
- Retrieval-time helpers (``_build_probe_filter``, etc.) — stay on
  CortexMemory

Design
------
The service holds a back-reference to CortexMemory (``self._orch``)
and creates/wires subsystems by assigning to ``self._orch._X`` attributes
at boot time. All subsystem attributes remain on CortexMemory for
admin route compatibility. Construction is sync and cheap — no I/O, no
model loading. The CortexMemory service registry lazily builds a single
``SubsystemBootstrapper`` instance via the ``_bootstrapper`` property.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencortex.cognition.state_types import OwnerType

if TYPE_CHECKING:
    from opencortex.cortex_memory import CortexMemory

logger = logging.getLogger(__name__)


class SubsystemBootstrapper:
    """Subsystem creation and wiring for CortexMemory.

    Owns the 11-step boot sequence that creates storage, embedder,
    CortexFS, intent analyzer, cognition components, alpha pipeline,
    skill engine, and all supporting subsystems. CortexMemory's
    ``init()`` delegates to ``SubsystemBootstrapper.init()``.

    Args:
        orchestrator: The parent CortexMemory instance.
            Subsystems are assigned as attributes on this object.
    """

    def __init__(self, orchestrator: CortexMemory) -> None:
        self._orch = orchestrator

    async def init(self) -> "CortexMemory":
        """Run the full 11-step subsystem boot sequence.

        Creates storage, embedder, CortexFS, intent analyzer, cognition
        components, alpha pipeline, skill engine, and all supporting
        subsystems. Assigns them as attributes on CortexMemory.

        Returns:
            The parent memory facade instance (for chaining).
        """
        orch = self._orch
        if orch._initialized:
            return orch

        # 1. Storage backend (auto-create QdrantStorageAdapter if not provided)
        if orch._storage is None:
            from opencortex.storage.qdrant import QdrantStorageAdapter

            db_path = str(Path(orch._config.data_root) / ".qdrant")
            qdrant_url = getattr(orch._config, "qdrant_url", "") or ""
            orch._storage = QdrantStorageAdapter(
                path=db_path,
                embedding_dim=orch._config.embedding_dimension,
                url=qdrant_url,
            )
            logger.info(
                "[SubsystemBootstrapper] Auto-created QdrantStorageAdapter at %s",
                qdrant_url or db_path,
            )

        # 1b. Embedder auto-creation
        if orch._embedder is None:
            orch._embedder = self._create_default_embedder()

        # 2. User identity (default; overridden per-request via HTTP headers)
        from opencortex.core.user_id import UserIdentifier

        orch._user = UserIdentifier("default", "default")

        # 3. CortexFS
        from opencortex.storage.cortex_fs import init_cortex_fs

        orch._fs = init_cortex_fs(
            data_root=orch._config.data_root,
            query_embedder=orch._embedder,
            rerank_config=orch._rerank_config,
            vector_store=orch._storage,
        )

        # 4. Create context collection if needed
        from opencortex.storage.collection_schemas import (
            init_context_collection,
        )

        await init_context_collection(
            orch._storage,
            orch._get_collection(),
            orch._config.embedding_dimension,
        )

        # 5. Intent analyzer: use provided callable or auto-create
        if orch._llm_completion is None:
            try:
                from opencortex.models.llm_factory import (
                    create_llm_completion,
                )

                orch._llm_completion = create_llm_completion(
                    orch._config,
                )
            except Exception as exc:
                logger.warning(
                    "[SubsystemBootstrapper] Could not create LLM "
                    "completion from config: %s",
                    exc,
                )

        # Plan 009 — RerankClient is created lazily by
        # ``_get_or_create_rerank_client()`` on first use. Eager
        # construction would trigger ``_init_local_reranker`` (fastembed
        # model download) for every orchestrator init, including in
        # tests that never need it. Lazy keeps the singleton invariant
        # (one instance per process across all admin requests) without
        # imposing the cold-start cost. ``CortexMemory.close()``
        # checks for ``None`` before invoking ``aclose`` (U3).

        # 6. Cone Retrieval: entity index + scorer
        if orch._config.cone_retrieval_enabled:
            from opencortex.retrieve.cone_scorer import ConeScorer
            from opencortex.retrieve.entity_index import EntityIndex

            orch._entity_index = EntityIndex()
            orch._cone_scorer = ConeScorer(orch._entity_index)
            asyncio.create_task(
                orch._entity_index.build_for_collection(
                    orch._storage, orch._get_collection()
                )
            )

        # 7. Memory bootstrap probe
        from opencortex.intent import MemoryBootstrapProbe

        orch._memory_probe = MemoryBootstrapProbe(
            storage=orch._storage,
            embedder=orch._embedder,
            collection_resolver=orch._get_collection,
            filter_builder=orch._build_probe_filter,
            top_k=6,
        )

        # 8. Background maintenance: text indexes, migrations, re-embed
        asyncio.create_task(self._startup_maintenance())

        # 8b. Document derive worker
        orch._start_derive_worker()

        # 9. Autophagy cognition plugin components
        if getattr(orch._config, "cognition_enabled", True) and getattr(
            orch._config,
            "autophagy_plugin_enabled",
            False,
        ):
            await self._init_cognition()
            self._register_autophagy_signal_handlers()
            orch._start_autophagy_sweeper()

        # 9b. Plan 009 / R5 — periodic sweeper that watches the
        # pooled httpx clients (LLM + rerank) for the leak shape
        # that caused the original CLOSE_WAIT incident.
        orch._start_connection_sweeper()

        # 10. Cortex Alpha components
        await self._init_alpha()

        # 11. Skill Engine
        if getattr(orch._config, "skill_engine_enabled", False):
            await self._init_skill_engine()

        orch._initialized = True
        logger.info(
            "[SubsystemBootstrapper] Initialized (data_root=%s)",
            orch._config.data_root,
        )
        return orch

    async def _init_cognition(self) -> None:
        """Initialize cognition-layer stores/controllers/kernel."""
        orch = self._orch
        if not orch._storage:
            return

        from opencortex.cognition import (
            AutophagyKernel,
            CandidateStore,
            CognitiveMetabolismController,
            CognitiveStateStore,
            ConsolidationGate,
            RecallMutationEngine,
        )

        orch._cognitive_state_store = CognitiveStateStore(orch._storage)
        await orch._cognitive_state_store.init()

        orch._candidate_store = CandidateStore(orch._storage)
        await orch._candidate_store.init()

        orch._recall_mutation_engine = RecallMutationEngine()
        orch._consolidation_gate = ConsolidationGate(
            candidate_store=orch._candidate_store,
        )
        orch._cognitive_metabolism_controller = CognitiveMetabolismController()
        orch._autophagy_kernel = AutophagyKernel(
            state_store=orch._cognitive_state_store,
            mutation_engine=orch._recall_mutation_engine,
            consolidation_gate=orch._consolidation_gate,
            candidate_store=orch._candidate_store,
            metabolism_controller=(orch._cognitive_metabolism_controller),
        )

    def _register_autophagy_signal_handlers(self) -> None:
        """Subscribe autophagy plugin handlers to memory lifecycle signals."""
        orch = self._orch
        signal_bus = getattr(orch, "_memory_signal_bus", None)
        if signal_bus is None or orch._autophagy_kernel is None:
            return

        async def on_memory_stored(signal: Any) -> None:
            if signal.context_type != "memory":
                return
            await orch._initialize_autophagy_owner_state(
                owner_type=OwnerType.MEMORY,
                owner_id=signal.record_id,
                tenant_id=signal.tenant_id,
                user_id=signal.user_id,
                project_id=signal.project_id,
            )

        async def on_recall_completed(signal: Any) -> None:
            recalled_owner_ids = await orch._resolve_memory_owner_ids(signal.memories)
            if not recalled_owner_ids or orch._autophagy_kernel is None:
                return
            await orch._autophagy_kernel.apply_recall_outcome(
                owner_ids=recalled_owner_ids,
                query=signal.query,
                recall_outcome={"selected_results": recalled_owner_ids},
            )

        signal_bus.subscribe("memory_stored", on_memory_stored)
        signal_bus.subscribe("recall_completed", on_recall_completed)

    async def _init_alpha(self) -> None:
        """Initialize Cortex Alpha components if enabled."""
        orch = self._orch
        alpha_cfg = orch._config.cortex_alpha

        # Observer — always initialized (lightweight in-memory)
        from opencortex.alpha.observer import Observer

        orch._observer = Observer()

        if orch._storage and orch._embedder and alpha_cfg.trace_splitter_enabled:
            from opencortex.alpha.trace_store import TraceStore

            orch._trace_store = TraceStore(
                storage=orch._storage,
                embedder=orch._embedder,
                cortex_fs=orch._fs,
                collection_name=alpha_cfg.trace_collection_name,
                embedding_dim=orch._config.embedding_dimension,
                on_trace_saved=(
                    orch._on_trace_saved if orch._autophagy_kernel else None
                ),
            )
            await orch._trace_store.init()

        if orch._storage and orch._embedder and alpha_cfg.archivist_enabled:
            from opencortex.alpha.knowledge_store import KnowledgeStore

            orch._knowledge_store = KnowledgeStore(
                storage=orch._storage,
                embedder=orch._embedder,
                cortex_fs=orch._fs,
                collection_name=alpha_cfg.knowledge_collection_name,
                embedding_dim=orch._config.embedding_dimension,
            )
            await orch._knowledge_store.init()

        # TraceSplitter (needs LLM)
        if orch._llm_completion and alpha_cfg.trace_splitter_enabled:
            from opencortex.alpha.trace_splitter import TraceSplitter

            orch._trace_splitter = TraceSplitter(
                llm_fn=orch._llm_completion,
                max_context_tokens=(alpha_cfg.trace_splitter_max_context_tokens),
            )

        # Archivist (needs LLM)
        if orch._llm_completion and alpha_cfg.archivist_enabled:
            from opencortex.alpha.archivist import Archivist

            orch._archivist = Archivist(
                llm_fn=orch._llm_completion,
                embedder=orch._embedder,
                trigger_threshold=alpha_cfg.archivist_trigger_threshold,
                trigger_mode=alpha_cfg.archivist_trigger_mode,
            )

        # ContextManager — three-phase lifecycle
        from opencortex.context import ContextManager

        orch._context_manager = ContextManager(
            orchestrator=orch,
            observer=orch._observer,
        )
        await orch._context_manager.start()

        logger.info("[SubsystemBootstrapper] Cortex Alpha initialized")

    async def _init_skill_engine(self) -> None:
        """Initialize Skill Engine if storage and embedder are available."""
        orch = self._orch
        if not orch._storage or not orch._embedder:
            return
        try:
            from opencortex.skill_engine.adapters.llm_adapter import (
                LLMCompletionAdapter,
            )
            from opencortex.skill_engine.adapters.storage_adapter import (
                SkillStorageAdapter,
            )
            from opencortex.skill_engine.http_routes import (
                set_skill_manager,
            )
            from opencortex.skill_engine.skill_manager import (
                SkillManager,
            )
            from opencortex.skill_engine.store import SkillStore

            storage_adapter = SkillStorageAdapter(
                storage=orch._storage,
                embedder=orch._embedder,
                embedding_dim=orch._config.embedding_dimension,
            )
            await storage_adapter.initialize()
            store = SkillStore(storage_adapter)

            analyzer = None
            evolver = None
            llm_adapter = None

            if orch._llm_completion:
                llm_adapter = LLMCompletionAdapter(orch._llm_completion)

                from opencortex.skill_engine.evolver import (
                    SkillEvolver,
                )

                evolver = SkillEvolver(llm=llm_adapter, store=store)

                # SourceAdapter + Analyzer (for extraction pipeline)
                from opencortex.skill_engine.adapters.source_adapter import (
                    QdrantSourceAdapter,
                )
                from opencortex.skill_engine.analyzer import (
                    SkillAnalyzer,
                )

                source_adapter = QdrantSourceAdapter(
                    storage=orch._storage,
                    embedder=orch._embedder,
                )
                analyzer = SkillAnalyzer(
                    source=source_adapter,
                    llm=llm_adapter,
                    store=store,
                )

            # Quality Gate (Phase A)
            quality_gate = None
            if llm_adapter:
                from opencortex.skill_engine.quality_gate import (
                    QualityGate,
                )

                quality_gate = QualityGate(llm=llm_adapter)

            # Sandbox TDD (Phase B — default OFF)
            sandbox_tdd = None
            if orch._config.cortex_alpha.sandbox_tdd_enabled and llm_adapter:
                from opencortex.skill_engine.sandbox_tdd import (
                    SandboxTDD,
                )

                sandbox_tdd = SandboxTDD(
                    llm=llm_adapter,
                    max_llm_calls=(orch._config.cortex_alpha.sandbox_tdd_max_llm_calls),
                )

            orch._skill_manager = SkillManager(
                store=store,
                analyzer=analyzer,
                evolver=evolver,
                quality_gate=quality_gate,
                sandbox_tdd=sandbox_tdd,
            )
            set_skill_manager(orch._skill_manager)

            # SkillEventStore + Evaluator (Phase C)
            from opencortex.skill_engine.evaluator import (
                SkillEvaluator,
            )
            from opencortex.skill_engine.event_store import (
                SkillEventStore,
            )

            orch._skill_event_store = SkillEventStore(
                storage=orch._storage,
            )
            await orch._skill_event_store.init()

            orch._skill_evaluator = SkillEvaluator(
                event_store=orch._skill_event_store,
                skill_store=store,
                trace_store=orch._trace_store,
                skill_storage=storage_adapter,
                llm=llm_adapter,
            )

            # Startup sweeper for crash recovery
            if orch._skill_evaluator:
                asyncio.create_task(orch._skill_evaluator.sweep_unevaluated())

            logger.info("[SubsystemBootstrapper] Skill Engine initialized")
        except Exception as exc:
            logger.info(
                "[SubsystemBootstrapper] Skill Engine not available: %s",
                exc,
            )

    def _create_default_embedder(self) -> Any:
        """Auto-create an embedder based on CortexConfig.

        Resolution order:
        1. ``embedding_provider == "local"`` → LocalEmbedder (FastEmbed).
        2. ``embedding_provider == "openai"`` → OpenAIDenseEmbedder.
        3. No provider or unknown → ``None`` (filter/scroll search only).

        Returns:
            An embedder instance, or ``None`` if creation fails.
        """
        import os

        orch = self._orch
        provider = (orch._config.embedding_provider or "").strip().lower()

        # Explicitly disabled
        if provider in ("none", "disabled", "off"):
            logger.info(
                "[SubsystemBootstrapper] embedding_provider='%s' — "
                "running without embedder (filter/scroll search only).",
                provider,
            )
            return None

        if provider == "local":
            return self._create_local_embedder()

        if provider == "volcengine":
            logger.warning(
                "[SubsystemBootstrapper] embedding_provider='volcengine' "
                "is deprecated. Use 'openai' with the same API key/base."
            )
            return None

        if provider == "openai":
            try:
                from opencortex.models.embedder.openai_embedder import (
                    OpenAIDenseEmbedder,
                )

                api_key = orch._config.embedding_api_key or os.environ.get(
                    "OPENCORTEX_EMBEDDING_API_KEY", ""
                )
                if not api_key:
                    logger.warning(
                        "[SubsystemBootstrapper] "
                        "embedding_provider='openai' but no api_key "
                        "found. Skipping auto-embedder creation."
                    )
                    return None

                model_name = orch._config.embedding_model
                if not model_name:
                    logger.warning(
                        "[SubsystemBootstrapper] "
                        "embedding_provider='openai' but "
                        "embedding_model not set. Skipping."
                    )
                    return None

                embedder = OpenAIDenseEmbedder(
                    model_name=model_name,
                    api_key=api_key,
                    api_base=(
                        orch._config.embedding_api_base or "https://api.openai.com/v1"
                    ),
                    dimension=(orch._config.embedding_dimension or None),
                )
                logger.info(
                    "[SubsystemBootstrapper] Auto-created "
                    "OpenAIDenseEmbedder (model=%s)",
                    model_name,
                )
                return orch._wrap_with_cache(orch._wrap_with_hybrid(embedder))
            except ImportError as exc:
                logger.warning(
                    "[SubsystemBootstrapper] Cannot create OpenAI "
                    "embedder — httpx not installed: %s",
                    exc,
                )
                return None
            except Exception as exc:
                logger.warning(
                    "[SubsystemBootstrapper] Failed to create OpenAI embedder: %s",
                    exc,
                )
                return None

        # No provider configured
        if not provider:
            logger.info(
                "[SubsystemBootstrapper] No embedding_provider "
                "configured. Running without embedder "
                "(filter/scroll search only)."
            )
            return None

        # Unknown / unsupported provider
        logger.warning(
            "[SubsystemBootstrapper] Unknown embedding_provider='%s'. "
            "No embedder will be auto-created.",
            provider,
        )
        return None

    def _create_local_embedder(self) -> Any:
        """Create a local FastEmbed embedder with BM25 sparse + LRU cache.

        Returns:
            An embedder instance, or ``None`` if creation fails.
        """
        orch = self._orch
        try:
            from opencortex.models.embedder.local_embedder import (
                DEFAULT_LOCAL_EMBEDDING_MODEL,
                LocalEmbedder,
            )

            model_name = orch._config.embedding_model or DEFAULT_LOCAL_EMBEDDING_MODEL
            local_config = {
                "onnx_intra_op_threads": (orch._config.onnx_intra_op_threads),
            }
            embedder = LocalEmbedder(
                model_name=model_name,
                config=local_config,
            )
            if not embedder.is_available:
                logger.warning(
                    "[SubsystemBootstrapper] LocalEmbedder failed to "
                    "load '%s'. Install with: uv add fastembed",
                    model_name,
                )
                return None

            # Update dimension from detected model
            detected_dim = embedder.get_dimension()
            if detected_dim and detected_dim != orch._config.embedding_dimension:
                logger.info(
                    "[SubsystemBootstrapper] Updating "
                    "embedding_dimension %d → %d from local model",
                    orch._config.embedding_dimension,
                    detected_dim,
                )
                orch._config.embedding_dimension = detected_dim

            logger.info(
                "[SubsystemBootstrapper] Auto-created LocalEmbedder (model=%s, dim=%d)",
                model_name,
                detected_dim,
            )

            return orch._wrap_with_cache(orch._wrap_with_hybrid(embedder))

        except ImportError as exc:
            logger.warning(
                "[SubsystemBootstrapper] Cannot create local embedder "
                "— fastembed not installed: %s",
                exc,
            )
            return None
        except Exception as exc:
            logger.warning(
                "[SubsystemBootstrapper] Failed to create local embedder: %s",
                exc,
            )
            return None

    async def _startup_maintenance(self) -> None:
        """Background: text indexes, migrations, re-embed, recovery.

        Runs as fire-and-forget after ``init()`` returns.
        """
        orch = self._orch
        if hasattr(orch._storage, "ensure_text_indexes"):
            try:
                await orch._storage.ensure_text_indexes()
            except Exception as exc:
                logger.warning(
                    "[SubsystemBootstrapper] Text index setup failed: %s",
                    exc,
                )

        try:
            from opencortex.migration.v030_path_redesign import (
                backfill_new_fields,
                cleanup_root_junk,
            )

            await cleanup_root_junk(orch._storage, orch._fs, orch._get_collection())
            await backfill_new_fields(orch._storage, orch._get_collection())
        except Exception as exc:
            logger.warning(
                "[SubsystemBootstrapper] Migration v0.3 skipped: %s",
                exc,
            )

        try:
            from opencortex.migration.v040_project_backfill import (
                backfill_project_id,
            )

            await backfill_project_id(orch._storage, orch._get_collection())
        except Exception as exc:
            logger.warning(
                "[SubsystemBootstrapper] Migration v0.4 skipped: %s",
                exc,
            )

        try:
            await self._check_and_reembed()
        except Exception as exc:
            logger.warning(
                "[SubsystemBootstrapper] Auto re-embed skipped: %s",
                exc,
            )

        await orch._recover_pending_derives()

    async def _check_and_reembed(self) -> None:
        """Auto re-embed if the embedding model has changed since last run."""
        orch = self._orch
        current_model = getattr(orch._embedder, "model_name", "")
        if not current_model or not orch._embedder:
            return

        marker = Path(orch._config.data_root) / ".embedding_model"
        previous_model = marker.read_text().strip() if marker.exists() else ""

        if previous_model == current_model:
            return

        # Only re-embed if there are existing records
        try:
            count = await orch._storage.count(orch._get_collection())
        except Exception:
            count = 0

        if count == 0:
            # No data yet — just write the marker
            marker.write_text(current_model)
            return

        logger.info(
            "[SubsystemBootstrapper] Embedding model changed: "
            "%s → %s. Re-embedding %d records ...",
            previous_model or "(none)",
            current_model,
            count,
        )
        from opencortex.migration.v040_reembed import (
            reembed_all as _reembed_all,
        )

        updated = await _reembed_all(
            orch._storage,
            orch._get_collection(),
            orch._embedder,
        )
        logger.info(
            "[SubsystemBootstrapper] Re-embedded %d records (model: %s → %s)",
            updated,
            previous_model or "(none)",
            current_model,
        )
        marker.write_text(current_model)
