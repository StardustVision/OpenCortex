# SPDX-License-Identifier: Apache-2.0
"""
Session manager for OpenCortex.

Manages session lifecycle (begin/add_message/end) and triggers
memory extraction + deduplication at session end.
"""

import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from opencortex.session.extractor import MemoryExtractor
from opencortex.session.types import (
    ExtractionResult,
    ExtractedMemory,
    Message,
    SessionContext,
)

logger = logging.getLogger(__name__)

LLMCompletionCallable = Callable[[str], Awaitable[str]]

# Semantic dedup threshold — memories with similarity >= this are merged
_DEDUP_THRESHOLD = 0.85
# Minimum confidence to store a memory
_MIN_CONFIDENCE = 0.3

# Categories where existing memories should be updated (merged) rather than
# creating duplicates.  Non-mergeable categories (events, cases) always produce
# new records because each occurrence is unique.
MERGEABLE_CATEGORIES = {"profile", "preferences", "entities", "patterns"}


class SessionManager:
    """Manages session lifecycle and memory extraction.

    Args:
        llm_completion: Async LLM callable for memory extraction.
        store_fn: Async callable to store a memory (abstract, content, category, context_type, uri_hint).
        search_fn: Async callable to search existing memories (query) -> list of dicts with 'uri', 'score'.
        update_fn: Async callable to update an existing memory (uri, abstract, content).
        feedback_fn: Async callable to send feedback (uri, reward).
    """

    def __init__(
        self,
        llm_completion: Optional[LLMCompletionCallable] = None,
        store_fn: Optional[Callable] = None,
        search_fn: Optional[Callable] = None,
        update_fn: Optional[Callable] = None,
        feedback_fn: Optional[Callable] = None,
    ):
        self._llm_completion = llm_completion
        self._store_fn = store_fn
        self._search_fn = search_fn
        self._update_fn = update_fn
        self._feedback_fn = feedback_fn
        self._extractor: Optional[MemoryExtractor] = None
        self._sessions: Dict[str, SessionContext] = {}

        if llm_completion:
            self._extractor = MemoryExtractor(llm_completion)

    async def begin(
        self,
        session_id: str,
        tenant_id: str = "",
        user_id: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> SessionContext:
        """Begin a new session.

        Args:
            session_id: Unique session identifier.
            tenant_id: Tenant for URI generation.
            user_id: User for URI generation.
            meta: Optional metadata.

        Returns:
            SessionContext for the new session.
        """
        ctx = SessionContext(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            started_at=time.time(),
            meta=meta or {},
        )
        self._sessions[session_id] = ctx
        logger.info("[SessionManager] Session started: %s", session_id)
        return ctx

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Add a message to an active session.

        Args:
            session_id: Session to add to.
            role: Message role ("user" | "assistant" | "system").
            content: Message content.
            meta: Optional metadata.

        Returns:
            True if added, False if session not found.
        """
        ctx = self._sessions.get(session_id)
        if not ctx:
            logger.warning("[SessionManager] Session not found: %s", session_id)
            return False

        msg = Message(
            role=role,
            content=content,
            timestamp=time.time(),
            meta=meta or {},
        )
        ctx.messages.append(msg)
        return True

    async def end(
        self,
        session_id: str,
        quality_score: float = 0.5,
    ) -> ExtractionResult:
        """End a session and trigger memory extraction.

        Performs:
        1. LLM-driven memory extraction from conversation
        2. Semantic deduplication against existing memories
        3. Store new / update existing memories via Viking FS

        Args:
            session_id: Session to end.
            quality_score: Overall session quality (0-1).

        Returns:
            ExtractionResult with counts of stored/merged/skipped memories.
        """
        ctx = self._sessions.pop(session_id, None)
        if not ctx:
            logger.warning("[SessionManager] Session not found for end: %s", session_id)
            return ExtractionResult(session_id=session_id)

        result = ExtractionResult(
            session_id=session_id,
            quality_score=quality_score,
        )

        if not self._extractor:
            logger.info("[SessionManager] No LLM — skipping memory extraction")
            return result

        if not ctx.messages:
            logger.info("[SessionManager] Empty session — skipping extraction")
            return result

        # Step 1: Extract memories from conversation
        extracted = await self._extractor.extract(
            messages=ctx.messages,
            quality_score=quality_score,
            session_summary=ctx.summary,
        )

        result.memories = extracted
        logger.info(
            "[SessionManager] Extracted %d candidate memories from session %s",
            len(extracted),
            session_id,
        )

        # Step 2: Deduplicate and store
        for memory in extracted:
            if memory.confidence < _MIN_CONFIDENCE:
                result.skipped_count += 1
                continue

            # Check for existing similar memories (dedup)
            is_merged = await self._try_merge(memory)
            if is_merged:
                result.merged_count += 1
            else:
                stored = await self._store_memory(memory)
                if stored:
                    result.stored_count += 1
                else:
                    result.skipped_count += 1

        logger.info(
            "[SessionManager] Session %s extraction done: stored=%d, merged=%d, skipped=%d",
            session_id,
            result.stored_count,
            result.merged_count,
            result.skipped_count,
        )
        return result

    async def _try_merge(self, memory: ExtractedMemory) -> bool:
        """Try to merge with an existing similar memory.

        Only attempts merge for categories in MERGEABLE_CATEGORIES (profile,
        preferences, entities, patterns).  Non-mergeable categories (events,
        cases) always create new records — each occurrence is unique.

        Returns True if merged, False if no similar memory found or category
        is not mergeable.
        """
        if not self._search_fn:
            return False

        # Non-mergeable categories always create new records
        if memory.category not in MERGEABLE_CATEGORIES:
            return False

        try:
            results = await self._search_fn(memory.abstract)
            for r in results:
                score = r.get("score", 0.0)
                if score >= _DEDUP_THRESHOLD:
                    uri = r.get("uri", "")
                    if uri and self._update_fn:
                        # Merge: update existing memory with new content
                        merged_content = f"{r.get('content', '')}\n---\n{memory.content}".strip()
                        await self._update_fn(uri, memory.abstract, merged_content)
                        # Reinforce the existing memory
                        if self._feedback_fn:
                            await self._feedback_fn(uri, 0.5)
                        logger.debug("[SessionManager] Merged memory into %s", uri)
                        return True
        except Exception as exc:
            logger.warning("[SessionManager] Dedup search failed: %s", exc)

        return False

    async def _store_memory(self, memory: ExtractedMemory) -> bool:
        """Store a new extracted memory."""
        if not self._store_fn:
            return False

        try:
            await self._store_fn(
                abstract=memory.abstract,
                content=memory.content,
                category=memory.category,
                context_type=memory.context_type,
            )
            return True
        except Exception as exc:
            logger.warning("[SessionManager] Failed to store memory: %s", exc)
            return False

    def get_session(self, session_id: str) -> Optional[SessionContext]:
        """Get an active session context."""
        return self._sessions.get(session_id)

    def active_sessions(self) -> List[str]:
        """Return list of active session IDs."""
        return list(self._sessions.keys())
