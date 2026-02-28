# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# Ported from OpenViking (https://github.com/volcengine/openviking)
# SPDX-License-Identifier: Apache-2.0
"""
Hierarchical retriever for OpenCortex.

Implements directory-based hierarchical retrieval with recursive search
and rerank-based relevance scoring.
"""

import asyncio

import heapq
import logging
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
from opencortex.storage import VikingDBInterface

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
    LATE_RERANK_FACTOR = 5          # Late rerank candidate multiplier
    LATE_RERANK_CAP = 50            # Late rerank candidate cap
    DEFAULT_MAX_WAVES = 8           # Default max wave iterations

    def __init__(
        self,
        storage: VikingDBInterface,
        embedder: Optional[Any],
        rerank_config: Optional[RerankConfig] = None,
        llm_completion: Optional[Any] = None,
        rl_weight: float = 0.05,
        use_frontier_batching: bool = True,
        max_waves: int = 8,
    ):
        """Initialize hierarchical retriever with rerank_config.

        Args:
            storage: VikingDBInterface instance
            embedder: Embedder instance (supports dense/sparse/hybrid)
            rerank_config: Rerank configuration (optional, will fallback to vector search only)
            llm_completion: Async LLM callable for RerankClient LLM fallback
        """
        self.storage = storage
        self.embedder = embedder
        self.rerank_config = rerank_config

        # Use rerank threshold if available, otherwise use a default
        self.threshold = rerank_config.threshold if rerank_config else 0

        self._rl_weight = rl_weight
        self._use_frontier_batching = use_frontier_batching
        self._max_waves = max_waves

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

    async def retrieve(
        self,
        query: TypedQuery,
        limit: int = 5,
        mode: RetrieverMode = RetrieverMode.THINKING,
        score_threshold: Optional[float] = None,
        score_gte: bool = False,
        metadata_filter: Optional[Dict[str, Any]] = None,
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

        collection = self._type_to_collection(query.context_type)

        target_dirs = [d for d in (query.target_directories or []) if d]

        # Create context_type filter
        type_filter = {"op": "must", "field": "context_type", "conds": [query.context_type.value]}

        # Merge all filters
        filters_to_merge = [type_filter]
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

        # Generate query vectors once to avoid duplicate embedding calls
        query_vector = None
        sparse_query_vector = None
        if self.embedder:
            loop = asyncio.get_event_loop()
            result: EmbedResult = await loop.run_in_executor(
                None, self.embedder.embed, query.query
            )
            query_vector = result.dense_vector
            sparse_query_vector = result.sparse_vector

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
            )
            # Apply RL boost to scroll results
            for r in results:
                reward = r.get("reward_score", 0.0)
                if reward != 0 and self._rl_weight:
                    r["_score"] = r.get("_score", 0.0) + self._rl_weight * reward
            results.sort(key=lambda r: r.get("_score", 0.0), reverse=True)
            matched = await self._convert_to_matched_contexts(
                results[:limit], query.context_type, query.detail_level,
            )
            return QueryResult(
                query=query,
                matched_contexts=matched,
                searched_directories=root_uris,
            )

        # Step 2: Global vector search to supplement starting points
        global_results = await self._global_vector_search(
            collection=collection,
            query_vector=query_vector,
            sparse_query_vector=sparse_query_vector,
            limit=self.GLOBAL_SEARCH_TOPK,
            filter=final_metadata_filter,
        )

        # Step 3: Merge starting points
        starting_points = await self._merge_starting_points(query.query, root_uris, global_results)

        # Step 4: Recursive search
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
        )

        # Step 6: Convert results
        matched = await self._convert_to_matched_contexts(
            candidates, query.context_type, query.detail_level,
        )

        return QueryResult(
            query=query,
            matched_contexts=matched[:limit],
            searched_directories=root_uris,
        )

    async def _global_vector_search(
        self,
        collection: str,
        query_vector: Optional[List[float]],
        sparse_query_vector: Optional[Dict[str, float]],
        limit: int,
        filter: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Global vector search to locate initial directories."""
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
        )
        return results

    def _should_rerank(self, results: List[Dict[str, Any]], score_key: str = "_score") -> bool:
        """Decide whether rerank is worth the cost.

        Skip rerank when the top result has a clear score lead over the
        second result — reranking is unlikely to change the ordering.

        Args:
            results: List of result dicts.
            score_key: Which score field to use ('_score' or '_final_score').
        """
        if len(results) < 2:
            return False
        scores = sorted(
            [r.get(score_key, 0.0) for r in results], reverse=True
        )
        gap = scores[0] - scores[1]
        if gap > self._score_gap_threshold:
            logger.debug(
                "[Rerank] Skipped — score gap %.3f > threshold %.3f",
                gap, self._score_gap_threshold,
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
                if reward != 0 and self._rl_weight:
                    fused += self._rl_weight * reward
                points.append((r["uri"], fused))
                seen.add(r["uri"])
        else:
            for r in global_results:
                score = r.get("_score", 0.0)
                reward = r.get("reward_score", 0.0)
                if reward != 0 and self._rl_weight:
                    score += self._rl_weight * reward
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

            results = await self.storage.search(
                collection=collection,
                query_vector=query_vector,
                sparse_query_vector=sparse_query_vector,  # Pass sparse vector
                filter=merge_filter(
                    {"op": "must", "field": "parent_uri", "conds": [current_uri]}, metadata_filter
                ),
                limit=pre_filter_limit,
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
                if reward != 0 and self._rl_weight:
                    final_score += self._rl_weight * reward

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
        """
        viking_fs = _get_cortex_fs()

        async def _build_one(c: Dict[str, Any]) -> MatchedContext:
            # Fetch relations concurrently
            relations: list = []
            if viking_fs:
                related_uris = await viking_fs.get_relations(c.get("uri", ""))
                if related_uris:
                    related_abstracts = await viking_fs.read_batch(
                        related_uris[: self.MAX_RELATIONS], level="l0"
                    )
                    for uri in related_uris[: self.MAX_RELATIONS]:
                        abstract = related_abstracts.get(uri, "")
                        if abstract:
                            relations.append(RelatedContext(uri=uri, abstract=abstract))

            # L0: always from Qdrant payload
            abstract = c.get("abstract", "")

            # L1: overview from Qdrant payload (zero I/O)
            overview = None
            if detail_level in (DetailLevel.L1, DetailLevel.L2):
                overview = c.get("overview", "") or None

            # L2: content from filesystem (on-demand)
            content = None
            if detail_level == DetailLevel.L2 and viking_fs:
                node_uri = c.get("uri", "")
                try:
                    raw = await viking_fs.read(node_uri + "/content.md")
                    content = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                except Exception:
                    pass

            return MatchedContext(
                uri=c.get("uri", ""),
                context_type=context_type,
                is_leaf=c.get("is_leaf", False),
                abstract=abstract,
                overview=overview,
                content=content,
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

        Uses global config for tenant_id and user_id to construct correct
        tenant-based URIs with user isolation.
        """
        from opencortex.config import get_config
        from opencortex.utils.uri import CortexURI

        cfg = get_config()
        tid = cfg.tenant_id
        uid = cfg.user_id

        if context_type == ContextType.MEMORY:
            return [
                # User private memories
                CortexURI.build_private(tid, uid, "memories"),
                # Shared agent patterns
                CortexURI.build_shared(tid, "agent", "memories", "patterns"),
                # User private agent cases
                CortexURI.build_private(tid, uid, "agent", "memories", "cases"),
            ]
        elif context_type == ContextType.RESOURCE:
            return [CortexURI.build_shared(tid, "resources")]
        elif context_type == ContextType.SKILL:
            return [CortexURI.build_shared(tid, "agent", "skills")]
        return []

    def _type_to_collection(self, context_type: ContextType) -> str:
        """
        Convert context type to collection name.
        """
        return "context"
