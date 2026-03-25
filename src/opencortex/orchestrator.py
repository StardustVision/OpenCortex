# SPDX-License-Identifier: Apache-2.0
"""
Memory Orchestrator for OpenCortex.

The orchestrator is the primary user-facing API that wires together all
internal components:

- CortexConfig: tenant/user isolation
- CortexFS: three-layer (L0/L1/L2) filesystem abstraction
- StorageInterface: vector storage (Qdrant-backed)
- HierarchicalRetriever: directory-aware recursive search
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
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union
from uuid import uuid4

from opencortex.config import CortexConfig, get_config
from opencortex.prompts import build_doc_summarization_prompt, build_layer_derivation_prompt
from opencortex.http.request_context import get_effective_identity, get_effective_project_id
from opencortex.core.context import Context, ContextType as CoreContextType
from opencortex.core.message import Message
from opencortex.core.user_id import UserIdentifier
from opencortex.models.embedder.base import EmbedderBase
from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever
from opencortex.retrieve.intent_analyzer import IntentAnalyzer, LLMCompletionCallable
from opencortex.retrieve.rerank_config import RerankConfig
from opencortex.retrieve.intent_router import IntentRouter
from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
    FindResult,
    MatchedContext,
    MERGEABLE_CATEGORIES,
    QueryResult,
    SearchIntent,
    TypedQuery,
)
from opencortex.storage.collection_schemas import init_context_collection
from opencortex.storage.cortex_fs import CortexFS, init_cortex_fs
from opencortex.storage.storage_interface import StorageInterface
from opencortex.utils.uri import CortexURI

logger = logging.getLogger(__name__)

# Default collection name for all context types
_CONTEXT_COLLECTION = "context"

# Maximum number of batch_add items processed concurrently
_BATCH_ADD_CONCURRENCY = 8

class MemoryOrchestrator:
    """
    Top-level orchestrator for OpenCortex memory operations.

    Wires together storage, filesystem, retrieval, embedding, and
    reinforcement learning into a single coherent API.

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
        self._retriever: Optional[HierarchicalRetriever] = None
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

        # v0.6: Query classifier (lazy — initialized after embedder is ready)
        self._query_classifier = None

    # =========================================================================
    # Collection Routing
    # =========================================================================

    def _get_collection(self) -> str:
        """Return active collection name (contextvar override or default)."""
        from opencortex.http.request_context import get_collection_name
        return get_collection_name() or _CONTEXT_COLLECTION

    def _ensure_query_classifier(self):
        """Lazily initialize QueryFastClassifier after embedder is ready."""
        if self._query_classifier is None and self._config.query_classifier_enabled:
            try:
                from opencortex.retrieve.query_classifier import QueryFastClassifier
                self._query_classifier = QueryFastClassifier(self._embedder, self._config)
            except Exception as e:
                logger.warning("[Orchestrator] Failed to init QueryFastClassifier: %s", e)

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
            self._storage = QdrantStorageAdapter(
                path=db_path,
                embedding_dim=self._config.embedding_dimension,
            )
            logger.info(
                "[MemoryOrchestrator] Auto-created QdrantStorageAdapter at %s",
                db_path,
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

        # 6. Retriever (with rerank config from CortexConfig + LLM fallback)
        rerank_cfg = self._build_rerank_config()
        self._retriever = HierarchicalRetriever(
            storage=self._storage,
            embedder=self._embedder,
            rerank_config=rerank_cfg,
            llm_completion=self._llm_completion,
            flat_rerank_multiplier=self._config.rerank_flat_pool_multiplier,
            force_flat_search=self._config.force_flat_search,
        )

        # 7. Background maintenance: text indexes, migrations, re-embed
        asyncio.create_task(self._startup_maintenance())

        # 8. Cortex Alpha components
        await self._init_alpha()

        self._initialized = True
        logger.info("[MemoryOrchestrator] Initialized (data_root=%s)", self._config.data_root)
        return self

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

    def _create_default_embedder(self) -> Optional[EmbedderBase]:
        """
        Auto-create an embedder based on CortexConfig.

        Resolution order:
        1. If ``embedding_provider == "local"``, create a
           :class:`LocalEmbedder` using FastEmbed ONNX inference (BGE-M3).
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
            try:
                from opencortex.models.embedder.volcengine_embedders import (
                    VolcengineDenseEmbedder,
                )

                api_key = (
                    self._config.embedding_api_key
                    or os.environ.get("OPENCORTEX_EMBEDDING_API_KEY", "")
                )
                if not api_key:
                    logger.warning(
                        "[MemoryOrchestrator] embedding_provider='volcengine' but no "
                        "api_key found in config or OPENCORTEX_EMBEDDING_API_KEY env var. "
                        "Skipping auto-embedder creation."
                    )
                    return None

                model_name = self._config.embedding_model
                if not model_name:
                    logger.warning(
                        "[MemoryOrchestrator] embedding_provider='volcengine' but "
                        "embedding_model is not set. Skipping auto-embedder creation."
                    )
                    return None

                embedder = VolcengineDenseEmbedder(
                    model_name=model_name,
                    api_key=api_key,
                    api_base=self._config.embedding_api_base
                    or "https://ark.cn-beijing.volces.com/api/v3",
                    dimension=self._config.embedding_dimension or None,
                )
                logger.info(
                    "[MemoryOrchestrator] Auto-created VolcengineDenseEmbedder "
                    "(model=%s)",
                    model_name,
                )
                return self._wrap_with_cache(self._wrap_with_hybrid(embedder))
            except ImportError as exc:
                logger.warning(
                    "[MemoryOrchestrator] Cannot create Volcengine embedder — "
                    "volcenginesdkarkruntime not installed: %s",
                    exc,
                )
                return None
            except Exception as exc:
                logger.warning(
                    "[MemoryOrchestrator] Failed to create Volcengine embedder: %s",
                    exc,
                )
                return None

        if provider == "openai":
            try:
                from opencortex.models.embedder.openai_embedder import (
                    OpenAIDenseEmbedder,
                )

                api_key = (
                    self._config.embedding_api_key
                    or os.environ.get("OPENCORTEX_EMBEDDING_API_KEY", "")
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
                    api_base=self._config.embedding_api_base or "https://api.openai.com/v1",
                    dimension=self._config.embedding_dimension or None,
                )
                logger.info(
                    "[MemoryOrchestrator] Auto-created OpenAIDenseEmbedder "
                    "(model=%s)",
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
            from opencortex.models.embedder.local_embedder import LocalEmbedder

            model_name = self._config.embedding_model or "intfloat/multilingual-e5-large"
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
                    self._config.embedding_dimension, detected_dim,
                )
                self._config.embedding_dimension = detected_dim

            logger.info(
                "[MemoryOrchestrator] Auto-created LocalEmbedder "
                "(model=%s, dim=%d)",
                model_name, detected_dim,
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
            logger.info("[MemoryOrchestrator] Wrapped embedder with LRU cache (max=10000, ttl=3600s)")
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
            previous_model or "(none)", current_model, count,
        )
        from opencortex.migration.v040_reembed import reembed_all as _reembed_all
        updated = await _reembed_all(
            self._storage, self._get_collection(), self._embedder,
        )
        logger.info(
            "[Orchestrator] Re-embedded %d records (model: %s → %s)",
            updated, previous_model or "(none)", current_model,
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
                backfill_new_fields, cleanup_root_junk,
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

    async def reembed_all(self) -> int:
        """Re-embed all records with the current embedder.

        Can be called manually or via the admin HTTP endpoint.

        Returns:
            Number of records updated.
        """
        from opencortex.migration.v040_reembed import reembed_all as _reembed_all
        count = await _reembed_all(
            self._storage, self._get_collection(), self._embedder,
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
            max_candidates=getattr(base, "max_candidates", 0) or cfg.rerank_max_candidates,
            use_llm_fallback=getattr(base, "use_llm_fallback", True),
        )

    async def _write_immediate(self, session_id: str, msg_index: int, text: str) -> str:
        """Write a single message for immediate searchability. No LLM, no CortexFS."""
        from opencortex.http.request_context import get_effective_identity, get_effective_project_id
        from opencortex.utils.uri import CortexURI
        from uuid import uuid4

        tid, uid = get_effective_identity()
        nid = uuid4().hex[:12]
        uri = CortexURI.build_private(tid, uid, "memories", "events", nid)

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
                loop.run_in_executor(None, self._embedder.embed, embed_input), timeout=2.0
            )
            vector = result.dense_vector
            sparse_vector = result.sparse_vector

        record = {
            "uri": uri,
            "parent_uri": CortexURI.build_private(tid, uid, "memories", "events", session_id),
            "is_leaf": True,
            "abstract": text,
            "overview": "",
            "context_type": "memory",
            "category": "events",
            "scope": "private",
            "source_user_id": uid,
            "source_tenant_id": tid,
            "keywords": "",
            "meta": {"layer": "immediate", "msg_index": msg_index, "session_id": session_id},
            "session_id": session_id,
            "project_id": get_effective_project_id(),
            "mergeable": False,
            "ttl_expires_at": "",
        }
        # 24h TTL safety net — explicit delete on merge/end is the primary cleanup
        from datetime import datetime, timedelta, timezone
        record["ttl_expires_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=24)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        if vector:
            record["vector"] = vector
        if sparse_vector:
            record["sparse_vector"] = sparse_vector

        await self._storage.upsert(self._get_collection(), record)
        return uri

    async def _add_document(
        self, content, abstract, overview, category, parent_uri,
        context_type, meta, session_id, source_path,
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
        _effective_source_path = source_path or (meta or {}).get("source_path", "") or (meta or {}).get("file_path", "")
        if _effective_source_path:
            source_doc_id = hashlib.sha256(_effective_source_path.encode()).hexdigest()[:16]
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
                    "source_section_path": chunks[0].meta.get("section_path", "") if chunks else "",
                    "chunk_role": "document",
                },
                session_id=session_id,
                embed_text=embed_text,
            )

        # Multi-chunk: create parent + children
        # Title priority: source filename > caller-provided abstract > fallback "Document"
        doc_title = (
            Path(source_path).stem if source_path
            else abstract if abstract
            else "Document"
        )

        parent_ctx = await self.add(
            abstract=doc_title,
            content="",
            category=category,
            parent_uri=parent_uri,
            is_leaf=False,
            context_type=context_type,
            meta={
                **(meta or {}),
                "ingest_mode": "memory",
                "source_doc_id": source_doc_id,
                "source_doc_title": source_doc_title,
                "source_section_path": "",
                "chunk_role": "document",
            },
            session_id=session_id,
        )
        doc_parent_uri = parent_ctx.uri

        # Process chunks sequentially to maintain parent_index references
        chunk_results = []
        for idx, chunk in enumerate(chunks):
            chunk_parent = doc_parent_uri
            if chunk.parent_index >= 0 and chunk.parent_index < len(chunk_results):
                parent_result = chunk_results[chunk.parent_index]
                if parent_result and not parent_result.is_leaf:
                    chunk_parent = parent_result.uri

            # Determine chunk_role: directory chunks (is_leaf=False) become "section",
            # leaf chunks become "leaf". We detect via next chunk's parent_index.
            # A chunk is a directory if any later chunk references it as parent.
            is_dir_chunk = any(
                c.parent_index == idx for c in chunks[idx + 1:]
            )
            chunk_role = "section" if is_dir_chunk else "leaf"

            chunk_abstract = ""
            embed_text = ""
            if self._config.context_flattening_enabled:
                parts = []
                if source_doc_title:
                    parts.append(f"[{source_doc_title}]")
                sp = chunk.meta.get("source_section_path", "") or chunk.meta.get("section_path", "")
                if sp:
                    parts.append(f"[{sp}]")
                parts.append(chunk_abstract)
                embed_text = " ".join(parts)
            ctx = await self.add(
                abstract=chunk_abstract,
                content=chunk.content,
                category=category,
                parent_uri=chunk_parent,
                context_type=context_type,
                meta={
                    **(meta or {}),
                    "ingest_mode": "memory",
                    "chunk_index": idx,
                    "source_doc_id": source_doc_id,
                    "source_doc_title": source_doc_title,
                    "source_section_path": chunk.meta.get("section_path", ""),
                    "chunk_role": chunk_role,
                },
                session_id=session_id,
                embed_text=embed_text,
            )
            chunk_results.append(ctx)

        return parent_ctx

    async def _derive_layers(
        self, user_abstract: str, content: str, user_overview: str = "",
    ) -> Dict[str, str]:
        """Derive L0/L1/keywords from L2 in a single LLM call.

        Returns {"abstract": str, "overview": str, "keywords": str}
        keywords is a comma-separated string (for Qdrant MatchText).
        """
        # Fast path: user already provided both abstract and overview
        if user_abstract and user_overview:
            return {"abstract": user_abstract, "overview": user_overview, "keywords": ""}

        if self._llm_completion:
            prompt = build_layer_derivation_prompt(content, user_abstract)
            try:
                response = await self._llm_completion(prompt)
                from opencortex.utils.json_parse import parse_json_from_response
                data = parse_json_from_response(response)
                if isinstance(data, dict):
                    llm_abstract = (data.get("abstract") or "").strip()
                    llm_overview = (data.get("overview") or "").strip()
                    keywords_list = data.get("keywords") or []
                    if isinstance(keywords_list, list):
                        keywords = ", ".join(str(k) for k in keywords_list if k)
                    else:
                        keywords = str(keywords_list)
                    return {
                        "abstract": user_abstract or llm_abstract,
                        "overview": user_overview or llm_overview,
                        "keywords": keywords,
                    }
            except Exception as e:
                logger.warning("[Orchestrator] _derive_layers LLM failed: %s", e)

        # No-LLM fallback
        abstract = user_abstract or content
        overview = user_overview
        if not overview and content and len(content) <= 500:
            overview = content
        if not user_abstract and not self._llm_completion:
            logger.warning("[Orchestrator] No LLM configured — abstract uses raw content")
        return {"abstract": abstract, "overview": overview, "keywords": ""}

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
            context_type: Explicit context type ("memory", "resource", "skill").
                          Auto-derived from URI if not provided.
            is_leaf: Whether this is a leaf node (default True).
            meta: Additional metadata dict.
            related_uri: List of related context URIs.
            session_id: Session identifier.
            dedup: If True, check for semantically similar records before
                inserting. Mergeable categories merge content; non-mergeable
                categories skip silently. Set False for bulk import.
            dedup_threshold: Minimum similarity score to consider a duplicate
                (default 0.82).
            embed_text: Optional text used for embedding instead of abstract.
                Useful when the display text (abstract) differs from the
                optimal search text (e.g., omitting date prefixes).

        Returns:
            The created Context object.  ``meta["dedup_action"]`` indicates
            what happened: ``"created"`` (new), ``"merged"``, or
            ``"skipped"``.
        """
        self._ensure_init()

        # Determine ingestion mode
        from opencortex.ingest.resolver import IngestModeResolver

        ingest_mode = IngestModeResolver.resolve(
            content=content,
            meta=meta,
            source_path=(meta or {}).get("source_path", ""),
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
                source_path=(meta or {}).get("source_path", ""),
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

        # Build parent URI if not provided
        if not parent_uri:
            parent_uri = self._derive_parent_uri(uri)

        # Derive L0/L1/keywords from L2 in a single structured LLM call
        keywords = ""
        if content and is_leaf:
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
            meta=meta or {},
            session_id=session_id,
            user=effective_user,
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
        effective_category = category or self._extract_category_from_uri(uri)
        if dedup and ctx.vector and is_leaf:
            dedup_started = asyncio.get_running_loop().time()
            dup = await self._check_duplicate(
                vector=ctx.vector,
                category=effective_category,
                context_type=context_type or "memory",
                threshold=dedup_threshold,
                tid=tid,
                uid=uid,
            )
            dedup_ms = int((asyncio.get_running_loop().time() - dedup_started) * 1000)
            if dup:
                existing_uri, existing_score = dup
                total_ms = int((asyncio.get_running_loop().time() - add_started) * 1000)
                if effective_category in MERGEABLE_CATEGORIES:
                    await self._merge_into(existing_uri, abstract, content)
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
                else:
                    logger.info(
                        "[MemoryOrchestrator] add tenant=%s user=%s uri=%s "
                        "dedup_action=skipped dedup_target=%s score=%.3f "
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
                    ctx.meta["dedup_action"] = "skipped"
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
        record["mergeable"] = effective_category in MERGEABLE_CATEGORIES
        record["session_id"] = session_id or ""
        record["ttl_expires_at"] = ""
        record["project_id"] = get_effective_project_id()
        record["source_tenant_id"] = tid
        record["keywords"] = keywords

        # v0.6: Flatten doc/conversation enrichment fields to top-level payload
        record["source_doc_id"] = (meta or {}).get("source_doc_id", "")
        record["source_doc_title"] = (meta or {}).get("source_doc_title", "")
        record["source_section_path"] = (meta or {}).get("source_section_path", "")
        record["chunk_role"] = (meta or {}).get("chunk_role", "")
        record["speaker"] = (meta or {}).get("speaker", "")
        record["event_date"] = (meta or {}).get("event_date")

        # Set TTL for staging records (24 hours from now)
        if context_type == "staging":
            from datetime import datetime, timezone, timedelta
            expires = datetime.now(timezone.utc) + timedelta(hours=24)
            record["ttl_expires_at"] = expires.strftime("%Y-%m-%dT%H:%M:%SZ")

        upsert_started = asyncio.get_running_loop().time()
        await self._storage.upsert(self._get_collection(), record)
        upsert_ms = int((asyncio.get_running_loop().time() - upsert_started) * 1000)

        # Write to filesystem (L0 abstract + L1 overview + L2 content)
        fs_write_started = asyncio.get_running_loop().time()
        await self._fs.write_context(
            uri=uri,
            content=content,
            abstract=abstract,
            overview=overview,
            is_leaf=is_leaf,
        )
        fs_write_ms = int((asyncio.get_running_loop().time() - fs_write_started) * 1000)

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

    # ------------------------------------------------------------------
    # Write-time dedup helpers
    # ------------------------------------------------------------------

    async def _check_duplicate(
        self,
        vector: list,
        category: str,
        context_type: str,
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
            if category:
                conds.append(
                    {"op": "must", "field": "category", "conds": [category]}
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
                                {"op": "must", "field": "source_user_id", "conds": [uid]},
                            ],
                        },
                    ],
                }
            )
            # Project isolation: only dedup within same project
            project_id = get_effective_project_id()
            if project_id:
                conds.append({"op": "must", "field": "project_id", "conds": [project_id]})

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
    ) -> bool:
        """
        Update an existing context.

        Re-embeds if abstract changes, updates vector DB and filesystem.

        Args:
            uri: URI of the context to update.
            abstract: New abstract (re-embeds if changed).
            content: New full content.
            meta: Metadata fields to merge.

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

        if abstract is not None:
            update_data["abstract"] = abstract
            # Re-embed
            if self._embedder:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, self._embedder.embed, abstract
                )
                update_data["vector"] = result.dense_vector
                if result.sparse_vector:
                    update_data["sparse_vector"] = result.sparse_vector

        if meta:
            existing_meta = record.get("meta", {})
            if isinstance(existing_meta, str):
                import json
                try:
                    existing_meta = json.loads(existing_meta)
                except (json.JSONDecodeError, TypeError):
                    existing_meta = {}
            existing_meta.update(meta)
            update_data["meta"] = existing_meta

        if update_data:
            await self._storage.update(self._get_collection(), record_id, update_data)

        # Update filesystem
        if abstract is not None or content is not None:
            await self._fs.write_context(
                uri=uri,
                content=content or "",
                abstract=abstract or "",
            )

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

        # Remove from vector DB
        count = await self._storage.remove_by_uri(self._get_collection(), uri)

        # Remove from filesystem
        try:
            await self._fs.rm(uri, recursive=recursive)
        except Exception as e:
            logger.warning(
                "[MemoryOrchestrator] FS removal failed for %s: %s", uri, e
            )

        logger.info("[MemoryOrchestrator] Removed %d records for: %s", count, uri)
        return count

    # =========================================================================
    # Search / Retrieve
    # =========================================================================

    async def search(
        self,
        query: str,
        context_type: Optional[ContextType] = None,
        target_uri: str = "",
        limit: int = 5,
        score_threshold: Optional[float] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
        detail_level: str = "l1",
        search_intent: Optional[SearchIntent] = None,
        meta: Optional[Dict[str, Any]] = None,
        session_context: Optional[Dict[str, Any]] = None,
    ) -> FindResult:
        """
        Search for relevant contexts.

        Uses IntentRouter to determine retrieval strategy (top_k, detail_level,
        time_scope, trigger categories) based on the query.

        Args:
            query: Natural language query.
            context_type: Restrict to a specific type (memory/resource/skill).
            target_uri: Restrict search to a directory subtree.
            limit: Maximum results per type.
            score_threshold: Minimum relevance score.
            metadata_filter: Additional filter conditions.
            detail_level: Fallback detail level if router doesn't override.
            meta: Optional metadata dict (may contain target_doc_id for classifier).
            session_context: Optional session context for classifier hints.

        Returns:
            FindResult with memories, resources, and skills.
        """
        self._ensure_init()
        search_started = asyncio.get_running_loop().time()
        tid, uid = get_effective_identity()

        # v0.6: Query classification (fast path)
        classification = None
        target_doc_id = None
        if isinstance(meta, dict):
            target_doc_id = meta.get("target_doc_id")

        if self._config.query_classifier_enabled:
            self._ensure_query_classifier()
            if self._query_classifier:
                classification = self._query_classifier.classify(
                    query, target_doc_id=target_doc_id, session_context=session_context
                )

        # Allow callers that already performed routing to reuse the intent
        # and avoid paying the LLM/classification cost twice.
        intent = search_intent
        intent_ms = 0
        if intent is None:
            intent_started = asyncio.get_running_loop().time()
            router = IntentRouter(llm_completion=self._llm_completion)
            intent = await router.route(query, context_type)
            intent_ms = int((asyncio.get_running_loop().time() - intent_started) * 1000)

        # Gate: skip retrieval if intent says no recall needed
        if not intent.should_recall:
            total_ms = int((asyncio.get_running_loop().time() - search_started) * 1000)
            logger.debug(
                "[search] should_recall=False tenant=%s user=%s total_ms=%d",
                tid, uid, total_ms,
            )
            return FindResult(
                memories=[], resources=[], skills=[],
                search_intent=intent,
            )

        # Use intent to determine effective limit
        effective_limit = max(limit, intent.top_k)

        typed_queries = intent.queries

        # Fallback: if router produced no queries, build them manually
        if not typed_queries:
            if context_type:
                types_to_search = [context_type]
            elif target_uri:
                types_to_search = [self._infer_context_type(target_uri)]
            else:
                types_to_search = [ContextType.ANY]

            dl = DetailLevel(detail_level)
            typed_queries = [
                TypedQuery(
                    query=query,
                    context_type=ct,
                    intent="",
                    priority=1,
                    target_directories=[target_uri] if target_uri else [],
                    detail_level=dl,
                )
                for ct in types_to_search
            ]

        # HyDE: generate hypothetical answers for dense embedding
        if self._config.hyde_enabled and self._llm_completion:
            from opencortex.prompts import build_hyde_prompt

            async def _hyde_rewrite(tq):
                try:
                    hyde_answer = await self._llm_completion(
                        build_hyde_prompt(tq.query),
                    )
                    if hyde_answer and len(hyde_answer.strip()) > 10:
                        tq.hyde_text = hyde_answer.strip()
                except Exception:
                    pass  # graceful degradation — use original query
                return tq

            typed_queries = list(
                await asyncio.gather(*[_hyde_rewrite(tq) for tq in typed_queries])
            )

        # Set target directories on queries if specified
        if target_uri:
            for tq in typed_queries:
                if not tq.target_directories:
                    tq.target_directories = [target_uri]

        # Exclude staging from global search
        staging_exclude = {"op": "must_not", "field": "context_type", "conds": ["staging"]}

        # Scope-aware filter: return shared + user's private + legacy (no scope)
        scope_filter = {"op": "or", "conds": [
            {"op": "must", "field": "scope", "conds": ["shared", ""]},
            {"op": "and", "conds": [
                {"op": "must", "field": "scope", "conds": ["private"]},
                {"op": "must", "field": "source_user_id", "conds": [uid]},
            ]},
        ]}

        # Tenant isolation: hard filter by source_tenant_id
        # Empty string covers legacy records without tenant field
        if tid:
            tenant_filter = {"op": "must", "field": "source_tenant_id", "conds": [tid, ""]}
            combined_conds = [staging_exclude, scope_filter, tenant_filter]
        else:
            combined_conds = [staging_exclude, scope_filter]

        # Project-scoped filter: strict isolation by project_id
        project_id = get_effective_project_id()
        if project_id and project_id != "public":
            project_filter = {"op": "or", "conds": [
                {"op": "must", "field": "project_id", "conds": [project_id, "public"]},
            ]}
            combined_conds.append(project_filter)

        if metadata_filter:
            metadata_filter = {"op": "and", "conds": [metadata_filter] + combined_conds}
        else:
            metadata_filter = {"op": "and", "conds": combined_conds}

        # Dynamic hybrid weight: classifier takes precedence over intent router
        lexical_boost = classification.lexical_boost if classification else intent.lexical_boost

        # Build retrieval coroutines
        retrieval_coros = [
            self._retriever.retrieve(
                tq,
                limit=effective_limit,
                score_threshold=score_threshold,
                metadata_filter=metadata_filter,
                lexical_boost=lexical_boost,
                classification=classification,
            )
            for tq in typed_queries
        ]

        query_results = list(await asyncio.gather(*retrieval_coros))
        retrieval_ms = int((asyncio.get_running_loop().time() - search_started) * 1000) - intent_ms

        result = self._aggregate_results(query_results)
        result.search_intent = intent

        # Filter out directory nodes (is_leaf=False) — they exist for
        # hierarchical traversal but have no abstract/content of their own.
        result.memories = [m for m in result.memories if m.is_leaf]
        result.resources = [m for m in result.resources if m.is_leaf]
        result.skills = [m for m in result.skills if m.is_leaf]

        # Fire-and-forget: resolve URIs → record IDs → update access stats
        all_matched = result.memories + result.resources + result.skills
        if all_matched:
            uris = [mc.uri for mc in all_matched]
            asyncio.create_task(self._resolve_and_update_access_stats(uris))

        total_ms = int((asyncio.get_running_loop().time() - search_started) * 1000)
        logger.info(
            "[search] tenant=%s user=%s intent=%s queries=%d results=%d "
            "timing_ms(total=%d intent=%d retrieval=%d)",
            tid,
            uid,
            intent.intent_type,
            len(typed_queries),
            len(all_matched),
            total_ms,
            intent_ms,
            max(retrieval_ms, 0),
        )

        # v0.6: Build SearchExplainSummary
        if getattr(self._config, 'explain_enabled', True) and query_results:
            from opencortex.retrieve.types import SearchExplainSummary
            primary = query_results[0]
            result.explain_summary = SearchExplainSummary(
                total_ms=float(total_ms),
                query_count=len(query_results),
                primary_query_class=primary.explain.query_class if primary.explain else "",
                primary_path=primary.explain.path if primary.explain else "",
                doc_scope_hit=any(qr.explain and qr.explain.doc_scope_hit for qr in query_results),
                time_filter_hit=any(qr.explain and qr.explain.time_filter_hit for qr in query_results),
                rerank_triggered=any(qr.explain and qr.explain.rerank_ms > 0 for qr in query_results),
            )

        return result

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
        from datetime import datetime, timezone
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

        # Exclude staging from session search
        staging_exclude = {"op": "must_not", "field": "context_type", "conds": ["staging"]}
        if metadata_filter:
            metadata_filter = {"op": "and", "conds": [metadata_filter, staging_exclude]}
        else:
            metadata_filter = staging_exclude

        query_results = await asyncio.gather(
            *[
                self._retriever.retrieve(
                    tq,
                    limit=limit,
                    score_threshold=score_threshold,
                    metadata_filter=metadata_filter,
                )
                for tq in query_plan.queries
            ]
        )

        result = self._aggregate_results(query_results)
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
    ) -> List[Dict[str, Any]]:
        """List user's accessible memories with readable content.

        Returns private (own) + shared memories, ordered by updated_at desc.
        """
        self._ensure_init()
        tid, uid = get_effective_identity()

        # Same scope filter as search(): private own + shared
        scope_filter = {"op": "or", "conds": [
            {"op": "must", "field": "scope", "conds": ["shared", ""]},
            {"op": "and", "conds": [
                {"op": "must", "field": "scope", "conds": ["private"]},
                {"op": "must", "field": "source_user_id", "conds": [uid]},
            ]},
        ]}

        conds: List[Dict[str, Any]] = [
            {"op": "must_not", "field": "context_type", "conds": ["staging"]},
            scope_filter,
        ]
        if tid:
            conds.append({"op": "must", "field": "source_tenant_id", "conds": [tid, ""]})
        if category:
            conds.append({"op": "must", "field": "category", "conds": [category]})
        if context_type:
            conds.append({"op": "must", "field": "context_type", "conds": [context_type]})

        # Project filter: strict isolation
        project_id = get_effective_project_id()
        if project_id and project_id != "public":
            conds.append({"op": "or", "conds": [
                {"op": "must", "field": "project_id", "conds": [project_id, "public"]},
            ]})

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
                "updated_at": r.get("updated_at", ""),
                "created_at": r.get("created_at", ""),
            }
            for r in records
        ]

    # =========================================================================
    # Reinforcement Learning
    # =========================================================================

    async def feedback(self, uri: str, reward: float) -> None:
        """
        Submit a reward signal for a context (reinforcement learning).

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

    async def feedback_batch(
        self, rewards: List[Dict[str, Any]]
    ) -> None:
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
        from datetime import datetime, timezone
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
            logger.info(
                "[MemoryOrchestrator] Set protected=%s for: %s", protected, uri
            )

    async def get_profile(self, uri: str) -> Optional[Dict[str, Any]]:
        """
        Get the reinforcement learning profile for a context.

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
            profile = await self._storage.get_profile(
                self._get_collection(), record_id
            )
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
                issues.append("No LLM configured — intent analysis and session extraction disabled")
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
                session_id=session_id,
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
            self._observer.record_message(
                session_id=session_id,
                role=role,
                content=content,
                tenant_id=tid,
                user_id=uid,
                meta=meta,
            )
            message_count = len(self._observer.get_transcript(session_id))
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
            transcript = self._observer.flush(session_id)
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
                        session_id, alpha_traces_count,
                    )

                    # Check Archivist trigger
                    if self._archivist and self._trace_store:
                        count = await self._trace_store.count_new_traces(tid)
                        if self._archivist.should_trigger(count):
                            asyncio.create_task(self._run_archivist(tid, uid))
                except Exception as exc:
                    logger.warning("[Alpha] Trace splitting failed: %s", exc)

        return {
            "session_id": session_id,
            "quality_score": quality_score,
            "alpha_traces": alpha_traces_count,
        }

    async def _run_archivist(self, tenant_id: str, user_id: str) -> None:
        """Run Archivist in background to extract knowledge from traces."""
        if not self._archivist or not self._trace_store or not self._knowledge_store:
            return
        try:
            from opencortex.alpha.types import KnowledgeScope
            traces = await self._trace_store.list_by_session("", tenant_id, user_id)
            if not traces:
                return
            knowledge_items = await self._archivist.run(
                traces, tenant_id, user_id, KnowledgeScope.USER,
            )
            for k in knowledge_items:
                await self._knowledge_store.save(k)
            logger.info(
                "[Alpha] Archivist extracted %d knowledge candidates",
                len(knowledge_items),
            )
        except Exception as exc:
            logger.warning("[Alpha] Archivist failed: %s", exc)

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
            query, tid, uid, types=types, limit=limit,
        )
        return {"results": results, "count": len(results)}

    async def knowledge_approve(self, knowledge_id: str) -> Dict[str, Any]:
        """Approve a knowledge candidate (move to active)."""
        self._ensure_init()
        if not self._knowledge_store:
            return {"ok": False, "error": "Knowledge store not initialized"}
        ok = await self._knowledge_store.approve(knowledge_id)
        return {"ok": ok, "knowledge_id": knowledge_id, "status": "active" if ok else "not_found"}

    async def knowledge_reject(self, knowledge_id: str) -> Dict[str, Any]:
        """Reject a knowledge candidate (deprecate)."""
        self._ensure_init()
        if not self._knowledge_store:
            return {"ok": False, "error": "Knowledge store not initialized"}
        ok = await self._knowledge_store.reject(knowledge_id)
        return {"ok": ok, "knowledge_id": knowledge_id, "status": "deprecated" if ok else "not_found"}

    async def knowledge_list_candidates(self) -> Dict[str, Any]:
        """List knowledge candidates pending approval."""
        self._ensure_init()
        if not self._knowledge_store:
            return {"candidates": [], "error": "Knowledge store not initialized"}
        tid, _ = get_effective_identity()
        candidates = await self._knowledge_store.list_candidates(tid)
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
                        meta={"source": "batch:scan", "dir_path": d, "ingest_mode": "memory"},
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
                abstract, overview = await self._generate_abstract_overview(content, file_path)

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
                new_uri = CortexURI.build_shared(tid, "resources", project_id, "documents", node_name)

                # 3. Update record fields
                record["uri"] = new_uri
                record["scope"] = "shared"
                record["project_id"] = project_id
                record["parent_uri"] = CortexURI.build_shared(tid, "resources", project_id, "documents")

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
        rerank_info = {"enabled": False, "mode": "disabled", "model": None, "fusion_beta": 0.0}
        if self._retriever and self._retriever._rerank_client:
            rc = self._retriever._rerank_client
            rerank_info = {
                "enabled": rc.mode != "disabled",
                "mode": rc.mode,
                "model": self._config.rerank_model or None,
                "fusion_beta": rc.fusion_beta,
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
        if not self._llm_completion:
            # Fallback: filename as abstract, first 500 chars as overview
            return file_path, content[:500]

        prompt = build_doc_summarization_prompt(file_path, content)

        try:
            response = await self._llm_completion(prompt)
            from opencortex.utils.json_parse import parse_json_from_response
            data = parse_json_from_response(response)
            if isinstance(data, dict):
                return data.get("abstract", file_path), data.get("overview", content[:500])
        except Exception:
            pass

        return file_path, content[:500]

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
        node_name = semantic_node_name(abstract) if abstract else uuid4().hex[:12]

        if context_type == "memory":
            cat = category if category in self._USER_MEMORY_CATEGORIES else "events"
            return CortexURI.build_private(tid, uid, "memories", cat, node_name)

        elif context_type == "case":
            return CortexURI.build_shared(tid, "shared", "cases", node_name)

        elif context_type == "pattern":
            return CortexURI.build_shared(tid, "shared", "patterns", node_name)

        elif context_type == "skill":
            section = category or "general"
            return CortexURI.build_shared(tid, "shared", "skills", section, node_name)

        elif context_type == "resource":
            project = get_effective_project_id()  # e.g. "OpenCortex" or "public"
            if category:
                return CortexURI.build_shared(tid, "resources", project, category, node_name)
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
        raise ValueError(f"URI conflict unresolved after {max_attempts} attempts: {uri}")

    @staticmethod
    def _extract_category_from_uri(uri: str) -> str:
        """Extract category from URI path. E.g. /memories/preferences/abc -> preferences.

        For resources the path is resources/{project}/{category}/{nid},
        so the category is two segments after "resources".
        """
        parts = uri.split("/")
        # Look for known parent segments, return next part
        for parent in ("memories", "cases", "patterns", "skills", "staging", "resources"):
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

        The HierarchicalRetriever traverses the directory tree via parent_uri
        links. For leaves to be discoverable, every intermediate directory
        must have a record in the vector store (is_leaf=False).

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
        self, query_results: List[QueryResult]
    ) -> FindResult:
        """Aggregate multiple QueryResults into a single FindResult (deduped by URI)."""
        memories, resources, skills = [], [], []
        seen_uris: set = set()

        for result in query_results:
            for ctx in result.matched_contexts:
                if ctx.uri in seen_uris:
                    continue
                seen_uris.add(ctx.uri)
                if ctx.context_type in (ContextType.MEMORY, ContextType.CASE, ContextType.PATTERN):
                    memories.append(ctx)
                elif ctx.context_type == ContextType.RESOURCE:
                    resources.append(ctx)
                elif ctx.context_type == ContextType.SKILL:
                    skills.append(ctx)
                else:
                    # ANY or unknown — classify as memory
                    memories.append(ctx)

        return FindResult(
            memories=memories,
            resources=resources,
            skills=skills,
        )
