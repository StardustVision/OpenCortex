# SPDX-License-Identifier: Apache-2.0
"""
Hierarchical retriever for OpenCortex.

Implements directory-based hierarchical retrieval with recursive search
and rerank-based relevance scoring.
"""

import asyncio
import math
import time

import heapq
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from opencortex.models.embedder.base import EmbedResult
from opencortex.retrieve.rerank_config import RerankConfig
from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
    MatchedContext,
    QueryResult,
    RelatedContext,
    TypedQuery,
)
from opencortex.storage import StorageInterface

logger = logging.getLogger(__name__)


def _get_cortex_fs():
    """Lazily import get_cortex_fs to avoid circular imports and allow parallel porting."""
    try:
        from opencortex.storage.cortex_fs import get_cortex_fs

        return get_cortex_fs()
    except (ImportError, RuntimeError):
        return None


class RetrieverMode(str):
    THINKING = "thinking"
    QUICK = "quick"


class HierarchicalRetriever:
    """Hierarchical retriever with dense and sparse vector support."""

    MAX_CONVERGENCE_ROUNDS = 3  # Stop after multiple rounds with unchanged topk
    MAX_RELATIONS = 5  # Maximum relations per resource
    SCORE_PROPAGATION_ALPHA = 0.5  # Score propagation coefficient
    DIRECTORY_DOMINANCE_RATIO = 1.2  # Directory score must exceed max child score
    GLOBAL_SEARCH_TOPK = 3  # Global retrieval count

    # Frontier batching constants
    MAX_FRONTIER_SIZE = 64          # Max directories per wave (prevents oversized IN)
    MIN_CHILDREN_PER_DIR = 2        # Min guaranteed children per parent directory
    LATE_RERANK_FACTOR = 2          # Late rerank candidate multiplier
    LATE_RERANK_CAP = 20            # Late rerank candidate cap
    DEFAULT_MAX_WAVES = 8           # Default max wave iterations

    def __init__(
        self,
        storage: StorageInterface,
        embedder: Optional[Any],
        rerank_config: Optional[RerankConfig] = None,
        llm_completion: Optional[Any] = None,
        reward_weight: float = 0.05,
        hot_weight: float = 0.03,
        use_frontier_batching: bool = True,
        max_waves: int = 8,
        embed_timeout: float = 2.0,
        flat_rerank_multiplier: int = 5,
        force_flat_search: bool = False,
    ):
        """Initialize hierarchical retriever with rerank_config.

        Args:
            storage: StorageInterface instance
            embedder: Embedder instance (supports dense/sparse/hybrid)
            rerank_config: Rerank configuration (optional, will fallback to vector search only)
            llm_completion: Async LLM callable for RerankClient LLM fallback
            hot_weight: Weight for hotness scoring in final score fusion.
        """
        self.storage = storage
        self.embedder = embedder
        self.rerank_config = rerank_config

        # Use rerank threshold if available, otherwise use a default
        self.threshold = rerank_config.threshold if rerank_config else 0

        self._reward_weight = reward_weight
        self._hot_weight = hot_weight
        self._use_frontier_batching = use_frontier_batching
        self._max_waves = max_waves
        self._embed_timeout = embed_timeout
        self._flat_rerank_multiplier = flat_rerank_multiplier
        self._force_flat_search = force_flat_search

        # Initialize rerank client only if config is available
        if rerank_config and rerank_config.is_available():
            from opencortex.retrieve.rerank_client import RerankClient

            self._rerank_client = RerankClient(rerank_config, llm_completion=llm_completion)
            self._fusion_beta = rerank_config.fusion_beta
            logger.info(
                "[HierarchicalRetriever] RerankClient active (mode=%s, beta=%.2f, threshold=%s)",
                self._rerank_client.mode,
                self._fusion_beta,
                self.threshold,
            )
        else:
            self._rerank_client = None
            self._fusion_beta = 0.0
            logger.info(
                "[HierarchicalRetriever] Rerank not configured, using vector search only with threshold=%s",
                self.threshold,
            )

        # Score gap threshold for conditional rerank
        self._score_gap_threshold = (
            rerank_config.score_gap_threshold if rerank_config else 0.15
        )

    # Half-life of 7 days: λ = ln(2)/7
    _HOTNESS_LAMBDA = math.log(2) / 7.0

    @staticmethod
    def _compute_hotness(record: Dict[str, Any]) -> float:
        """Compute hotness score from access frequency and recency.

        Formula: sigmoid(log1p(active_count)) * exp(-λ * age_days)
        where λ = ln(2)/7 gives a 7-day half-life.
        """
        active_count = 0
        try:
            active_count = int(record.get("active_count", 0))
        except (TypeError, ValueError):
            pass

        # Parse accessed_at for age calculation
        age_days = 30.0  # default if no timestamp
        accessed_at = record.get("accessed_at", "")
        if accessed_at:
            try:
                if isinstance(accessed_at, str):
                    # ISO format: "2026-03-02T10:00:00Z" or similar
                    ts = datetime.fromisoformat(accessed_at.replace("Z", "+00:00"))
                else:
                    ts = accessed_at
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                delta = datetime.now(timezone.utc) - ts
                age_days = max(0.0, delta.total_seconds() / 86400.0)
            except (ValueError, TypeError, AttributeError):
                pass

        # sigmoid(log1p(active_count)) gives a 0..1 frequency signal
        freq = 1.0 / (1.0 + math.exp(-math.log1p(active_count)))
        # Exponential decay with 7-day half-life
        recency = math.exp(-HierarchicalRetriever._HOTNESS_LAMBDA * age_days)
        return freq * recency

    async def _embed_with_timeout(self, text: str):
        """Embed text with server-side timeout.

        Uses run_in_executor (embedder.embed is synchronous) +
        asyncio.wait_for for timeout control.  Returns None on timeout
        so caller can fall back to lexical search.
        """
        if not self.embedder:
            return None
        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self.embedder.embed_query, text),
                timeout=self._embed_timeout,
            )
            return result
        except asyncio.TimeoutError:
            logger.warning(
                "[HierarchicalRetriever] Embedding timeout (%.1fs), "
                "falling back to lexical search",
                self._embed_timeout,
            )
            return None
        except Exception as exc:
            logger.warning(
                "[HierarchicalRetriever] Embedding error: %s, "
                "falling back to lexical search",
                exc,
            )
            return None

    async def retrieve(
        self,
        query: TypedQuery,
        limit: int = 5,
        mode: RetrieverMode = RetrieverMode.THINKING,
        score_threshold: Optional[float] = None,
        score_gte: bool = False,
        metadata_filter: Optional[Dict[str, Any]] = None,
        lexical_boost: float = 0.3,
        classification: Optional[Any] = None,
    ) -> QueryResult:
        """
        Execute hierarchical retrieval.

        Args:
            query: TypedQuery with query text and context type
            limit: Maximum number of results to return
            mode: Retriever mode (thinking or quick)
            score_threshold: Custom score threshold (overrides config)
            score_gte: True uses >=, False uses >
            metadata_filter: Additional metadata filter conditions
        """

        # Use custom threshold or default threshold
        effective_threshold = score_threshold if score_threshold is not None else self.threshold

        # v0.6: SearchExplain timing markers (always set to avoid scoping issues)
        t_start = time.perf_counter()
        t_embed = t_start
        t_search = t_start
        t_rerank = t_start
        t_assemble = t_start
        _rerank_ms_flat = 0.0

        collection = self._type_to_collection(query.context_type)

        target_dirs = [d for d in (query.target_directories or []) if d]

        # v0.6: Document scope filter — narrow search to a specific source document
        if query.target_doc_id and getattr(getattr(self, '_config', None), 'doc_scope_search_enabled', True):
            doc_filter = {"op": "match", "field": "source_doc_id", "value": query.target_doc_id}
            if metadata_filter:
                metadata_filter = {"op": "and", "conds": [metadata_filter, doc_filter]}
            else:
                metadata_filter = doc_filter

        # v0.6: Time filter — narrow search to recent/today/session time window
        time_filter_active = False
        if (classification and getattr(classification, 'time_filter_hint', None)
                and getattr(getattr(self, '_config', None), 'time_filter_enabled', True)):
            from datetime import datetime, timedelta
            hint = classification.time_filter_hint
            time_filter = None

            if hint == "recent":
                cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat() + "Z"
                time_filter = {"op": "range", "field": "created_at", "gte": cutoff}
            elif hint == "today":
                cutoff = datetime.utcnow().replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).isoformat() + "Z"
                time_filter = {"op": "range", "field": "created_at", "gte": cutoff}
            elif hint == "session" and getattr(query, 'session_id', None):
                time_filter = {"op": "match", "field": "session_id", "value": query.session_id}

            if time_filter:
                time_filter_active = True
                if metadata_filter:
                    metadata_filter = {"op": "and", "conds": [metadata_filter, time_filter]}
                else:
                    metadata_filter = time_filter

        # Merge all filters
        filters_to_merge = []
        # Only apply context_type filter for specific type queries (skip for ANY)
        if query.context_type != ContextType.ANY:
            type_filter = {"op": "must", "field": "context_type", "conds": [query.context_type.value]}
            filters_to_merge.append(type_filter)
        if target_dirs:
            target_filter = {
                "op": "or",
                "conds": [
                    {"op": "prefix", "field": "uri", "prefix": target_dir}
                    for target_dir in target_dirs
                ],
            }
            filters_to_merge.append(target_filter)
        if metadata_filter:
            filters_to_merge.append(metadata_filter)

        final_metadata_filter = {"op": "and", "conds": filters_to_merge}

        if not await self.storage.collection_exists(collection):
            logger.warning(f"[RecursiveSearch] Collection {collection} does not exist")
            return QueryResult(
                query=query,
                matched_contexts=[],
                searched_directories=[],
            )

        # Generate query vectors with timeout protection
        # Use HyDE text for dense embedding when available; keep original for lexical/rerank
        query_vector = None
        sparse_query_vector = None
        text_query = query.query  # Always original for lexical + rerank
        embed_text = getattr(query, "hyde_text", None) or query.query
        _t0 = asyncio.get_event_loop().time()
        if self.embedder:
            result = await self._embed_with_timeout(embed_text)
            if result:
                query_vector = result.dense_vector
                sparse_query_vector = result.sparse_vector
            # If result is None (timeout/error), query_vector stays None
            # and text_query will trigger lexical fallback in adapter
        t_embed = time.perf_counter()

        # Step 1: Determine starting directories based on target_directories or context_type
        if target_dirs:
            root_uris = target_dirs
        else:
            root_uris = self._get_root_uris_for_type(query.context_type)

        # No-embedder fallback: pure filter/scroll (no semantic ranking)
        if not self.embedder:
            results = await self.storage.search(
                collection=collection,
                query_vector=None,
                filter=final_metadata_filter,
                limit=limit,
                text_query=text_query,
            )
            # Apply reward + hotness boost to scroll results
            for r in results:
                reward = r.get("reward_score", 0.0)
                if reward != 0 and self._reward_weight:
                    r["_score"] = r.get("_score", 0.0) + self._reward_weight * reward
                if self._hot_weight:
                    r["_score"] = r.get("_score", 0.0) + self._hot_weight * self._compute_hotness(r)
            results.sort(key=lambda r: r.get("_score", 0.0), reverse=True)
            t_search = time.perf_counter()
            t_rerank = t_search
            matched = await self._convert_to_matched_contexts(
                results[:limit], query.context_type, query.detail_level,
            )
            t_assemble = time.perf_counter()
            qr = QueryResult(
                query=query,
                matched_contexts=matched,
                searched_directories=root_uris,
            )
            if getattr(getattr(self, '_config', None), 'explain_enabled', True):
                from opencortex.retrieve.types import SearchExplain
                qr.explain = SearchExplain(
                    query_class=getattr(query, 'intent', '') or '',
                    path="no_embedder",
                    intent_ms=0.0,
                    embed_ms=(t_embed - t_start) * 1000,
                    search_ms=(t_search - t_embed) * 1000,
                    rerank_ms=0.0,
                    assemble_ms=(t_assemble - t_rerank) * 1000,
                    doc_scope_hit=bool(getattr(query, 'target_doc_id', None)),
                    time_filter_hit=time_filter_active,
                    candidates_before_rerank=len(results),
                    candidates_after_rerank=len(matched),
                    frontier_waves=0,
                    frontier_budget_exceeded=False,
                    total_ms=(t_assemble - t_start) * 1000,
                )
            return qr

        _t1 = asyncio.get_event_loop().time()
        logger.info("[Retrieve:timing] embed=%.1fms q=%s", (_t1 - _t0) * 1000, query.query[:40])

        # Step 2: Global vector search to supplement starting points
        global_results = await self._global_vector_search(
            collection=collection,
            query_vector=query_vector,
            sparse_query_vector=sparse_query_vector,
            limit=self.GLOBAL_SEARCH_TOPK,
            filter=final_metadata_filter,
            text_query=text_query,
        )

        _t2 = asyncio.get_event_loop().time()
        logger.info("[Retrieve:timing] global_search=%.1fms", (_t2 - _t1) * 1000)

        # Step 3: Merge starting points
        starting_points = await self._merge_starting_points(query.query, root_uris, global_results)

        # Step 4: Search (frontier batching or recursive fallback)
        # When query_vector is available, run dense + lexical in parallel for RRF fusion
        if query_vector is not None:
            # Flat-record fallback: when no directory starting points exist
            # (or only root-URI starting points with no global matches),
            # frontier search may miss flat records.  Fall back to direct
            # vector search for better recall on non-hierarchical data.
            # Also skip frontier when global results are all leaf nodes
            # (no hierarchy to traverse — frontier waves are pure overhead).
            has_real_starting_points = any(s for _, s in starting_points if s > 0)
            # Skip frontier when:
            # 1. force_flat_search is enabled (config-driven, e.g. for flat data)
            # 2. Global results contain no directories to traverse
            if self._force_flat_search:
                has_real_starting_points = False
            elif has_real_starting_points:
                has_directory_results = any(
                    not r.get("is_leaf", True) for r in global_results
                )
                if not has_directory_results:
                    logger.info("[HierarchicalRetriever] No directory results — using flat search")
                    has_real_starting_points = False
            if has_real_starting_points:
                if self._use_frontier_batching:
                    dense_coro = self._frontier_search(
                        query=query.query,
                        collection=collection,
                        query_vector=query_vector,
                        sparse_query_vector=sparse_query_vector,
                        starting_points=starting_points,
                        limit=limit,
                        mode=mode,
                        threshold=effective_threshold,
                        score_gte=score_gte,
                        metadata_filter=final_metadata_filter,
                        text_query=text_query,
                        classification=classification,
                    )
                else:
                    dense_coro = self._recursive_search(
                        query=query.query,
                        collection=collection,
                        query_vector=query_vector,
                        sparse_query_vector=sparse_query_vector,
                        starting_points=starting_points,
                        limit=limit,
                        mode=mode,
                        threshold=effective_threshold,
                        score_gte=score_gte,
                        metadata_filter=final_metadata_filter,
                        text_query=text_query,
                    )
            else:
                logger.info("[HierarchicalRetriever] No starting points — using flat vector search")
                # Fetch a wider candidate pool for reranking when data is flat
                # (all records under one parent, no hierarchy to exploit).
                rerank_pool = limit * self._flat_rerank_multiplier if self._rerank_client else limit
                dense_coro = self._flat_vector_search(
                    collection=collection,
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    filter=final_metadata_filter,
                    limit=rerank_pool,
                    text_query=text_query,
                )
            lexical_coro = self._lexical_search(
                collection=collection,
                text_query=text_query,
                filter=final_metadata_filter,
                limit=limit,
            )
            dense_candidates, lexical_candidates = await asyncio.gather(
                dense_coro, lexical_coro,
            )
            _t3 = asyncio.get_event_loop().time()
            logger.info("[Retrieve:timing] dense+lexical=%.1fms dense=%d lex=%d",
                        (_t3 - _t2) * 1000, len(dense_candidates), len(lexical_candidates))
            candidates = self._merge_rrf(
                dense_candidates, lexical_candidates, lexical_weight=lexical_boost,
            )
            # Rerank top candidates when in flat-search mode.
            # Truncate to rerank_config.max_candidates to control local-reranker latency.
            _candidates_before_rerank = len(candidates)
            t_search = time.perf_counter()
            if (
                not has_real_starting_points
                and self._rerank_client
                and len(candidates) > limit
                and self._should_rerank(candidates, score_key="_final_score", classification=classification)
            ):
                rerank_cap = (self.rerank_config.max_candidates or len(candidates)) if self.rerank_config else len(candidates)
                rerank_slice = candidates[:rerank_cap]
                remainder = candidates[rerank_cap:]
                docs = [c.get("abstract", "") for c in rerank_slice]
                _t_rr0 = asyncio.get_event_loop().time()
                rerank_scores = await self._rerank_client.rerank(
                    query.query, docs,
                )
                _t_rr1 = asyncio.get_event_loop().time()
                logger.info("[Retrieve:timing] rerank=%.1fms n_docs=%d",
                            (_t_rr1 - _t_rr0) * 1000, len(docs))
                _rerank_ms_flat = (_t_rr1 - _t_rr0) * 1000
                beta = self._fusion_beta
                for c, rs in zip(rerank_slice, rerank_scores):
                    c["_final_score"] = beta * rs + (1 - beta) * c.get("_final_score", c.get("_score", 0.0))
                candidates = rerank_slice + remainder
                candidates.sort(key=lambda x: x.get("_final_score", 0), reverse=True)
            t_rerank = time.perf_counter()
            candidates = candidates[:limit]
        else:
            # Embedding failed — adapter.search() already uses lexical fallback
            _candidates_before_rerank = 0
            if self._use_frontier_batching:
                candidates = await self._frontier_search(
                    query=query.query,
                    collection=collection,
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    starting_points=starting_points,
                    limit=limit,
                    mode=mode,
                    threshold=effective_threshold,
                    score_gte=score_gte,
                    metadata_filter=final_metadata_filter,
                    text_query=text_query,
                    classification=classification,
                )
            else:
                candidates = await self._recursive_search(
                    query=query.query,
                    collection=collection,
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    starting_points=starting_points,
                    limit=limit,
                    mode=mode,
                    threshold=effective_threshold,
                    score_gte=score_gte,
                    metadata_filter=final_metadata_filter,
                    text_query=text_query,
                )
            _candidates_before_rerank = len(candidates)
            t_search = time.perf_counter()
            t_rerank = t_search

        # Step 6: Convert results
        matched = await self._convert_to_matched_contexts(
            candidates, query.context_type, query.detail_level,
        )
        t_assemble = time.perf_counter()

        qr = QueryResult(
            query=query,
            matched_contexts=matched[:limit],
            searched_directories=root_uris,
        )
        if getattr(getattr(self, '_config', None), 'explain_enabled', True):
            from opencortex.retrieve.types import SearchExplain
            _path = "fast_path"
            if classification:
                _path = "llm_intent" if getattr(classification, 'from_llm', False) else "fast_path"
            qr.explain = SearchExplain(
                query_class=getattr(query, 'intent', '') or '',
                path=_path,
                intent_ms=0.0,
                embed_ms=(t_embed - t_start) * 1000,
                search_ms=(t_search - t_embed) * 1000,
                rerank_ms=(t_rerank - t_search) * 1000,
                assemble_ms=(t_assemble - t_rerank) * 1000,
                doc_scope_hit=bool(getattr(query, 'target_doc_id', None)),
                time_filter_hit=time_filter_active,
                candidates_before_rerank=_candidates_before_rerank,
                candidates_after_rerank=len(matched),
                frontier_waves=0,
                frontier_budget_exceeded=False,
                total_ms=(t_assemble - t_start) * 1000,
            )
        return qr

    async def _global_vector_search(
        self,
        collection: str,
        query_vector: Optional[List[float]],
        sparse_query_vector: Optional[Dict[str, float]],
        limit: int,
        filter: Optional[Dict[str, Any]] = None,
        text_query: str = "",
    ) -> List[Dict[str, Any]]:
        """Global vector search to locate initial directories.

        Applies the caller's metadata filter AND is_leaf=False.  This means
        content-level filters (e.g. category=observation) will exclude
        directories that don't carry that category — which is the desired
        behaviour: flat records with no matching parent directories will
        trigger the flat-search fallback path (better recall).
        """
        if not query_vector:
            return []
        sparse_query_vector = sparse_query_vector or {}

        global_filter = {
            "op": "and",
            "conds": [filter, {"op": "must", "field": "is_leaf", "conds": [False]}],
        }

        results = await self.storage.search(
            collection=collection,
            query_vector=query_vector,
            sparse_query_vector=sparse_query_vector,
            filter=global_filter,
            limit=limit,
            text_query=text_query,
        )
        return results

    def _should_rerank(
        self,
        results: List[Dict[str, Any]],
        score_key: str = "_score",
        classification: Optional[Any] = None,
    ) -> bool:
        """Decide whether rerank is worth the cost.

        Skip rerank when the top result has a clear score lead over the
        second result — reranking is unlikely to change the ordering.

        Args:
            results: List of result dicts.
            score_key: Which score field to use ('_score' or '_final_score').
            classification: Optional QueryClassification for class-based gates.
        """
        if len(results) < 2:
            return False
        scores = sorted(
            [r.get(score_key, 0.0) for r in results], reverse=True
        )
        gap = scores[0] - scores[1]
        threshold = getattr(
            getattr(self, '_config', None),
            'rerank_gate_score_gap_threshold',
            self._score_gap_threshold,
        )
        if gap > threshold:
            logger.debug(
                "[Rerank] Skipped — score gap %.3f > threshold %.3f",
                gap, threshold,
            )
            return False
        # v0.6: classification-based gates
        if classification:
            if (classification.query_class == "fact_lookup"
                    and classification.lexical_boost >= 0.6):
                logger.debug("[Rerank] Skipped — fact_lookup with high lexical_boost")
                return False
            skip_threshold = getattr(
                getattr(self, '_config', None),
                'rerank_gate_doc_scope_skip_threshold',
                5,
            )
            if (classification.query_class == "document_scoped"
                    and len(results) < skip_threshold):
                logger.debug(
                    "[Rerank] Skipped — document_scoped pool size %d < %d",
                    len(results), skip_threshold,
                )
                return False
        return True

    async def _merge_starting_points(
        self,
        query: str,
        root_uris: List[str],
        global_results: List[Dict[str, Any]],
        mode: str = "thinking",
    ) -> List[Tuple[str, float]]:
        """Merge starting points with optional rerank fusion.

        When RerankClient is active in THINKING mode:
          final_score = beta * rerank_score + (1-beta) * retrieval_score

        Returns:
            List of (uri, parent_score) tuples
        """
        points = []
        seen = set()

        if (
            self._rerank_client
            and mode == RetrieverMode.THINKING
            and global_results
            and self._should_rerank(global_results)
        ):
            docs = [r.get("abstract", "") for r in global_results]
            rerank_scores = await self._rerank_client.rerank(query, docs)
            beta = self._fusion_beta
            for i, r in enumerate(global_results):
                retrieval_score = r.get("_score", 0.0)
                fused = beta * rerank_scores[i] + (1 - beta) * retrieval_score
                reward = r.get("reward_score", 0.0)
                if reward != 0 and self._reward_weight:
                    fused += self._reward_weight * reward
                if self._hot_weight:
                    fused += self._hot_weight * self._compute_hotness(r)
                points.append((r["uri"], fused))
                seen.add(r["uri"])
        else:
            for r in global_results:
                score = r.get("_score", 0.0)
                reward = r.get("reward_score", 0.0)
                if reward != 0 and self._reward_weight:
                    score += self._reward_weight * reward
                if self._hot_weight:
                    score += self._hot_weight * self._compute_hotness(r)
                points.append((r["uri"], score))
                seen.add(r["uri"])

        # Root directories as starting points
        for uri in root_uris:
            if uri not in seen:
                points.append((uri, 0.0))
                seen.add(uri)

        return points

    async def _recursive_search(
        self,
        query: str,
        collection: str,
        query_vector: Optional[List[float]],
        sparse_query_vector: Optional[Dict[str, float]],
        starting_points: List[Tuple[str, float]],
        limit: int,
        mode: str,
        threshold: Optional[float] = None,
        score_gte: bool = False,
        metadata_filter: Optional[Dict[str, Any]] = None,
        text_query: str = "",
    ) -> List[Dict[str, Any]]:
        """
        Recursive search with directory priority return and score propagation.

        Args:
            threshold: Score threshold
            score_gte: True uses >=, False uses >
            metadata_filter: Additional metadata filter conditions
        """
        # Use passed threshold or default threshold
        effective_threshold = threshold if threshold is not None else self.threshold

        def passes_threshold(score: float) -> bool:
            """Check if score passes threshold."""
            if score_gte:
                return score >= effective_threshold
            return score > effective_threshold

        def merge_filter(base_filter: Dict, extra_filter: Optional[Dict]) -> Dict:
            """Merge filter conditions."""
            if not extra_filter:
                return base_filter
            return {"op": "and", "conds": [base_filter, extra_filter]}

        sparse_query_vector = sparse_query_vector or None

        collected: List[Dict[str, Any]] = []  # Collected results (directories and leaves)
        dir_queue: List[tuple] = []  # Priority queue: (-score, uri)
        visited: set = set()
        prev_topk_uris: set = set()
        convergence_rounds = 0

        alpha = self.SCORE_PROPAGATION_ALPHA

        # Initialize: process starting points
        for uri, score in starting_points:
            heapq.heappush(dir_queue, (-score, uri))

        while dir_queue:
            temp_score, current_uri = heapq.heappop(dir_queue)
            current_score = -temp_score
            if current_uri in visited:
                continue
            visited.add(current_uri)
            logger.info(f"[RecursiveSearch] Entering URI: {current_uri}")

            pre_filter_limit = max(limit * 2, 20)

            if metadata_filter:
                rec_dir_friendly = {"op": "or", "conds": [
                    {"op": "must", "field": "is_leaf", "conds": [False]},
                    metadata_filter,
                ]}
                rec_filter = merge_filter(
                    {"op": "must", "field": "parent_uri", "conds": [current_uri]},
                    rec_dir_friendly,
                )
            else:
                rec_filter = {"op": "must", "field": "parent_uri", "conds": [current_uri]}
            results = await self.storage.search(
                collection=collection,
                query_vector=query_vector,
                sparse_query_vector=sparse_query_vector,
                filter=rec_filter,
                limit=pre_filter_limit,
                text_query=text_query,
            )

            if not results:
                continue

            query_scores = []
            if (
                self._rerank_client
                and mode == RetrieverMode.THINKING
                and self._should_rerank(results)
            ):
                documents = [r.get("abstract", "") for r in results]
                rerank_scores = await self._rerank_client.rerank(query, documents)
                beta = self._fusion_beta
                for r, rerank_s in zip(results, rerank_scores):
                    base_score = r.get("_score", 0.0)
                    query_scores.append(beta * rerank_s + (1 - beta) * base_score)
            else:
                for r in results:
                    query_scores.append(r.get("_score", 0))

            for r, score in zip(results, query_scores):
                uri = r.get("uri", "")
                final_score = (
                    alpha * score + (1 - alpha) * current_score if current_score else score
                )
                reward = r.get("reward_score", 0.0)
                if reward != 0 and self._reward_weight:
                    final_score += self._reward_weight * reward
                if self._hot_weight:
                    final_score += self._hot_weight * self._compute_hotness(r)

                if not passes_threshold(final_score):
                    logger.debug(
                        f"[RecursiveSearch] URI {uri} score {final_score} did not pass threshold {effective_threshold}"
                    )
                    continue

                # Always collect results that pass threshold, even if already
                # visited as a directory starting point. The visited set only
                # prevents re-entering directories for child search.
                if not any(c.get("uri") == uri for c in collected):
                    r["_final_score"] = final_score
                    collected.append(r)
                    logger.debug(
                        f"[RecursiveSearch] Added URI: {uri} to candidates with score: {final_score}"
                    )

                if uri not in visited:
                    if r.get("is_leaf"):
                        visited.add(uri)
                    else:
                        heapq.heappush(dir_queue, (-final_score, uri))

            # Convergence check
            current_topk = sorted(collected, key=lambda x: x.get("_final_score", 0), reverse=True)[
                :limit
            ]
            current_topk_uris = {c.get("uri", "") for c in current_topk}

            if current_topk_uris == prev_topk_uris and len(current_topk_uris) >= limit:
                convergence_rounds += 1

                if convergence_rounds >= self.MAX_CONVERGENCE_ROUNDS:
                    break
            else:
                convergence_rounds = 0
                prev_topk_uris = current_topk_uris

        collected.sort(key=lambda x: x.get("_final_score", 0), reverse=True)
        return collected[:limit]

    async def _frontier_search(
        self,
        query: str,
        collection: str,
        query_vector: Optional[List[float]],
        sparse_query_vector: Optional[Dict[str, float]],
        starting_points: List[Tuple[str, float]],
        limit: int,
        mode: str,
        threshold: Optional[float] = None,
        score_gte: bool = False,
        metadata_filter: Optional[Dict[str, Any]] = None,
        text_query: str = "",
        classification: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """Frontier search with auto-fallback to recursive on error."""
        try:
            return await self._frontier_search_impl(
                query=query, collection=collection,
                query_vector=query_vector, sparse_query_vector=sparse_query_vector,
                starting_points=starting_points, limit=limit, mode=mode,
                threshold=threshold, score_gte=score_gte,
                metadata_filter=metadata_filter,
                text_query=text_query,
                classification=classification,
            )
        except Exception as e:
            logger.error("[FrontierSearch] Fallback to recursive: %s", e)
            return await self._recursive_search(
                query=query, collection=collection,
                query_vector=query_vector, sparse_query_vector=sparse_query_vector,
                starting_points=starting_points, limit=limit, mode=mode,
                threshold=threshold, score_gte=score_gte,
                metadata_filter=metadata_filter,
                text_query=text_query,
            )

    async def _frontier_search_impl(
        self,
        query: str,
        collection: str,
        query_vector: Optional[List[float]],
        sparse_query_vector: Optional[Dict[str, float]],
        starting_points: List[Tuple[str, float]],
        limit: int,
        mode: str,
        threshold: Optional[float] = None,
        score_gte: bool = False,
        metadata_filter: Optional[Dict[str, Any]] = None,
        text_query: str = "",
        classification: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """Wave-based frontier batching search.

        Replaces per-directory heapq search with batch queries per wave.
        See docs/plans/2026-02-28-frontier-batching-design.md for algorithm.
        """
        effective_threshold = threshold if threshold is not None else self.threshold

        def passes_threshold(score: float) -> bool:
            if score_gte:
                return score >= effective_threshold
            return score > effective_threshold

        def merge_filter(base_filter: Dict, extra_filter: Optional[Dict]) -> Dict:
            if not extra_filter:
                return base_filter
            return {"op": "and", "conds": [base_filter, extra_filter]}

        sparse_query_vector = sparse_query_vector or None
        alpha = self.SCORE_PROPAGATION_ALPHA

        # collected: uri -> dict (O(1) dedup, no index invalidation)
        collected: Dict[str, Dict[str, Any]] = {}
        visited_dirs: set = set()
        convergence_rounds = 0
        prev_topk_uris: set = set()

        # v0.6: Frontier hard budget — cap total storage.search() calls
        total_search_calls = 0
        max_calls = getattr(getattr(self, '_config', None), 'max_total_search_calls', 12)
        frontier_budget_exceeded = False

        frontier: List[Tuple[str, float]] = list(starting_points)

        for wave_idx in range(self._max_waves):
            if not frontier:
                break

            # 1. Frontier truncation (diversity-aware)
            if len(frontier) > self.MAX_FRONTIER_SIZE:
                frontier = self._diverse_truncate(frontier, self.MAX_FRONTIER_SIZE)

            # 2. Batch query
            # Use limit*5 base to give reranker enough candidates,
            # especially for flat data where one parent holds many leaves.
            per_wave_limit = max(
                limit * 5,
                len(frontier) * self.MIN_CHILDREN_PER_DIR * 2,
                50,
            )
            parent_uris = [uri for uri, _ in frontier]
            frontier_scores = {uri: score for uri, score in frontier}

            # Directory records (is_leaf=False) have empty category by design,
            # so they must bypass content filters (like category=X) to allow
            # tree traversal. Leaf records still need to pass the full metadata_filter.
            if metadata_filter:
                dir_friendly = {"op": "or", "conds": [
                    {"op": "must", "field": "is_leaf", "conds": [False]},
                    metadata_filter,
                ]}
                batch_filter = merge_filter(
                    {"op": "must", "field": "parent_uri", "conds": parent_uris},
                    dir_friendly,
                )
            else:
                batch_filter = {"op": "must", "field": "parent_uri", "conds": parent_uris}
            results = await self.storage.search(
                collection=collection,
                query_vector=query_vector,
                sparse_query_vector=sparse_query_vector,
                filter=batch_filter,
                limit=per_wave_limit,
                text_query=text_query,
            )
            total_search_calls += 1
            if total_search_calls >= max_calls:
                logger.warning(
                    "[Retriever] Frontier budget exceeded (%d calls)", total_search_calls
                )
                frontier_budget_exceeded = True
                # Process current wave results then stop
                # (break at end of wave logic below)

            # 3. Group by parent + score propagation
            children_by_parent: Dict[str, List[Dict[str, Any]]] = {}
            for r in results:
                p_uri = r.get("parent_uri", "")
                if p_uri not in children_by_parent:
                    children_by_parent[p_uri] = []
                children_by_parent[p_uri].append(r)

            for p_uri, children in children_by_parent.items():
                parent_score = frontier_scores.get(p_uri, 0.0)
                for child in children:
                    raw_score = child.get("_score", 0.0)
                    child["_final_score"] = (
                        alpha * raw_score + (1 - alpha) * parent_score
                        if parent_score
                        else raw_score
                    )
                    reward = child.get("reward_score", 0.0)
                    if reward != 0 and self._reward_weight:
                        child["_final_score"] += self._reward_weight * reward
                    if self._hot_weight:
                        child["_final_score"] += self._hot_weight * self._compute_hotness(child)

            # 4. Compensation query (starved parents)
            starved = [
                uri for uri in parent_uris
                if len(children_by_parent.get(uri, [])) < self.MIN_CHILDREN_PER_DIR
                and uri not in visited_dirs
            ]
            if starved and not frontier_budget_exceeded:
                if metadata_filter:
                    comp_dir_friendly = {"op": "or", "conds": [
                        {"op": "must", "field": "is_leaf", "conds": [False]},
                        metadata_filter,
                    ]}
                    comp_filter = merge_filter(
                        {"op": "must", "field": "parent_uri", "conds": starved},
                        comp_dir_friendly,
                    )
                else:
                    comp_filter = {"op": "must", "field": "parent_uri", "conds": starved}
                comp_results = await self.storage.search(
                    collection=collection,
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    filter=comp_filter,
                    limit=len(starved) * self.MIN_CHILDREN_PER_DIR,
                    text_query=text_query,
                )
                total_search_calls += 1
                if total_search_calls >= max_calls:
                    logger.warning(
                        "[Retriever] Frontier budget exceeded (%d calls)", total_search_calls
                    )
                    frontier_budget_exceeded = True
                for r in comp_results:
                    p_uri = r.get("parent_uri", "")
                    if p_uri not in children_by_parent:
                        children_by_parent[p_uri] = []
                    if not any(c.get("uri") == r.get("uri") for c in children_by_parent[p_uri]):
                        parent_score = frontier_scores.get(p_uri, 0.0)
                        raw_score = r.get("_score", 0.0)
                        r["_final_score"] = (
                            alpha * raw_score + (1 - alpha) * parent_score
                            if parent_score else raw_score
                        )
                        reward = r.get("reward_score", 0.0)
                        if reward != 0 and self._reward_weight:
                            r["_final_score"] += self._reward_weight * reward
                        if self._hot_weight:
                            r["_final_score"] += self._hot_weight * self._compute_hotness(r)
                        children_by_parent[p_uri].append(r)

                # Tiny queries for still-starved parents — single batch replaces N round-trips
                still_starved = [
                    uri for uri in starved
                    if len(children_by_parent.get(uri, [])) < self.MIN_CHILDREN_PER_DIR
                ]
                if still_starved and not frontier_budget_exceeded:
                    if metadata_filter:
                        still_dir_friendly = {"op": "or", "conds": [
                            {"op": "must", "field": "is_leaf", "conds": [False]},
                            metadata_filter,
                        ]}
                        still_batch_filter = merge_filter(
                            {"op": "must", "field": "parent_uri", "conds": still_starved},
                            still_dir_friendly,
                        )
                    else:
                        still_batch_filter = {
                            "op": "must", "field": "parent_uri", "conds": still_starved,
                        }
                    still_results = await self.storage.search(
                        collection=collection,
                        query_vector=query_vector,
                        sparse_query_vector=sparse_query_vector,
                        filter=still_batch_filter,
                        limit=len(still_starved) * self.MIN_CHILDREN_PER_DIR,
                        text_query=text_query,
                    )
                    total_search_calls += 1
                    if total_search_calls >= max_calls:
                        logger.warning(
                            "[Retriever] Frontier budget exceeded (%d calls)", total_search_calls
                        )
                        frontier_budget_exceeded = True
                    for r in still_results:
                        s_uri = r.get("parent_uri", "")
                        if s_uri not in still_starved:
                            continue
                        if any(c.get("uri") == r.get("uri")
                               for c in children_by_parent.get(s_uri, [])):
                            continue
                        parent_score = frontier_scores.get(s_uri, 0.0)
                        raw_score = r.get("_score", 0.0)
                        r["_final_score"] = (
                            alpha * raw_score + (1 - alpha) * parent_score
                            if parent_score else raw_score
                        )
                        reward = r.get("reward_score", 0.0)
                        if reward != 0 and self._reward_weight:
                            r["_final_score"] += self._reward_weight * reward
                        if self._hot_weight:
                            r["_final_score"] += self._hot_weight * self._compute_hotness(r)
                        if s_uri not in children_by_parent:
                            children_by_parent[s_uri] = []
                        children_by_parent[s_uri].append(r)

            # 5. Fair select
            selected = self._per_parent_fair_select(
                children_by_parent,
                min_quota=self.MIN_CHILDREN_PER_DIR,
                total_budget=per_wave_limit,
            )

            # 6. Triage + cycle prevention
            next_frontier: Dict[str, float] = {}
            for child in selected:
                final_score = child.get("_final_score", 0.0)
                if not passes_threshold(final_score):
                    continue
                uri = child.get("uri", "")
                if uri in collected:
                    if final_score > collected[uri].get("_final_score", 0.0):
                        collected[uri] = child
                else:
                    collected[uri] = child
                if not child.get("is_leaf", False) and uri not in visited_dirs:
                    old_score = next_frontier.get(uri, -1.0)
                    if final_score > old_score:
                        next_frontier[uri] = final_score

            visited_dirs.update(uri for uri, _ in frontier)

            # Budget guard: stop after budget-exceeded wave completes
            if frontier_budget_exceeded:
                break

            # 7. Convergence check
            top_k_items = heapq.nlargest(
                limit, collected.values(),
                key=lambda x: x.get("_final_score", 0.0),
            )
            current_topk_uris = {c.get("uri", "") for c in top_k_items}
            if current_topk_uris == prev_topk_uris and len(collected) >= limit:
                convergence_rounds += 1
                if convergence_rounds >= self.MAX_CONVERGENCE_ROUNDS:
                    logger.info("[FrontierSearch] Converged after %d waves", wave_idx + 1)
                    break
            else:
                convergence_rounds = 0
                prev_topk_uris = current_topk_uris

            frontier = [(uri, score) for uri, score in next_frontier.items()]

        # 8. Late Rerank
        all_candidates = sorted(
            collected.values(),
            key=lambda x: x.get("_final_score", 0.0),
            reverse=True,
        )
        rerank_count = min(self.LATE_RERANK_CAP, limit * self.LATE_RERANK_FACTOR)
        top_m = all_candidates[:rerank_count]

        if (
            self._rerank_client
            and mode == RetrieverMode.THINKING
            and self._should_rerank(top_m, score_key="_final_score", classification=classification)
        ):
            docs = [c.get("abstract", "") for c in top_m]
            rerank_scores = await self._rerank_client.rerank(query, docs)
            beta = self._fusion_beta
            for c, rs in zip(top_m, rerank_scores):
                c["_final_score"] = beta * rs + (1 - beta) * c.get("_final_score", 0.0)
            top_m.sort(key=lambda x: x.get("_final_score", 0.0), reverse=True)

        return top_m[:limit]

    async def _flat_vector_search(
        self,
        collection: str,
        query_vector: List[float],
        sparse_query_vector: Optional[Dict[str, float]] = None,
        filter: Optional[Dict[str, Any]] = None,
        limit: int = 10,
        text_query: str = "",
    ) -> List[Dict[str, Any]]:
        """Direct vector search for flat (non-hierarchical) records.

        Fallback when no directory starting points exist — skips frontier
        batching and does a simple nearest-neighbour query.  Applies reward +
        hotness boosts identically to the frontier path.
        """
        results = await self.storage.search(
            collection=collection,
            query_vector=query_vector,
            sparse_query_vector=sparse_query_vector or {},
            filter=filter,
            limit=limit,
            text_query=text_query,
        )
        for r in results:
            reward = r.get("reward_score", 0.0)
            if reward != 0 and self._reward_weight:
                r["_score"] = r.get("_score", 0.0) + self._reward_weight * reward
            if self._hot_weight:
                r["_score"] = r.get("_score", 0.0) + self._hot_weight * self._compute_hotness(r)
        results.sort(key=lambda r: r.get("_score", 0.0), reverse=True)
        return results

    async def _lexical_search(
        self,
        collection: str,
        text_query: str,
        filter: Optional[Dict[str, Any]] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Run standalone lexical search if storage supports it.

        Uses hasattr detection (same pattern as reward methods) so InMemoryStorage
        in tests gracefully returns empty results.
        """
        if not hasattr(self.storage, "search_lexical"):
            return []
        try:
            results = await self.storage.search_lexical(
                collection=collection,
                text_query=text_query,
                filter=filter,
                limit=limit,
            )
            # Apply reward + hotness boost
            for r in results:
                reward = r.get("reward_score", 0.0)
                if reward != 0 and self._reward_weight:
                    r["_score"] = r.get("_score", 0.0) + self._reward_weight * reward
                if self._hot_weight:
                    r["_score"] = r.get("_score", 0.0) + self._hot_weight * self._compute_hotness(r)
            return results
        except Exception as exc:
            logger.warning("[HierarchicalRetriever] Lexical search failed: %s", exc)
            return []

    @staticmethod
    def _merge_rrf(
        dense_results: List[Dict[str, Any]],
        lexical_results: List[Dict[str, Any]],
        lexical_weight: float = 0.3,
        k: int = 60,
    ) -> List[Dict[str, Any]]:
        """Merge dense and lexical results using weighted Reciprocal Rank Fusion.

        Formula:
            RRF(d) = (1-b) * 1/(k + rank_dense(d)) + b * 1/(k + rank_lexical(d))

        Where b = lexical_weight, k = 60.
        Documents not present in a path get rank = len(results) (worst rank).
        """
        if not lexical_results:
            return dense_results

        # Build rank maps (0-indexed)
        dense_rank = {}
        dense_by_uri = {}
        for i, r in enumerate(dense_results):
            uri = r.get("uri", "")
            if uri:
                dense_rank[uri] = i
                dense_by_uri[uri] = r

        lexical_rank = {}
        lexical_by_uri = {}
        for i, r in enumerate(lexical_results):
            uri = r.get("uri", "")
            if uri:
                lexical_rank[uri] = i
                lexical_by_uri[uri] = r

        all_uris = set(dense_rank.keys()) | set(lexical_rank.keys())
        dense_default = len(dense_results)
        lexical_default = len(lexical_results)

        b = lexical_weight
        scored: List[tuple] = []  # (rrf_score, uri)
        for uri in all_uris:
            dr = dense_rank.get(uri, dense_default)
            lr = lexical_rank.get(uri, lexical_default)
            rrf = (1 - b) / (k + dr) + b / (k + lr)
            scored.append((rrf, uri))

        scored.sort(key=lambda x: x[0], reverse=True)

        merged = []
        for rrf_score, uri in scored:
            record = dense_by_uri.get(uri) or lexical_by_uri.get(uri)
            if record:
                record = dict(record)  # avoid mutating original
                record["_final_score"] = rrf_score
                merged.append(record)

        return merged

    async def _convert_to_matched_contexts(
        self,
        candidates: List[Dict[str, Any]],
        context_type: ContextType,
        detail_level: DetailLevel = DetailLevel.L1,
    ) -> List[MatchedContext]:
        """Convert candidate results to MatchedContext list.

        Args:
            candidates: Raw candidate dicts from vector search
            context_type: Type of context
            detail_level: Controls what data to load:
                L0: abstract only (from Qdrant payload)
                L1: abstract + overview (from Qdrant payload, zero I/O)
                L2: abstract + overview + content (filesystem read)

        Relations are pre-fetched in two bulk phases to avoid N×read_batch:
          Phase 1 — one asyncio.gather over all get_relations() calls
          Phase 2 — single read_batch for the union of all related URIs
          Phase 3 — build MatchedContext objects from pre-fetched maps (no FS I/O)
        """
        cortex_fs = _get_cortex_fs()

        # Phase 1: batch-prefetch relation tables (one gather, all concurrent)
        all_related: Dict[str, List[str]] = {}
        if cortex_fs and candidates:
            candidate_uris = [c.get("uri", "") for c in candidates if c.get("uri")]
            raw_relations = await asyncio.gather(
                *[cortex_fs.get_relations(u) for u in candidate_uris],
                return_exceptions=True,
            )
            for uri, result in zip(candidate_uris, raw_relations):
                if isinstance(result, list) and result:
                    all_related[uri] = result

        # Phase 2: single read_batch for all unique related URIs
        unique_related: set = set()
        for rel_list in all_related.values():
            unique_related.update(rel_list[: self.MAX_RELATIONS])

        related_abstracts: Dict[str, str] = {}
        if cortex_fs and unique_related:
            related_abstracts = await cortex_fs.read_batch(
                list(unique_related), level="l0"
            )

        # Phase 3: build MatchedContext objects using pre-fetched data (no FS I/O)
        async def _build_one(c: Dict[str, Any]) -> MatchedContext:
            uri = c.get("uri", "")
            relations: list = []
            for rel_uri in all_related.get(uri, [])[: self.MAX_RELATIONS]:
                abstract = related_abstracts.get(rel_uri, "")
                if abstract:
                    relations.append(RelatedContext(uri=rel_uri, abstract=abstract))

            abstract = c.get("abstract", "")
            overview = None
            if detail_level in (DetailLevel.L1, DetailLevel.L2):
                overview = c.get("overview", "") or None

            # v0.6: Small-to-Big — enrich leaf chunks with parent section overview
            if (getattr(getattr(self, '_config', None), 'small_to_big_enabled', True)
                    and c.get("is_leaf", False)
                    and c.get("parent_uri")):
                parent_uri_stb = c["parent_uri"]
                parent_abstract_stb = related_abstracts.get(parent_uri_stb, "")
                if parent_abstract_stb and overview:
                    overview = f"[Parent Section] {parent_abstract_stb}\n\n{overview}"
                elif parent_abstract_stb:
                    overview = f"[Parent Section] {parent_abstract_stb}"

            content = None
            if detail_level == DetailLevel.L2 and cortex_fs:
                try:
                    raw = await cortex_fs.read(uri + "/content.md")
                    content = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                except Exception:
                    pass

            effective_type = context_type
            if context_type == ContextType.ANY:
                raw_type = c.get("context_type", "memory")
                try:
                    effective_type = ContextType(raw_type)
                except ValueError:
                    effective_type = ContextType.MEMORY

            return MatchedContext(
                uri=uri,
                context_type=effective_type,
                is_leaf=c.get("is_leaf", False),
                abstract=abstract,
                overview=overview,
                content=content,
                keywords=c.get("keywords", ""),
                category=c.get("category", ""),
                score=c.get("_final_score", c.get("_score", 0.0)),
                relations=relations,
            )

        results = await asyncio.gather(*[_build_one(c) for c in candidates])
        return list(results)

    @staticmethod
    def _diverse_truncate(
        frontier: List[Tuple[str, float]],
        max_size: int,
    ) -> List[Tuple[str, float]]:
        """Truncate frontier with diversity across root branches.

        Buckets by URI prefix (root branch), sorts each bucket by score desc,
        then round-robin fills to max_size.
        """
        if len(frontier) <= max_size:
            return frontier

        buckets: Dict[str, List[Tuple[str, float]]] = {}
        for uri, score in frontier:
            parts = uri.split("/")
            root = "/".join(parts[:5]) if len(parts) >= 5 else uri
            if root not in buckets:
                buckets[root] = []
            buckets[root].append((uri, score))

        for b in buckets.values():
            b.sort(key=lambda x: x[1], reverse=True)

        result: List[Tuple[str, float]] = []
        iters = [iter(b) for b in buckets.values()]
        while len(result) < max_size and iters:
            next_round = []
            for it in iters:
                if len(result) >= max_size:
                    break
                item = next(it, None)
                if item is not None:
                    result.append(item)
                    next_round.append(it)
            iters = next_round

        return result[:max_size]

    @staticmethod
    def _per_parent_fair_select(
        children_by_parent: Dict[str, List[Dict[str, Any]]],
        min_quota: int,
        total_budget: int,
    ) -> List[Dict[str, Any]]:
        """Fair select: each parent gets min_quota first, rest compete globally.

        Args:
            children_by_parent: {parent_uri: [child_dicts]} with '_final_score' set.
            min_quota: Minimum children guaranteed per parent.
            total_budget: Maximum total children to return.
        """
        selected: List[Dict[str, Any]] = []
        remaining: List[Dict[str, Any]] = []

        for children in children_by_parent.values():
            sorted_children = sorted(
                children, key=lambda x: x.get("_final_score", 0.0), reverse=True
            )
            selected.extend(sorted_children[:min_quota])
            remaining.extend(sorted_children[min_quota:])

        if len(selected) < total_budget:
            remaining.sort(key=lambda x: x.get("_final_score", 0.0), reverse=True)
            selected.extend(remaining[: total_budget - len(selected)])

        return selected[:total_budget]

    def _get_root_uris_for_type(self, context_type: ContextType) -> List[str]:
        """Return starting directory URI list based on context_type.

        Uses per-request identity (contextvar) when available, falling back
        to global config for tenant_id and user_id.
        """
        from opencortex.http.request_context import get_effective_identity
        from opencortex.utils.uri import CortexURI

        tid, uid = get_effective_identity()

        if context_type == ContextType.MEMORY:
            return [
                # User private memories
                CortexURI.build_private(tid, uid, "memories"),
                # Shared patterns
                CortexURI.build_shared(tid, "shared", "patterns"),
                # Shared cases
                CortexURI.build_shared(tid, "shared", "cases"),
            ]
        elif context_type == ContextType.RESOURCE:
            return [CortexURI.build_shared(tid, "resources")]
        elif context_type == ContextType.CASE:
            return [CortexURI.build_shared(tid, "shared", "cases")]
        elif context_type == ContextType.PATTERN:
            return [CortexURI.build_shared(tid, "shared", "patterns")]
        elif context_type == ContextType.ANY:
            # Global search: include all known root paths
            return [
                CortexURI.build_private(tid, uid, "memories"),
                CortexURI.build_shared(tid, "shared", "patterns"),
                CortexURI.build_shared(tid, "shared", "cases"),
                CortexURI.build_shared(tid, "resources"),
            ]
        return []

    def _type_to_collection(self, context_type: ContextType) -> str:
        """
        Convert context type to collection name.
        Respects X-Collection contextvar override for benchmark isolation.
        """
        from opencortex.http.request_context import get_collection_name
        return get_collection_name() or "context"
