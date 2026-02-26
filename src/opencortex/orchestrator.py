# SPDX-License-Identifier: Apache-2.0
"""
Memory Orchestrator for OpenCortex.

The orchestrator is the primary user-facing API that wires together all
internal components:

- CortexConfig: tenant/user isolation
- VikingFS: three-layer (L0/L1/L2) filesystem abstraction
- VikingDBInterface (RuVectorAdapter): vector storage + SONA reinforcement
- HierarchicalRetriever: directory-aware recursive search
- IntentAnalyzer: LLM-driven session-aware query planning
- EmbedderBase: pluggable embedding

Typical usage::

    from opencortex import CortexConfig, init_config
    from opencortex.orchestrator import MemoryOrchestrator

    init_config(CortexConfig(tenant_id="myteam", user_id="alice"))
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
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union
from uuid import uuid4

from opencortex.config import CortexConfig, get_config
from opencortex.core.context import Context, ContextType as CoreContextType
from opencortex.core.message import Message
from opencortex.core.user_id import UserIdentifier
from opencortex.models.embedder.base import EmbedderBase
from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever
from opencortex.retrieve.intent_analyzer import IntentAnalyzer, LLMCompletionCallable
from opencortex.retrieve.rerank_config import RerankConfig
from opencortex.retrieve.types import (
    ContextType,
    FindResult,
    MatchedContext,
    QueryResult,
    TypedQuery,
)
from opencortex.storage.collection_schemas import init_context_collection
from opencortex.storage.ruvector.hooks_client import RuVectorHooks
from opencortex.storage.viking_fs import VikingFS, init_viking_fs
from opencortex.storage.vikingdb_interface import VikingDBInterface
from opencortex.utils.uri import CortexURI

logger = logging.getLogger(__name__)

# Default collection name for all context types
_CONTEXT_COLLECTION = "context"


class MemoryOrchestrator:
    """
    Top-level orchestrator for OpenCortex memory operations.

    Wires together storage, filesystem, retrieval, embedding, and SONA
    reinforcement into a single coherent API.

    Args:
        config: CortexConfig instance. Uses global config if not provided.
        storage: VikingDBInterface backend. Auto-creates RuVectorAdapter if None.
        embedder: Embedding model. Required for add/search operations.
        rerank_config: Rerank configuration for retrieval scoring.
        llm_completion: Async callable for IntentAnalyzer (session-aware search).
    """

    def __init__(
        self,
        config: Optional[CortexConfig] = None,
        storage: Optional[VikingDBInterface] = None,
        embedder: Optional[EmbedderBase] = None,
        rerank_config: Optional[RerankConfig] = None,
        llm_completion: Optional[LLMCompletionCallable] = None,
        hooks: Optional[RuVectorHooks] = None,
    ):
        self._config = config or get_config()
        self._storage = storage
        self._embedder = embedder
        self._rerank_config = rerank_config or RerankConfig()
        self._llm_completion = llm_completion
        self._hooks = hooks

        self._fs: Optional[VikingFS] = None
        self._retriever: Optional[HierarchicalRetriever] = None
        self._analyzer: Optional[IntentAnalyzer] = None
        self._user: Optional[UserIdentifier] = None
        self._initialized = False

    # =========================================================================
    # Initialization
    # =========================================================================

    async def init(self) -> "MemoryOrchestrator":
        """
        Initialize all internal components.

        Creates the storage backend (if not provided), initializes VikingFS,
        sets up the context collection, and wires up the retriever.

        Returns:
            self (for chaining)
        """
        if self._initialized:
            return self

        # 1. Storage backend
        if self._storage is None:
            self._storage = self._create_default_storage()

        # 1a. Startup health probe for RuVector HTTP backend
        await self._probe_ruvector_health()

        # 1b. Embedder auto-creation
        if self._embedder is None:
            self._embedder = self._create_default_embedder()

        # 2. User identity
        self._user = UserIdentifier(
            self._config.tenant_id,
            self._config.user_id,
        )

        # 3. VikingFS
        self._fs = init_viking_fs(
            data_root=self._config.data_root,
            query_embedder=self._embedder,
            rerank_config=self._rerank_config,
            vector_store=self._storage,
        )

        # 4. Create context collection if needed
        await init_context_collection(
            self._storage,
            _CONTEXT_COLLECTION,
            self._config.embedding_dimension,
        )

        # 5. Retriever
        self._retriever = HierarchicalRetriever(
            storage=self._storage,
            embedder=self._embedder,
            rerank_config=self._rerank_config,
        )

        # 6. Intent analyzer: use provided callable or auto-create from config
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

        # 7. RuVector hooks for native self-learning (auto-create if not provided)
        if self._hooks is None:
            self._hooks = self._create_default_hooks()

        self._initialized = True
        logger.info(
            "[MemoryOrchestrator] Initialized (tenant=%s, user=%s)",
            self._config.tenant_id,
            self._config.user_id,
        )
        return self

    def _create_default_storage(self) -> VikingDBInterface:
        """Create default RuVector storage backend from config."""
        from opencortex.storage.ruvector import RuVectorAdapter, RuVectorConfig

        rv_config = RuVectorConfig(
            data_dir=self._config.data_root,
            dimension=self._config.embedding_dimension,
            server_host=self._config.ruvector_host,
            server_port=self._config.ruvector_port,
        )
        return RuVectorAdapter(rv_config)

    async def _probe_ruvector_health(self) -> None:
        """Run a startup health probe if storage is a RuVectorAdapter (HTTP mode).

        Logs a clear INFO message when reachable, or a WARNING when not.
        Never raises — startup must continue regardless of probe outcome.
        """
        from opencortex.storage.ruvector.adapter import RuVectorAdapter

        if not isinstance(self._storage, RuVectorAdapter):
            return

        config = self._storage.config
        if not config.use_http:
            # CLI mode — no HTTP server to probe
            return

        host = config.server_host
        port = config.server_port

        try:
            from opencortex.storage.ruvector.http_client import check_ruvector_health

            result = await check_ruvector_health(host, port)
            if result["available"]:
                version_info = (
                    f"version: {result['version']}"
                    if result["version"]
                    else "version: unknown"
                )
                logger.info(
                    "[MemoryOrchestrator] RuVector server at %s:%d is available (%s)",
                    host,
                    port,
                    version_info,
                )
            else:
                logger.warning(
                    "[MemoryOrchestrator] RuVector server at %s:%d is NOT reachable: %s. "
                    "Storage operations will fail.",
                    host,
                    port,
                    result["error"] or "unknown error",
                )
        except Exception as exc:
            logger.warning(
                "[MemoryOrchestrator] RuVector health probe failed unexpectedly: %s. "
                "Storage operations may fail.",
                exc,
            )

    def _create_default_embedder(self) -> Optional[EmbedderBase]:
        """
        Auto-create an embedder based on CortexConfig.

        Resolution order:
        1. If ``embedding_provider == "volcengine"`` in config, create a
           :class:`VolcengineDenseEmbedder` using config values.  The API key
           is taken from ``config.embedding_api_key`` first, then from the
           environment variable ``OPENCORTEX_EMBEDDING_API_KEY``.
        2. If no provider is configured (or the volcengine attempt failed),
           try loading from ``~/.openviking/ov.conf`` via
           :func:`create_embedder_from_ov_conf`.
        3. If nothing works, log a warning and return ``None`` so that tests
           that supply their own mock embedder are not affected.

        Returns:
            An :class:`EmbedderBase` instance, or ``None`` if creation fails.
        """
        import os

        provider = (self._config.embedding_provider or "").strip().lower()

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
                return embedder
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

        # No provider configured — try the ov.conf fallback
        if not provider:
            try:
                from pathlib import Path

                conf_path = Path.home() / ".openviking" / "ov.conf"
                if not conf_path.exists():
                    logger.debug(
                        "[MemoryOrchestrator] No embedding_provider configured and "
                        "%s not found. No embedder will be auto-created.",
                        conf_path,
                    )
                    return None

                from opencortex.models.embedder.volcengine_embedders import (
                    create_embedder_from_ov_conf,
                )

                embedder = create_embedder_from_ov_conf(str(conf_path))
                logger.info(
                    "[MemoryOrchestrator] Auto-created embedder from %s", conf_path
                )
                return embedder
            except ImportError as exc:
                logger.warning(
                    "[MemoryOrchestrator] Cannot create embedder from ov.conf — "
                    "volcenginesdkarkruntime not installed: %s",
                    exc,
                )
                return None
            except Exception as exc:
                logger.warning(
                    "[MemoryOrchestrator] Failed to create embedder from ov.conf: %s",
                    exc,
                )
                return None

        # Unknown / unsupported provider
        logger.warning(
            "[MemoryOrchestrator] Unknown embedding_provider='%s'. "
            "No embedder will be auto-created.",
            provider,
        )
        return None

    def _create_default_hooks(self) -> Optional[RuVectorHooks]:
        """
        Auto-create RuVector hooks for native self-learning.

        Uses npx ruvector hooks to provide:
        - Semantic memory (remember/recall)
        - Q-learning (learn/batch-learn)
        - Trajectory tracking
        - Error pattern learning

        Returns:
            RuVectorHooks instance, or None if npx is not available.
        """
        try:
            from opencortex.storage.ruvector import RuVectorHooks

            hooks = RuVectorHooks(
                data_dir=self._config.data_root,
                cli_path="npx",
                timeout=30,
            )
            logger.info(
                "[MemoryOrchestrator] Auto-created RuVectorHooks for native self-learning"
            )
            return hooks
        except ImportError:
            logger.warning(
                "[MemoryOrchestrator] Could not import RuVectorHooks. "
                "Native self-learning disabled."
            )
            return None
        except Exception as exc:
            logger.warning(
                "[MemoryOrchestrator] Failed to create RuVectorHooks: %s", exc
            )
            return None

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
    def storage(self) -> VikingDBInterface:
        self._ensure_init()
        return self._storage

    @property
    def fs(self) -> VikingFS:
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
        category: str = "",
        parent_uri: Optional[str] = None,
        uri: Optional[str] = None,
        context_type: Optional[str] = None,
        is_leaf: bool = True,
        meta: Optional[Dict[str, Any]] = None,
        related_uri: Optional[List[str]] = None,
        session_id: Optional[str] = None,
    ) -> Context:
        """
        Add a new context (memory, resource, or skill).

        Performs the full pipeline: build URI -> embed -> store vector ->
        write filesystem (L0/L1).

        Args:
            abstract: Short summary (L0). Used as the vectorization text.
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

        Returns:
            The created Context object.
        """
        self._ensure_init()

        # Build URI if not provided
        if not uri:
            uri = self._auto_uri(context_type or "memory", category)

        # Build parent URI if not provided
        if not parent_uri:
            parent_uri = self._derive_parent_uri(uri)

        # Create context object
        ctx = Context(
            uri=uri,
            parent_uri=parent_uri,
            is_leaf=is_leaf,
            abstract=abstract,
            context_type=context_type,
            category=category,
            related_uri=related_uri or [],
            meta=meta or {},
            session_id=session_id,
            user=self._user,
        )

        # Embed
        if self._embedder:
            result = self._embedder.embed(ctx.get_vectorization_text())
            ctx.vector = result.dense_vector

        # Ensure parent directory records exist in vector DB
        if is_leaf and parent_uri:
            await self._ensure_parent_records(parent_uri)

        # Store in vector DB
        record = ctx.to_dict()
        if ctx.vector:
            record["vector"] = ctx.vector
        await self._storage.upsert(_CONTEXT_COLLECTION, record)

        # Write to filesystem (L0 abstract + L2 content)
        await self._fs.write_context(
            uri=uri,
            content=content,
            abstract=abstract,
            is_leaf=is_leaf,
        )

        logger.info("[MemoryOrchestrator] Added context: %s", uri)
        return ctx

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
            _CONTEXT_COLLECTION,
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
                result = self._embedder.embed(abstract)
                update_data["vector"] = result.dense_vector

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
            await self._storage.update(_CONTEXT_COLLECTION, record_id, update_data)

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
        count = await self._storage.remove_by_uri(_CONTEXT_COLLECTION, uri)

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
    ) -> FindResult:
        """
        Search for relevant contexts.

        Performs hierarchical retrieval across memory/resource/skill types.
        If a specific context_type or target_uri is given, narrows the scope.

        Args:
            query: Natural language query.
            context_type: Restrict to a specific type (memory/resource/skill).
            target_uri: Restrict search to a directory subtree.
            limit: Maximum results per type.
            score_threshold: Minimum relevance score.
            metadata_filter: Additional filter conditions.

        Returns:
            FindResult with memories, resources, and skills.
        """
        self._ensure_init()

        if context_type:
            types_to_search = [context_type]
        elif target_uri:
            types_to_search = [self._infer_context_type(target_uri)]
        else:
            types_to_search = [
                ContextType.MEMORY,
                ContextType.RESOURCE,
                ContextType.SKILL,
            ]

        typed_queries = [
            TypedQuery(
                query=query,
                context_type=ct,
                intent="",
                priority=1,
                target_directories=[target_uri] if target_uri else [],
            )
            for ct in types_to_search
        ]

        query_results = await asyncio.gather(
            *[
                self._retriever.retrieve(
                    tq,
                    limit=limit,
                    score_threshold=score_threshold,
                    metadata_filter=metadata_filter,
                )
                for tq in typed_queries
            ]
        )

        return self._aggregate_results(query_results)

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
    # SONA Reinforcement Face
    # =========================================================================

    async def feedback(self, uri: str, reward: float) -> None:
        """
        Submit a reward signal for a context (SONA reinforcement).

        Positive rewards reinforce retrieval; negative rewards penalize it.
        The reinforced score formula:
            reinforced_score = similarity * (1 + alpha * reward_factor) * decay_factor

        Args:
            uri: URI of the context.
            reward: Scalar reward value (positive = good, negative = bad).
        """
        self._ensure_init()

        # Find the record ID for this URI
        records = await self._storage.filter(
            _CONTEXT_COLLECTION,
            {"op": "must", "field": "uri", "conds": [uri]},
            limit=1,
        )
        if not records:
            logger.warning("[MemoryOrchestrator] feedback: URI not found: %s", uri)
            return

        record_id = records[0].get("id", "")
        if not record_id:
            return

        # Send reward to SONA via RuVector adapter
        if hasattr(self._storage, "update_reward"):
            await self._storage.update_reward(_CONTEXT_COLLECTION, record_id, reward)
            logger.info(
                "[MemoryOrchestrator] Feedback sent: uri=%s, reward=%s",
                uri,
                reward,
            )
        else:
            logger.debug(
                "[MemoryOrchestrator] Storage backend does not support SONA rewards"
            )

        # Also update activity count
        ctx_data = records[0]
        active_count = ctx_data.get("active_count", 0)
        await self._storage.update(
            _CONTEXT_COLLECTION,
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
        Trigger time-decay across all records (SONA).

        Normal nodes decay at rate=0.95, protected nodes at rate=0.99.
        Records below threshold (0.01) may be archived.

        Returns:
            Decay summary dict, or None if backend doesn't support SONA.
        """
        self._ensure_init()

        if hasattr(self._storage, "apply_decay"):
            result = await self._storage.apply_decay()
            logger.info("[MemoryOrchestrator] Decay applied: %s", result)
            return {
                "records_processed": result.records_processed,
                "records_decayed": result.records_decayed,
                "records_below_threshold": result.records_below_threshold,
                "records_archived": result.records_archived,
            }
        logger.debug("[MemoryOrchestrator] Storage backend does not support decay")
        return None

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
            _CONTEXT_COLLECTION,
            {"op": "must", "field": "uri", "conds": [uri]},
            limit=1,
        )
        if not records:
            logger.warning("[MemoryOrchestrator] protect: URI not found: %s", uri)
            return

        record_id = records[0].get("id", "")
        if hasattr(self._storage, "set_protected"):
            await self._storage.set_protected(
                _CONTEXT_COLLECTION, record_id, protected
            )
            logger.info(
                "[MemoryOrchestrator] Set protected=%s for: %s", protected, uri
            )

    async def get_profile(self, uri: str) -> Optional[Dict[str, Any]]:
        """
        Get the SONA behavior profile for a context.

        Returns:
            Profile dict with reward_score, retrieval_count, feedback counts,
            effective_score, is_protected. None if not found.
        """
        self._ensure_init()

        records = await self._storage.filter(
            _CONTEXT_COLLECTION,
            {"op": "must", "field": "uri", "conds": [uri]},
            limit=1,
        )
        if not records:
            return None

        record_id = records[0].get("id", "")
        if hasattr(self._storage, "get_profile"):
            profile = await self._storage.get_profile(
                _CONTEXT_COLLECTION, record_id
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
    # Native Self-Learning (RuVector Hooks)
    # =========================================================================

    async def hooks_learn(
        self,
        state: str,
        action: str,
        reward: float,
        available_actions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Record a learning outcome using RuVector native hooks (Q-learning).

        Maps OpenCortex concepts to RuVector hooks:
        - state: URI or context identifier
        - action: context_type (memory/skill/resource)
        - reward: feedback signal (-1 to 1)

        Args:
            state: Current state (e.g., URI)
            action: Action taken (e.g., "memory", "skill")
            reward: Reward value
            available_actions: List of available actions

        Returns:
            Dict with learning result
        """
        self._ensure_init()
        if not self._hooks:
            return {"success": False, "error": "Hooks not initialized"}

        result = await self._hooks.learn(
            state=state,
            action=action,
            reward=reward,
            available_actions=available_actions,
        )
        return {
            "success": result.success,
            "state": state,
            "best_action": result.best_action,
            "message": result.message,
        }

    async def hooks_remember(
        self,
        content: str,
        memory_type: str = "general",
    ) -> Dict[str, Any]:
        """
        Store content in RuVector semantic memory.

        Args:
            content: Content to remember
            memory_type: Type of memory

        Returns:
            Dict with remember result
        """
        self._ensure_init()
        if not self._hooks:
            return {"success": False, "error": "Hooks not initialized"}

        return await self._hooks.remember(content=content, memory_type=memory_type)

    async def hooks_recall(
        self,
        query: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Search RuVector semantic memory.

        Args:
            query: Search query
            limit: Maximum results

        Returns:
            List of matching memories
        """
        self._ensure_init()
        if not self._hooks:
            return []

        return await self._hooks.recall(query=query, limit=limit)

    async def hooks_trajectory_begin(self, trajectory_id: str, initial_state: str) -> Dict[str, Any]:
        """Begin a learning trajectory."""
        self._ensure_init()
        if not self._hooks:
            return {"success": False, "error": "Hooks not initialized"}

        return await self._hooks.trajectory_begin(
            trajectory_id=trajectory_id,
            initial_state=initial_state,
        )

    async def hooks_trajectory_step(
        self,
        trajectory_id: str,
        action: str,
        reward: float,
        next_state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add a step to a trajectory."""
        self._ensure_init()
        if not self._hooks:
            return {"success": False, "error": "Hooks not initialized"}

        return await self._hooks.trajectory_step(
            trajectory_id=trajectory_id,
            action=action,
            reward=reward,
            next_state=next_state,
        )

    async def hooks_trajectory_end(
        self,
        trajectory_id: str,
        quality_score: float,
    ) -> Dict[str, Any]:
        """End a trajectory with quality score."""
        self._ensure_init()
        if not self._hooks:
            return {"success": False, "error": "Hooks not initialized"}

        return await self._hooks.trajectory_end(
            trajectory_id=trajectory_id,
            quality_score=quality_score,
        )

    async def hooks_error_record(self, error: str, fix: str, context: Optional[str] = None) -> Dict[str, Any]:
        """Record an error and its fix for learning."""
        self._ensure_init()
        if not self._hooks:
            return {"success": False, "error": "Hooks not initialized"}

        return await self._hooks.error_record(error=error, fix=fix, context=context)

    async def hooks_error_suggest(self, error: str) -> List[Dict[str, Any]]:
        """Get suggested fixes for an error."""
        self._ensure_init()
        if not self._hooks:
            return []

        return await self._hooks.error_suggest(error=error)

    async def hooks_stats(self) -> Dict[str, Any]:
        """Get RuVector hooks statistics."""
        self._ensure_init()
        if not self._hooks:
            return {"success": False, "error": "Hooks not initialized"}

        stats = await self._hooks.stats()
        return {
            "success": True,
            "q_learning_patterns": stats.q_learning_patterns,
            "vector_memories": stats.vector_memories,
            "learning_trajectories": stats.learning_trajectories,
            "error_patterns": stats.error_patterns,
        }

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def close(self) -> None:
        """Close storage and release resources."""
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
        return {
            "tenant_id": self._config.tenant_id,
            "user_id": self._config.user_id,
            "storage": storage_stats,
            "embedder": self._embedder.model_name if self._embedder else None,
            "has_llm": self._llm_completion is not None,
        }

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _auto_uri(self, context_type: str, category: str) -> str:
        """Generate a URI based on context type and category."""
        tid = self._config.tenant_id
        uid = self._config.user_id
        node_id = uuid4().hex[:12]

        if context_type == "memory":
            if category:
                return CortexURI.build_private(
                    tid, uid, "memories", category, node_id
                )
            return CortexURI.build_private(tid, uid, "memories", node_id)

        elif context_type == "skill":
            return CortexURI.build_shared(tid, "agent", "skills", node_id)

        elif context_type == "resource":
            if category:
                return CortexURI.build_shared(tid, "resources", category, node_id)
            return CortexURI.build_shared(tid, "resources", node_id)

        # Fallback: treat as memory
        return CortexURI.build_private(tid, uid, "memories", node_id)

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
        if "/memories" in uri:
            return ContextType.MEMORY
        elif "/skills" in uri:
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
                _CONTEXT_COLLECTION,
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

        # Create directory records from top down (so parent_uri links are valid)
        for dir_uri in reversed(to_create):
            dir_parent = self._derive_parent_uri(dir_uri)
            dir_ctx = Context(
                uri=dir_uri,
                parent_uri=dir_parent,
                is_leaf=False,
                abstract="",
                user=self._user,
            )

            # Embed the directory name as a minimal vector
            dir_name = dir_uri.rstrip("/").rsplit("/", 1)[-1]
            if self._embedder and dir_name:
                embed_result = self._embedder.embed(dir_name)
                dir_ctx.vector = embed_result.dense_vector

            record = dir_ctx.to_dict()
            if dir_ctx.vector:
                record["vector"] = dir_ctx.vector
            await self._storage.upsert(_CONTEXT_COLLECTION, record)
            logger.debug("[MemoryOrchestrator] Created directory record: %s", dir_uri)

    def _aggregate_results(
        self, query_results: List[QueryResult]
    ) -> FindResult:
        """Aggregate multiple QueryResults into a single FindResult."""
        memories, resources, skills = [], [], []

        for result in query_results:
            for ctx in result.matched_contexts:
                if ctx.context_type == ContextType.MEMORY:
                    memories.append(ctx)
                elif ctx.context_type == ContextType.RESOURCE:
                    resources.append(ctx)
                elif ctx.context_type == ContextType.SKILL:
                    skills.append(ctx)

        return FindResult(
            memories=memories,
            resources=resources,
            skills=skills,
        )
