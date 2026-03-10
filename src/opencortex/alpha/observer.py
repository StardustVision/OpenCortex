"""
Observer — real-time prompt/response recording.

Records every message server-side so transcript is not lost on crash.
Supports single message and batch recording (client debounce buffer).

Design doc §5.1, §9.1.
"""

import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class Observer:
    """In-memory transcript accumulator for active sessions."""

    def __init__(self):
        # session_id -> list of message dicts
        self._transcripts: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        # session_id -> session metadata
        self._session_meta: Dict[str, Dict[str, Any]] = {}

    def begin_session(
        self, session_id: str, tenant_id: str, user_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._session_meta[session_id] = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "started_at": time.time(),
            **(meta or {}),
        }
        # Ensure transcript list exists
        if session_id not in self._transcripts:
            self._transcripts[session_id] = []

    def record_message(
        self, session_id: str, role: str, content: str,
        tenant_id: str, user_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a single message to the session transcript."""
        self._transcripts[session_id].append({
            "role": role,
            "content": content,
            "timestamp": time.time(),
            **(meta or {}),
        })

    def record_batch(
        self, session_id: str, messages: List[Dict[str, Any]],
        tenant_id: str, user_id: str,
    ) -> None:
        """Record a batch of messages (from client debounce buffer)."""
        for msg in messages:
            self._transcripts[session_id].append({
                "role": msg["role"],
                "content": msg["content"],
                "timestamp": msg.get("timestamp", time.time()),
            })

    def get_transcript(self, session_id: str) -> List[Dict[str, Any]]:
        """Get full transcript for a session (non-destructive)."""
        return list(self._transcripts.get(session_id, []))

    def get_session_meta(self, session_id: str) -> Dict[str, Any]:
        """Get session metadata."""
        return self._session_meta.get(session_id, {})

    def flush(self, session_id: str) -> List[Dict[str, Any]]:
        """Get transcript and remove session from memory."""
        transcript = list(self._transcripts.pop(session_id, []))
        self._session_meta.pop(session_id, None)
        return transcript

    def active_sessions(self) -> List[str]:
        """List active session IDs."""
        return list(self._transcripts.keys())
