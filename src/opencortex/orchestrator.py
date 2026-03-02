# SPDX-License-Identifier: Apache-2.0
"""
Memory Orchestrator for OpenCortex.

The orchestrator is the primary user-facing API that wires together all
internal components:

- CortexConfig: tenant/user isolation
- CortexFS: three-layer (L0/L1/L2) filesystem abstraction
- VikingDBInterface: vector storage (use OpenViking's native implementation)
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
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union
from uuid import uuid4

from opencortex.config import CortexConfig, get_config
from opencortex.http.request_context import get_effective_identity
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
    QueryResult,
    SearchIntent,
    TypedQuery,
)
from opencortex.ace.rule_extractor import RuleExtractor
from opencortex.storage.collection_schemas import init_context_collection
from opencortex.storage.cortex_fs import CortexFS, init_cortex_fs
from opencortex.storage.vikingdb_interface import VikingDBInterface
from opencortex.utils.uri import CortexURI

logger = logging.getLogger(__name__)

# Default collection name for all context types
_CONTEXT_COLLECTION = "context"


class MemoryOrchestrator:
    """
    Top-level orchestrator for OpenCortex memory operations.

    Wires together storage, filesystem, retrieval, embedding, and
    reinforcement learning into a single coherent API.

    Args:
        config: CortexConfig instance. Uses global config if not provided.
        storage: VikingDBInterface backend. Must be provided (use OpenViking's native).
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
        hooks: Optional[Any] = None,
    ):
        self._config = config or get_config()
        self._storage = storage
        self._embedder = embedder
        self._rerank_config = rerank_config or RerankConfig()
        self._llm_completion = llm_completion
        self._hooks = hooks

        self._fs: Optional[CortexFS] = None
        self._retriever: Optional[HierarchicalRetriever] = None
        self._analyzer: Optional[IntentAnalyzer] = None
        self._user: Optional[UserIdentifier] = None
        self._session_manager = None
        self._rule_extractor: Optional[RuleExtractor] = RuleExtractor()
        self._initialized = False

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
            _CONTEXT_COLLECTION,
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
        )

        # 7. Hooks — ACE (Agentic Context Engine) self-learning
        if self._hooks is None:
            from opencortex.ace.engine import ACEngine

            self._hooks = ACEngine(
                storage=self._storage,
                embedder=self._embedder,
                cortex_fs=self._fs,
                llm_fn=self._llm_completion,
            )
            await self._hooks.init()

        # 7b. Ensure full-text indexes on existing collections (idempotent)
        if hasattr(self._storage, "ensure_text_indexes"):
            await self._storage.ensure_text_indexes()

        # 8. Session manager for context self-iteration
        self._session_manager = self._create_session_manager()

        self._initialized = True
        logger.info("[MemoryOrchestrator] Initialized (data_root=%s)", self._config.data_root)
        return self

    def _create_default_embedder(self) -> Optional[EmbedderBase]:
        """
        Auto-create an embedder based on CortexConfig.

        Resolution order:
        1. If ``embedding_provider == "volcengine"`` in config, create a
           :class:`VolcengineDenseEmbedder` using config values.  The API key
           is taken from ``config.embedding_api_key`` first, then from the
           environment variable ``OPENCORTEX_EMBEDDING_API_KEY``.
        2. If ``embedding_provider == "openai"`` in config, create an
           :class:`OpenAIDenseEmbedder` (works with any OpenAI-compatible API).
        3. If no provider is configured (or the above attempts failed),
           try loading from ``~/.openviking/ov.conf`` via
           :func:`create_embedder_from_ov_conf`.
        4. If nothing works, log a warning and return ``None`` so that tests
           that supply their own mock embedder are not affected.

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
                return embedder
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

    def _create_default_hooks(self) -> None:
        """
        Hooks placeholder — will be replaced by ACE.

        Self-learning will be replaced by ACE (Agentic Context Engine)
        with playbook-based strategy evolution.

        Returns:
            None (hooks disabled).
        """
        return None

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
            max_candidates=getattr(base, "max_candidates", 20),
            use_llm_fallback=getattr(base, "use_llm_fallback", True),
        )

    async def _generate_overview(self, abstract: str, content: str) -> str:
        """Generate L1 overview from content.

        Strategy:
        - No content -> empty
        - Short content (<=500 chars) -> use content as-is
        - Long content + LLM available -> LLM summarization
        - Long content + no LLM -> truncated paragraphs
        """
        if not content:
            return ""
        if len(content) <= 500:
            return content
        if self._llm_completion:
            prompt = (
                "Generate a concise paragraph overview (3-8 sentences) "
                "of the following content. Focus on key facts, decisions, "
                "and actionable details.\n\n"
                f"Title: {abstract}\n\nContent:\n{content[:4000]}\n\nOverview:"
            )
            try:
                overview = await self._llm_completion(prompt)
                if overview and len(overview.strip()) > 10:
                    return overview.strip()
            except Exception as e:
                logger.warning("[Orchestrator] L1 generation failed: %s", e)
        # Fallback: truncate by paragraphs
        paragraphs = content.split("\n\n")
        truncated, total = [], 0
        for p in paragraphs:
            p = p.strip()
            if not p:
                continue
            if total + len(p) > 500:
                break
            truncated.append(p)
            total += len(p)
        return "\n\n".join(truncated) if truncated else content[:500] + "..."

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

        # Generate L1 overview if not provided and content exists
        if not overview and content and is_leaf:
            overview = await self._generate_overview(abstract, content)

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

        # Embed (offload sync embedder to thread so we don't block the loop)
        if self._embedder:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, self._embedder.embed, ctx.get_vectorization_text()
            )
            ctx.vector = result.dense_vector

        # Ensure parent directory records exist in vector DB
        if is_leaf and parent_uri:
            await self._ensure_parent_records(parent_uri)

        # Store in vector DB
        record = ctx.to_dict()
        if ctx.vector:
            record["vector"] = ctx.vector
        await self._storage.upsert(_CONTEXT_COLLECTION, record)

        # Write to filesystem (L0 abstract + L1 overview + L2 content)
        await self._fs.write_context(
            uri=uri,
            content=content,
            abstract=abstract,
            overview=overview,
            is_leaf=is_leaf,
        )

        # Async skill extraction (non-blocking)
        if self._rule_extractor and self._hooks and content:
            asyncio.create_task(self._try_extract_skills(abstract, content))

        logger.info("[MemoryOrchestrator] Added context: %s", uri)
        return ctx

    async def _try_extract_skills(self, abstract: str, content: str) -> None:
        """Background skill extraction from stored content. Failures are silent."""
        try:
            skills = self._rule_extractor.extract(abstract, content)
            for skill in skills:
                # Dedup: check if a similar skill already exists
                existing = await self._hooks.recall(skill.content, limit=1)
                if existing and existing[0].get("score", 0) > 0.85:
                    continue
                await self._hooks.remember(skill.content, skill.section)
                logger.debug(
                    "[Orchestrator] Extracted skill: %s → %s",
                    skill.section, skill.content[:60],
                )
        except Exception:
            logger.debug("[Orchestrator] Skill extraction failed silently")

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
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, self._embedder.embed, abstract
                )
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
        detail_level: str = "l1",
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

        Returns:
            FindResult with memories, resources, and skills.
        """
        self._ensure_init()

        # Intent Router determines retrieval strategy
        router = IntentRouter(llm_completion=self._llm_completion)
        intent = await router.route(query, context_type)

        # Gate: skip retrieval if intent says no recall needed
        if not intent.should_recall:
            logger.debug("[search] should_recall=False, returning empty result")
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
                types_to_search = [
                    ContextType.MEMORY,
                    ContextType.RESOURCE,
                    ContextType.SKILL,
                ]

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

        # Set target directories on queries if specified
        if target_uri:
            for tq in typed_queries:
                if not tq.target_directories:
                    tq.target_directories = [target_uri]

        # Build retrieval coroutines
        retrieval_coros = [
            self._retriever.retrieve(
                tq,
                limit=effective_limit,
                score_threshold=score_threshold,
                metadata_filter=metadata_filter,
            )
            for tq in typed_queries
        ]

        # Parallel skillbook search (if hooks available)
        skill_search_coro = self._search_skillbook(query, limit=3) if self._hooks else None

        if skill_search_coro:
            all_results = await asyncio.gather(*retrieval_coros, skill_search_coro)
            query_results = list(all_results[:-1])
            skill_contexts = all_results[-1]
        else:
            query_results = list(await asyncio.gather(*retrieval_coros))
            skill_contexts = []

        result = self._aggregate_results(query_results)

        # Merge skillbook results (deduplicate by URI)
        if skill_contexts:
            existing_uris = {s.uri for s in result.skills}
            for sc in skill_contexts:
                if sc.uri not in existing_uris:
                    result.skills.append(sc)
                    existing_uris.add(sc.uri)
            result.total = len(result.memories) + len(result.resources) + len(result.skills)

        result.search_intent = intent

        # Async update access stats for returned results (fire-and-forget)
        all_matched = result.memories + result.resources + result.skills
        if all_matched:
            record_ids = []
            for mc in all_matched:
                try:
                    recs = await self._storage.filter(
                        _CONTEXT_COLLECTION,
                        {"op": "must", "field": "uri", "conds": [mc.uri]},
                        limit=1,
                    )
                    if recs:
                        rid = recs[0].get("id", "")
                        if rid:
                            record_ids.append(rid)
                except Exception:
                    pass
            if record_ids:
                asyncio.create_task(self._update_access_stats(record_ids))

        return result

    async def _update_access_stats(self, record_ids: list) -> None:
        """Async batch update access_count + accessed_at for retrieved records.

        Called as fire-and-forget task after search returns Top-K.
        Failures are logged but do not affect search results.
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for record_id in record_ids:
            try:
                records = await self._storage.get(_CONTEXT_COLLECTION, [record_id])
                if records:
                    count = records[0].get("active_count", 0)
                    await self._storage.update(
                        _CONTEXT_COLLECTION,
                        record_id,
                        {"active_count": count + 1, "accessed_at": now},
                    )
            except Exception as exc:
                logger.debug(
                    "[Orchestrator] Access stats update failed for %s: %s",
                    record_id, exc,
                )

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

        # If URI points to a skillbook entry, update skill tag directly
        if "/skillbooks/" in uri and self._hooks:
            skill_id = uri.rsplit("/", 1)[-1]
            tag = "helpful" if reward > 0 else ("harmful" if reward < 0 else "neutral")
            try:
                await self._hooks.skillbook.tag_skill(skill_id, tag)
                logger.info(
                    "[MemoryOrchestrator] Skillbook feedback: skill=%s, tag=%s",
                    skill_id, tag,
                )
            except Exception:
                logger.debug("[Orchestrator] Skillbook tag update failed for %s", uri)
            return

        # Find the record ID for this URI in context collection
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

        # Send reward via storage adapter
        if hasattr(self._storage, "update_reward"):
            await self._storage.update_reward(_CONTEXT_COLLECTION, record_id, reward)
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
        Get the reinforcement learning profile for a context.

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
    # Native Self-Learning (Hooks — placeholder)
    # =========================================================================

    async def hooks_learn(
        self,
        state: str,
        action: str,
        reward: float,
        available_actions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Record a learning outcome using hooks (Q-learning).

        Maps OpenCortex concepts to hooks:
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

        tid, uid = get_effective_identity()
        result = await self._hooks.learn(
            state=state,
            action=action,
            reward=reward,
            available_actions=available_actions,
            tenant_id=tid,
            user_id=uid,
        )
        return {
            "success": result.success,
            "state": state,
            "best_action": result.best_action,
            "message": result.message,
            "operations_applied": result.operations_applied,
            "reflection_key_insight": result.reflection_key_insight,
        }

    async def hooks_remember(
        self,
        content: str,
        memory_type: str = "general",
    ) -> Dict[str, Any]:
        """
        Store content in semantic memory.

        Args:
            content: Content to remember
            memory_type: Type of memory

        Returns:
            Dict with remember result
        """
        self._ensure_init()
        if not self._hooks:
            return {"success": False, "error": "Hooks not initialized"}

        tid, uid = get_effective_identity()
        return await self._hooks.remember(
            content=content, memory_type=memory_type, tenant_id=tid, user_id=uid,
        )

    async def hooks_recall(
        self,
        query: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Search semantic memory.

        Args:
            query: Search query
            limit: Maximum results

        Returns:
            List of matching memories
        """
        self._ensure_init()
        if not self._hooks:
            return []

        tid, uid = get_effective_identity()
        return await self._hooks.recall(query=query, limit=limit, tenant_id=tid, user_id=uid)

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

        tid, uid = get_effective_identity()
        return await self._hooks.trajectory_end(
            trajectory_id=trajectory_id,
            quality_score=quality_score,
            tenant_id=tid,
            user_id=uid,
        )

    async def hooks_error_record(self, error: str, fix: str, context: Optional[str] = None) -> Dict[str, Any]:
        """Record an error and its fix for learning."""
        self._ensure_init()
        if not self._hooks:
            return {"success": False, "error": "Hooks not initialized"}

        tid, uid = get_effective_identity()
        return await self._hooks.error_record(
            error=error, fix=fix, context=context, tenant_id=tid, user_id=uid,
        )

    async def hooks_error_suggest(self, error: str) -> List[Dict[str, Any]]:
        """Get suggested fixes for an error."""
        self._ensure_init()
        if not self._hooks:
            return []

        tid, uid = get_effective_identity()
        return await self._hooks.error_suggest(error=error, tenant_id=tid, user_id=uid)

    async def hooks_stats(self) -> Dict[str, Any]:
        """Get hooks statistics."""
        self._ensure_init()
        if not self._hooks:
            return {"success": False, "error": "Hooks not initialized"}

        tid, uid = get_effective_identity()
        stats = await self._hooks.stats(tenant_id=tid, user_id=uid)
        return {
            "success": True,
            "q_learning_patterns": stats.q_learning_patterns,
            "vector_memories": stats.vector_memories,
            "learning_trajectories": stats.learning_trajectories,
            "error_patterns": stats.error_patterns,
        }

    # =========================================================================
    # Skill Approval & Demotion
    # =========================================================================

    async def hooks_list_candidates(self) -> List[Dict[str, Any]]:
        """List candidate skills awaiting review for the current tenant."""
        self._ensure_init()
        if not self._hooks:
            return []
        tid, _uid = get_effective_identity()
        return await self._hooks.list_candidates(tenant_id=tid)

    async def hooks_review_skill(
        self,
        skill_id: str,
        decision: str,
    ) -> Dict[str, Any]:
        """Approve or reject a candidate skill."""
        self._ensure_init()
        if not self._hooks:
            return {"success": False, "error": "Hooks not initialized"}
        tid, uid = get_effective_identity()
        return await self._hooks.review_skill(
            skill_id=skill_id, decision=decision, tenant_id=tid, user_id=uid,
        )

    async def hooks_demote_skill(
        self,
        skill_id: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Demote a shared skill back to private."""
        self._ensure_init()
        if not self._hooks:
            return {"success": False, "error": "Hooks not initialized"}
        tid, uid = get_effective_identity()
        return await self._hooks.demote_skill(
            skill_id=skill_id, reason=reason, tenant_id=tid, user_id=uid,
        )

    async def hooks_migrate_legacy(self) -> Dict[str, Any]:
        """Migrate legacy skills that lack scope fields."""
        self._ensure_init()
        if not self._hooks:
            return {"success": False, "error": "Hooks not initialized"}
        tid, uid = get_effective_identity()
        return await self._hooks.migrate_legacy_skills(tenant_id=tid, user_id=uid)

    # =========================================================================
    # Hooks Integration (Route through MCP → CortexFS)
    # =========================================================================

    async def hooks_route(
        self,
        task: str,
        agents: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Route a task to the best agent based on learned patterns.

        Uses memory search + Q-learning patterns to recommend the best agent.
        """
        self._ensure_init()

        # Search for relevant memories about this task type
        result = await self.search(query=task, limit=3)
        context_hints = [m.abstract for m in result.memories[:3]]

        # Use hooks Q-learning if available
        best_action = None
        if self._hooks:
            try:
                recall_results = await self._hooks.recall(query=task, limit=3)
                if recall_results:
                    context_hints.extend(
                        r.get("content", "") for r in recall_results[:2]
                    )
            except Exception:
                pass

        return {
            "task": task,
            "agents": agents or [],
            "context_hints": context_hints,
            "recommended_agent": best_action,
        }

    async def hooks_init(self, project_path: str = ".") -> Dict[str, Any]:
        """Initialize hooks configuration for a project."""
        self._ensure_init()
        tid, uid = get_effective_identity()
        return {
            "status": "ok",
            "project_path": project_path,
            "tenant_id": tid,
            "user_id": uid,
            "http_server_host": self._config.http_server_host,
            "http_server_port": self._config.http_server_port,
        }

    async def hooks_pretrain(self, repo_path: str = ".") -> Dict[str, Any]:
        """Pre-train from repository content."""
        self._ensure_init()

        if self._hooks:
            try:
                result = await self._hooks.remember(
                    content=f"Repository at {repo_path}",
                    memory_type="project_structure",
                )
                return {"status": "ok", "repo_path": repo_path, "remember_result": result}
            except Exception as exc:
                return {"status": "partial", "repo_path": repo_path, "error": str(exc)}

        return {"status": "ok", "repo_path": repo_path, "note": "No hooks backend, skipping pretrain"}

    async def hooks_verify(self) -> Dict[str, Any]:
        """Verify hooks configuration."""
        self._ensure_init()
        health = await self.health_check()
        hooks_ok = self._hooks is not None
        return {
            "status": "ok" if health.get("storage") and hooks_ok else "degraded",
            "storage": health.get("storage", False),
            "embedder": health.get("embedder", False),
            "llm": health.get("llm", False),
            "hooks": hooks_ok,
            "session_manager": self._session_manager is not None,
        }

    async def hooks_doctor(self) -> Dict[str, Any]:
        """Run diagnostics on the OpenCortex system."""
        self._ensure_init()
        health = await self.health_check()
        stats = await self.stats()

        issues = []
        if not health.get("storage"):
            issues.append("Storage backend not reachable")
        if not health.get("embedder"):
            issues.append("No embedder configured — search will not work")
        if not health.get("llm"):
            issues.append("No LLM configured — intent analysis and session extraction disabled")
        if not self._hooks:
            issues.append("Hooks not initialized — native self-learning disabled")

        return {
            "status": "healthy" if not issues else "issues_found",
            "health": health,
            "stats": stats,
            "issues": issues,
        }

    async def hooks_export(self, format: str = "json") -> Dict[str, Any]:
        """Export intelligence data."""
        self._ensure_init()
        stats = await self.stats()

        hooks_stats = {}
        if self._hooks:
            try:
                hooks_stats = await self.hooks_stats()
            except Exception:
                pass

        export_data = {
            "format": format,
            "orchestrator_stats": stats,
            "hooks_stats": hooks_stats,
        }
        return export_data

    async def hooks_build_agents(self) -> Dict[str, Any]:
        """Generate agent configurations based on learned patterns."""
        self._ensure_init()

        # Search for skill memories to inform agent configuration
        result = await self.search(query="agent skills capabilities", context_type=None, limit=10)
        skills = [{"uri": m.uri, "abstract": m.abstract, "score": m.score} for m in result.skills]

        return {
            "agents": [
                {
                    "name": "memory-agent",
                    "description": "Manages persistent memory storage and retrieval",
                    "tools": ["memory_store", "memory_search", "memory_feedback"],
                },
                {
                    "name": "session-agent",
                    "description": "Manages session lifecycle and memory extraction",
                    "tools": ["session_begin", "session_message", "session_end"],
                },
            ],
            "learned_skills": skills,
        }

    # =========================================================================
    # Session Management (Context Self-Iteration)
    # =========================================================================

    def _create_session_manager(self):
        """Create a SessionManager wired to the orchestrator's storage pipeline."""
        from opencortex.session.manager import SessionManager

        async def _store_fn(abstract: str, content: str = "", category: str = "", context_type: str = "memory"):
            await self.add(abstract=abstract, content=content, category=category, context_type=context_type)

        async def _search_fn(query: str):
            result = await self.search(query=query, limit=3, score_threshold=0.5)
            items = []
            for m in result:
                items.append({"uri": m.uri, "score": m.score, "content": m.abstract})
            return items

        async def _update_fn(uri: str, abstract: str, content: str):
            await self.update(uri=uri, abstract=abstract, content=content)

        async def _feedback_fn(uri: str, reward: float):
            await self.feedback(uri=uri, reward=reward)

        return SessionManager(
            llm_completion=self._llm_completion,
            store_fn=_store_fn,
            search_fn=_search_fn,
            update_fn=_update_fn,
            feedback_fn=_feedback_fn,
        )

    async def session_begin(
        self,
        session_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Begin a new session for context self-iteration.

        Args:
            session_id: Unique session identifier.
            meta: Optional metadata.

        Returns:
            Dict with session info.
        """
        self._ensure_init()
        tid, uid = get_effective_identity()
        ctx = await self._session_manager.begin(
            session_id=session_id,
            tenant_id=tid,
            user_id=uid,
            meta=meta,
        )
        return {
            "session_id": ctx.session_id,
            "started_at": ctx.started_at,
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

        Args:
            session_id: Session identifier.
            role: Message role.
            content: Message content.
            meta: Optional metadata.

        Returns:
            Dict with message count.
        """
        self._ensure_init()
        ok = await self._session_manager.add_message(session_id, role, content, meta)
        ctx = self._session_manager.get_session(session_id)
        return {
            "added": ok,
            "message_count": len(ctx.messages) if ctx else 0,
        }

    async def session_end(
        self,
        session_id: str,
        quality_score: float = 0.5,
    ) -> Dict[str, Any]:
        """End a session and trigger memory extraction.

        Performs LLM-driven memory analysis, deduplication, and storage.

        Args:
            session_id: Session to end.
            quality_score: Session quality (0-1).

        Returns:
            Dict with extraction results.
        """
        self._ensure_init()
        result = await self._session_manager.end(session_id, quality_score)
        return {
            "session_id": result.session_id,
            "stored_count": result.stored_count,
            "merged_count": result.merged_count,
            "skipped_count": result.skipped_count,
            "quality_score": result.quality_score,
            "total_extracted": len(result.memories),
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

    def _auto_uri(self, context_type: str, category: str) -> str:
        """Generate a URI based on context type and category."""
        tid, uid = get_effective_identity()
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
            if self._embedder and dir_name:
                loop = asyncio.get_event_loop()
                embed_result = await loop.run_in_executor(
                    None, self._embedder.embed, dir_name
                )
                dir_ctx.vector = embed_result.dense_vector

            record = dir_ctx.to_dict()
            if dir_ctx.vector:
                record["vector"] = dir_ctx.vector
            await self._storage.upsert(_CONTEXT_COLLECTION, record)
            logger.debug("[MemoryOrchestrator] Created directory record: %s", dir_uri)

    async def _search_skillbook(self, query: str, limit: int = 3) -> List[MatchedContext]:
        """Search ACE Skillbook for relevant skills.

        Returns MatchedContext list for merging into FindResult.
        """
        try:
            raw_skills = await self._hooks.recall(query, limit=limit)
            results = []
            for s in raw_skills:
                results.append(MatchedContext(
                    uri=s.get("uri", f"skillbook://{s.get('skill_id', '')}"),
                    context_type=ContextType.SKILL,
                    is_leaf=True,
                    abstract=s.get("content", ""),
                    score=s.get("score", 0.0),
                    category=s.get("section", ""),
                    match_reason="skillbook",
                ))
            return results
        except Exception:
            logger.debug("[Orchestrator] Skillbook search failed silently")
            return []

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
