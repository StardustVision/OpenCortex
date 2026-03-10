# SPDX-License-Identifier: Apache-2.0
"""
ContextManager — three-phase lifecycle for the Memory Context Protocol.

Manages prepare/commit/end phases for platform-agnostic memory recall and
session recording.  Replaces Claude Code hooks with a single MCP tool.

Design doc: docs/memory-context-protocol.md v1.2
"""

import asyncio
import logging
import orjson as json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from opencortex.http.request_context import (
    get_effective_identity,
    reset_request_identity,
    set_request_identity,
)
from opencortex.retrieve.intent_router import IntentRouter
from opencortex.retrieve.types import DetailLevel, SearchIntent

logger = logging.getLogger(__name__)

# Type aliases — all internal state keyed by these to prevent cross-tenant collision
SessionKey = Tuple[str, str, str]      # (tenant_id, user_id, session_id)
CacheKey = Tuple[str, str, str, str]   # (tenant_id, user_id, session_id, turn_id)


class ContextManager:
    """Manages the prepare/commit/end lifecycle for memory_context protocol.

    Args:
        orchestrator: MemoryOrchestrator instance.
        observer: Observer instance for transcript recording.
        prepare_cache_ttl: Prepare result cache TTL in seconds (default 5min).
        session_idle_ttl: Session idle auto-close TTL in seconds (default 30min).
        idle_check_interval: Idle sweep interval in seconds (default 60s).
        max_content_chars: Per-item content hard limit (default 50k chars).
    """

    def __init__(
        self,
        orchestrator,  # MemoryOrchestrator (avoid circular import)
        observer,      # Observer
        *,
        prepare_cache_ttl: float = 300.0,
        session_idle_ttl: float = 1800.0,
        idle_check_interval: float = 60.0,
        max_content_chars: int = 50_000,
    ):
        self._orchestrator = orchestrator
        self._observer = observer

        # Prepare cache: {(tid, uid, sid, turn_id): (result, timestamp)}
        self._prepare_cache: Dict[CacheKey, Tuple[Dict, float]] = {}
        # Reverse index: {session_key: set(cache_key)} — for end cleanup
        self._session_cache_keys: Dict[SessionKey, Set[CacheKey]] = {}
        # Committed turn_ids: {session_key: set(turn_id)}
        self._committed_turns: Dict[SessionKey, Set[str]] = {}
        # Session activity: {session_key: last_activity_timestamp}
        self._session_activity: Dict[SessionKey, float] = {}
        # Session-level locks: prevent concurrent begin_session
        self._session_locks: Dict[SessionKey, asyncio.Lock] = {}
        # Pending async tasks (cited_uris reward, etc.)
        self._pending_tasks: Set[asyncio.Task] = set()

        # Config
        self._prepare_cache_ttl = prepare_cache_ttl
        self._session_idle_ttl = session_idle_ttl
        self._idle_check_interval = idle_check_interval
        self._max_content_chars = max_content_chars

        # Background task
        self._idle_checker: Optional[asyncio.Task] = None

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """Start background idle session sweeper."""
        self._idle_checker = asyncio.create_task(self._idle_session_loop())

    async def close(self) -> None:
        """Cancel idle checker and await pending tasks."""
        if self._idle_checker:
            self._idle_checker.cancel()
            try:
                await self._idle_checker
            except asyncio.CancelledError:
                pass
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
            self._pending_tasks.clear()

    # =========================================================================
    # Unified entry point
    # =========================================================================

    async def handle(
        self,
        session_id: str,
        phase: str,
        tenant_id: str,
        user_id: str,
        turn_id: Optional[str] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        cited_uris: Optional[List[str]] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Unified entry point — dispatches to prepare/commit/end."""
        if phase == "prepare":
            if not turn_id:
                raise ValueError("turn_id is required for prepare")
            if not messages or not any(m.get("role") == "user" for m in messages):
                raise ValueError("prepare requires at least one user message")
            return await self._prepare(
                session_id, turn_id, messages, tenant_id, user_id, config,
            )

        elif phase == "commit":
            if not turn_id:
                raise ValueError("turn_id is required for commit")
            if not messages or len(messages) < 2:
                raise ValueError("commit requires at least user + assistant messages")
            return await self._commit(
                session_id, turn_id, messages, tenant_id, user_id, cited_uris,
            )

        elif phase == "end":
            return await self._end(session_id, tenant_id, user_id)

        else:
            raise ValueError(f"Unknown phase: {phase}")

    # =========================================================================
    # Phase: prepare
    # =========================================================================

    async def _prepare(
        self,
        session_id: str,
        turn_id: str,
        messages: List[Dict[str, str]],
        tenant_id: str,
        user_id: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        config = config or {}
        max_items = min(config.get("max_items", 5), 20)
        detail_level = config.get("detail_level", "l1")
        recall_mode = config.get("recall_mode", "auto")
        sk = self._make_session_key(tenant_id, user_id, session_id)

        # 1. Idempotent: cache hit → return directly
        cache_key: CacheKey = (tenant_id, user_id, session_id, turn_id)
        cached = self._get_cached_prepare(cache_key)
        if cached is not None:
            self._touch_session(sk)
            logger.debug("[ContextManager] prepare CACHE_HIT sid=%s tid=%s", session_id, turn_id)
            return cached

        # 2. Session auto-create (session-level lock prevents concurrent begin)
        self._touch_session(sk)
        lock = self._session_locks.setdefault(sk, asyncio.Lock())
        async with lock:
            if session_id not in self._observer.active_sessions():
                self._observer.begin_session(session_id, tenant_id, user_id)

        # 3. Extract user query
        query = self._extract_query(messages)
        if not query:
            result = self._empty_prepare(session_id, turn_id)
            self._cache_prepare(cache_key, sk, result)
            return result

        # 4. Intent analysis (2s timeout, degrade to default on failure)
        intent = SearchIntent()
        if recall_mode != "never":
            try:
                router = IntentRouter(
                    llm_completion=self._orchestrator._llm_completion,
                )
                intent = await asyncio.wait_for(
                    router.route(query), timeout=2.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[ContextManager] IntentRouter timeout for turn %s", turn_id,
                )
            except Exception as exc:
                logger.warning(
                    "[ContextManager] IntentRouter failed: %s", exc,
                )

        should_recall = (
            recall_mode == "always"
            or (recall_mode == "auto" and intent.should_recall)
        )

        # 5. Retrieval
        memory_items: List[Dict[str, Any]] = []
        knowledge_items: List[Dict[str, Any]] = []

        if should_recall:
            # 5a. Memory search via orchestrator (includes tenant isolation + RL fusion)
            try:
                find_result = await self._orchestrator.search(
                    query=query,
                    limit=max_items,
                    detail_level=detail_level,
                )
                memory_items = self._format_memories(find_result, detail_level)
            except Exception as exc:
                logger.warning("[ContextManager] Memory search failed: %s", exc)

            # 5b. Knowledge search via orchestrator
            try:
                k_result = await self._orchestrator.knowledge_search(
                    query=query,
                    limit=min(3, max_items),
                )
                knowledge_items = self._format_knowledge(k_result.get("results", []))
            except Exception as exc:
                logger.warning("[ContextManager] Knowledge search failed: %s", exc)

        # 6. Build instructions
        instructions = self._build_instructions(intent, memory_items, knowledge_items)

        result = {
            "session_id": session_id,
            "turn_id": turn_id,
            "intent": {
                "should_recall": should_recall,
                "intent_type": intent.intent_type,
                "detail_level": intent.detail_level.value if intent.detail_level else "l1",
            },
            "memory": memory_items,
            "knowledge": knowledge_items,
            "instructions": instructions,
        }

        logger.info(
            "[ContextManager] prepare sid=%s tid=%s intent=%s recall=%d latency_placeholder",
            session_id, turn_id, intent.intent_type, len(memory_items),
        )
        self._cache_prepare(cache_key, sk, result)
        return result

    # =========================================================================
    # Phase: commit
    # =========================================================================

    async def _commit(
        self,
        session_id: str,
        turn_id: str,
        messages: List[Dict[str, str]],
        tenant_id: str,
        user_id: str,
        cited_uris: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        sk = self._make_session_key(tenant_id, user_id, session_id)
        self._touch_session(sk)

        # Idempotent: same turn_id already committed → duplicate
        if turn_id in self._committed_turns.get(sk, set()):
            logger.debug("[ContextManager] commit DUPLICATE sid=%s tid=%s", session_id, turn_id)
            return {
                "accepted": True,
                "write_status": "duplicate",
                "turn_id": turn_id,
            }

        # Write to Observer (synchronous in-memory buffer)
        observer_ok = True
        try:
            self._observer.record_batch(session_id, messages, tenant_id, user_id)
        except Exception as exc:
            observer_ok = False
            logger.warning(
                "[ContextManager] Observer record failed: %s — writing to fallback", exc,
            )
            self._write_fallback(session_id, turn_id, messages, tenant_id, user_id)

        # Mark turn as committed
        self._committed_turns.setdefault(sk, set()).add(turn_id)

        # RL reward for cited URIs (async, non-blocking)
        if cited_uris:
            valid_uris = [u for u in cited_uris if u.startswith("opencortex://")]
            if valid_uris:
                task = asyncio.create_task(self._apply_cited_rewards(valid_uris))
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)

        write_status = "ok" if observer_ok else "fallback"
        if not observer_ok:
            logger.warning(
                "[ContextManager] commit FALLBACK sid=%s tid=%s", session_id, turn_id,
            )
        else:
            logger.info(
                "[ContextManager] commit sid=%s tid=%s messages=%d cited=%d",
                session_id, turn_id, len(messages),
                len(cited_uris) if cited_uris else 0,
            )

        return {
            "accepted": True,
            "write_status": write_status,
            "turn_id": turn_id,
            "session_turns": len(self._committed_turns.get(sk, set())),
        }

    # =========================================================================
    # Phase: end
    # =========================================================================

    async def _end(
        self,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        sk = self._make_session_key(tenant_id, user_id, session_id)
        total_turns = len(self._committed_turns.get(sk, set()))

        # Delegate to orchestrator.session_end() — includes:
        # Observer.flush → TraceSplitter → TraceStore → Archivist
        start_time = time.monotonic()
        status = "closed"
        traces = 0
        knowledge_candidates = 0

        try:
            result = await self._orchestrator.session_end(
                session_id=session_id,
                quality_score=0.5,
            )
            traces = result.get("alpha_traces", 0)
            knowledge_candidates = result.get("knowledge_candidates", 0)
        except Exception as exc:
            logger.warning("[ContextManager] session_end failed: %s", exc)
            status = "partial"

        duration_ms = int((time.monotonic() - start_time) * 1000)

        # Cleanup session state
        self._cleanup_session(sk)

        logger.info(
            "[ContextManager] end sid=%s turns=%d traces=%d latency=%dms",
            session_id, total_turns, traces, duration_ms,
        )

        return {
            "session_id": session_id,
            "status": status,
            "total_turns": total_turns,
            "traces": traces,
            "knowledge_candidates": knowledge_candidates,
            "duration_ms": duration_ms,
        }

    # =========================================================================
    # Cache management
    # =========================================================================

    def _cache_prepare(self, cache_key: CacheKey, sk: SessionKey, result: Dict) -> None:
        """Cache prepare result with reverse index for session cleanup."""
        now = time.time()

        # LRU eviction: over 1000 entries → evict oldest
        if len(self._prepare_cache) >= 1000:
            oldest_key = min(
                self._prepare_cache, key=lambda k: self._prepare_cache[k][1],
            )
            self._prepare_cache.pop(oldest_key)
            for keys in self._session_cache_keys.values():
                keys.discard(oldest_key)

        self._prepare_cache[cache_key] = (result, now)
        self._session_cache_keys.setdefault(sk, set()).add(cache_key)

    def _get_cached_prepare(self, cache_key: CacheKey) -> Optional[Dict]:
        """Return cached result if exists and not expired."""
        entry = self._prepare_cache.get(cache_key)
        if entry is None:
            return None
        result, ts = entry
        if time.time() - ts > self._prepare_cache_ttl:
            self._prepare_cache.pop(cache_key, None)
            return None
        return result

    # =========================================================================
    # Session state helpers
    # =========================================================================

    def _make_session_key(
        self, tenant_id: str, user_id: str, session_id: str,
    ) -> SessionKey:
        return (tenant_id, user_id, session_id)

    def _touch_session(self, sk: SessionKey) -> None:
        self._session_activity[sk] = time.time()

    def _cleanup_session(self, sk: SessionKey) -> None:
        """Remove all session state including cache entries via reverse index."""
        cache_keys = self._session_cache_keys.pop(sk, set())
        for key in cache_keys:
            self._prepare_cache.pop(key, None)
        self._committed_turns.pop(sk, None)
        self._session_activity.pop(sk, None)
        self._session_locks.pop(sk, None)

    # =========================================================================
    # Idle session auto-close
    # =========================================================================

    async def _idle_session_loop(self) -> None:
        """Periodic sweep to auto-close idle sessions."""
        while True:
            await asyncio.sleep(self._idle_check_interval)
            now = time.time()
            expired = [
                sk for sk, ts in self._session_activity.items()
                if now - ts > self._session_idle_ttl
            ]
            for sk in expired:
                tid, uid, sid = sk
                logger.info(
                    "[ContextManager] idle-close sid=%s (tenant=%s, user=%s)",
                    sid, tid, uid,
                )
                try:
                    # Set contextvars for orchestrator.session_end()
                    tokens = set_request_identity(tid, uid)
                    try:
                        await self._end(sid, tid, uid)
                    finally:
                        reset_request_identity(tokens)
                except Exception as exc:
                    logger.warning(
                        "[ContextManager] Auto-close failed for %s: %s", sid, exc,
                    )

    # =========================================================================
    # Formatting helpers
    # =========================================================================

    def _extract_query(self, messages: List[Dict[str, str]]) -> str:
        """Extract the last user message content as query."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return msg.get("content", "").strip()
        return ""

    def _empty_prepare(self, session_id: str, turn_id: str) -> Dict[str, Any]:
        """Return an empty prepare result (no query to search)."""
        return {
            "session_id": session_id,
            "turn_id": turn_id,
            "intent": {
                "should_recall": False,
                "intent_type": "unknown",
                "detail_level": "l1",
            },
            "memory": [],
            "knowledge": [],
            "instructions": {
                "should_cite_memory": False,
                "memory_confidence": 0.0,
                "recall_count": 0,
                "guidance": "",
            },
        }

    def _format_memories(
        self, find_result, detail_level: str,
    ) -> List[Dict[str, Any]]:
        """Format FindResult into response items."""
        items = []
        for matched in find_result:
            item: Dict[str, Any] = {
                "uri": matched.uri,
                "abstract": matched.abstract,
                "score": round(matched.score, 3),
                "context_type": str(matched.context_type),
                "category": matched.category,
            }
            if detail_level in ("l1", "l2") and matched.overview:
                item["overview"] = self._clamp(matched.overview)
            if detail_level == "l2" and matched.content:
                item["content"] = self._clamp(matched.content)
            items.append(item)
        return items

    def _format_knowledge(
        self, results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Format knowledge search results."""
        items = []
        for r in results:
            items.append({
                "knowledge_id": r.get("knowledge_id", r.get("id", "")),
                "type": r.get("knowledge_type", ""),
                "abstract": r.get("abstract", ""),
                "confidence": r.get("confidence", 0.0),
            })
        return items

    def _clamp(self, text: str) -> str:
        """Hard limit per-item content to max_content_chars."""
        if len(text) <= self._max_content_chars:
            return text
        return text[: self._max_content_chars] + "...[truncated]"

    def _build_instructions(
        self,
        intent: SearchIntent,
        memory_items: List[Dict[str, Any]],
        knowledge_items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build instructions for Agent based on intent and results."""
        total_items = len(memory_items) + len(knowledge_items)

        if total_items == 0:
            return {
                "should_cite_memory": False,
                "memory_confidence": 0.0,
                "recall_count": 0,
                "guidance": "",
            }

        avg_score = (
            sum(m.get("score", 0) for m in memory_items) / max(len(memory_items), 1)
        )
        max_confidence = max(
            [k.get("confidence", 0) for k in knowledge_items],
            default=0.0,
        )
        confidence = max(avg_score, max_confidence)

        guidance_map = {
            "quick_lookup": "Relevant context found. Reference if directly applicable.",
            "deep_analysis": "Multiple related memories retrieved. Synthesize with retrieved context for comprehensive analysis.",
            "recent_recall": "Recent session context retrieved. Continue from where the conversation left off.",
            "summarize": "Historical context loaded. Summarize key themes and patterns.",
            "personalized": "User preferences and past patterns retrieved. Adapt response accordingly.",
        }
        guidance = guidance_map.get(
            intent.intent_type, "Context available for reference.",
        )

        return {
            "should_cite_memory": confidence >= 0.5,
            "memory_confidence": round(confidence, 3),
            "recall_count": total_items,
            "guidance": guidance,
        }

    # =========================================================================
    # Fallback log
    # =========================================================================

    def _write_fallback(
        self,
        session_id: str,
        turn_id: str,
        messages: List[Dict[str, str]],
        tenant_id: str,
        user_id: str,
    ) -> None:
        """Write commit messages to fallback JSONL when Observer fails."""
        try:
            data_root = self._orchestrator._config.data_root
            fallback_path = Path(data_root) / "commit_fallback.jsonl"
            entry = {
                "session_id": session_id,
                "turn_id": turn_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "messages": messages,
                "timestamp": time.time(),
            }
            with open(fallback_path, "ab") as f:
                f.write(json.dumps(entry) + b"\n")
        except Exception as exc:
            logger.error(
                "[ContextManager] Failed to write fallback log: %s", exc,
            )

    # =========================================================================
    # RL reward for cited URIs
    # =========================================================================

    async def _apply_cited_rewards(self, uris: List[str]) -> None:
        """Apply +0.1 RL reward to each cited memory URI."""
        for uri in uris:
            try:
                await self._orchestrator.feedback(uri=uri, reward=0.1)
            except Exception as exc:
                logger.debug(
                    "[ContextManager] Reward feedback failed for %s: %s", uri, exc,
                )
