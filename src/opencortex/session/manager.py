# SPDX-License-Identifier: Apache-2.0
"""
Session manager for OpenCortex.

Manages session lifecycle (begin/add_message/end) and triggers
memory extraction + deduplication at session end.

Write-time dedup is now handled by ``orchestrator.add(dedup=True)``
so the session manager no longer needs its own merge logic.
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

# Minimum confidence to store a memory
_MIN_CONFIDENCE = 0.3


class SessionManager:
    """Manages session lifecycle and memory extraction.

    Args:
        llm_completion: Async LLM callable for memory extraction.
        store_fn: Async callable to store a memory.  Must accept keyword
            args ``abstract``, ``content``, ``category``, ``context_type``
            and return an object with a ``.meta`` dict containing
            ``dedup_action`` (``"created"`` | ``"merged"`` | ``"skipped"``).
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
        # search_fn, update_fn, feedback_fn kept for backward compat but
        # no longer used — dedup is handled by orchestrator.add().
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
        2. Store each memory via store_fn (which runs write-time dedup)
        3. Classify result as created / merged / skipped from dedup_action

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

        # Step 2: Store (dedup handled by orchestrator.add)
        for memory in extracted:
            if memory.confidence < _MIN_CONFIDENCE:
                result.skipped_count += 1
                continue

            stored = await self._store_memory(memory)
            if stored == "merged":
                result.merged_count += 1
            elif stored == "skipped":
                result.skipped_count += 1
            elif stored == "created":
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

    async def extract_turn(
        self,
        session_id: str,
        quality_score: float = 0.5,
    ) -> ExtractionResult:
        """Extract memories from the latest turn without ending the session.

        Takes the last 2 messages (1 user + 1 assistant) and runs LLM extraction.
        Does NOT remove the session — it continues accumulating messages.
        """
        ctx = self._sessions.get(session_id)
        if not ctx:
            logger.warning("[SessionManager] extract_turn: session not found: %s", session_id)
            return ExtractionResult(session_id=session_id)

        result = ExtractionResult(session_id=session_id, quality_score=quality_score)

        if not self._extractor:
            return result

        # Take last 2 messages (the latest turn)
        recent = ctx.messages[-2:] if len(ctx.messages) >= 2 else ctx.messages[:]
        if not recent:
            return result

        extracted = await self._extractor.extract(
            messages=recent,
            quality_score=quality_score,
        )
        result.memories = extracted

        for memory in extracted:
            if memory.confidence < _MIN_CONFIDENCE:
                result.skipped_count += 1
                continue
            stored = await self._store_memory(memory)
            if stored == "merged":
                result.merged_count += 1
            elif stored == "skipped":
                result.skipped_count += 1
            elif stored == "created":
                result.stored_count += 1
            else:
                result.skipped_count += 1

        logger.info(
            "[SessionManager] extract_turn %s: stored=%d, merged=%d, skipped=%d",
            session_id, result.stored_count, result.merged_count, result.skipped_count,
        )
        return result

    async def _store_memory(self, memory: ExtractedMemory) -> str:
        """Store a memory via store_fn and return the dedup_action.

        Returns:
            ``"created"``, ``"merged"``, ``"skipped"``, or ``""`` on error.
        """
        if not self._store_fn:
            return ""

        try:
            ctx = await self._store_fn(
                abstract=memory.abstract,
                content=memory.content,
                category=memory.category,
                context_type=memory.context_type,
                meta=memory.meta,
            )
            # store_fn returns a Context with meta["dedup_action"]
            if hasattr(ctx, "meta") and isinstance(ctx.meta, dict):
                return ctx.meta.get("dedup_action", "created")
            return "created"
        except Exception as exc:
            logger.warning("[SessionManager] Failed to store memory: %s", exc)
            return ""

    def get_session(self, session_id: str) -> Optional[SessionContext]:
        """Get an active session context."""
        return self._sessions.get(session_id)

    def active_sessions(self) -> List[str]:
        """Return list of active session IDs."""
        return list(self._sessions.keys())
