# SPDX-License-Identifier: Apache-2.0
"""Session message buffer state."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from opencortex.core.identity import IdentityProfile
from opencortex.parse.base import estimate_tokens

SessionKey = tuple[str, str, str, str]


@dataclass
class ConversationBuffer:
    """Per-session immediate-message buffer."""

    messages: list[str] = field(default_factory=list)
    token_count: int = 0
    start_msg_index: int = 0
    immediate_uris: list[str] = field(default_factory=list)
    tool_calls_per_turn: list[list[dict[str, Any]]] = field(default_factory=list)


@dataclass(frozen=True)
class SessionBufferSnapshot:
    """Immutable session buffer snapshot used by merge/end."""

    messages: list[str]
    token_count: int
    start_msg_index: int
    immediate_uris: list[str]
    tool_calls_per_turn: list[list[dict[str, Any]]]


class SessionBuffer:
    """Own per-session locks and immediate-message buffer state."""

    def __init__(
        self,
        *,
        collection_resolver: Any,
        merge_token_budget: int,
        idle_ttl_seconds: float = 1800.0,
    ) -> None:
        self.collection_resolver = collection_resolver
        self.merge_token_budget = max(1, int(merge_token_budget or 1))
        self.idle_ttl_seconds = max(1.0, float(idle_ttl_seconds))
        self.locks: dict[SessionKey, asyncio.Lock] = {}
        self.activity: dict[SessionKey, float] = {}
        self.project_ids: dict[SessionKey, str] = {}
        self.buffers: dict[SessionKey, ConversationBuffer] = {}

    def session_key(
        self,
        *,
        collection: str | None = None,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> SessionKey:
        """Build the scoped session state key."""
        return (collection or self.collection_resolver(), tenant_id, user_id, session_id)

    def profile_key(self, profile: IdentityProfile) -> SessionKey:
        """Build the scoped session key from an identity profile."""
        return self.session_key(
            collection=profile.collection or None,
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            session_id=profile.session_id,
        )

    def lock(self, key: SessionKey) -> asyncio.Lock:
        """Return the lock for one session key."""
        self.prune_idle()
        return self.locks.setdefault(key, asyncio.Lock())

    def touch(self, key: SessionKey, profile: IdentityProfile | None = None) -> None:
        """Record current activity and project context for the session."""
        self.activity[key] = time.time()
        if profile is not None:
            self.project_ids[key] = profile.project_id

    def next_msg_index(self, key: SessionKey) -> int:
        """Return the next message index in the active buffer."""
        buffer = self.buffers.setdefault(key, ConversationBuffer())
        return buffer.start_msg_index + len(buffer.messages)

    def append(
        self,
        key: SessionKey,
        *,
        text: str,
        record_uri: str,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        """Append one written immediate record into the active buffer."""
        buffer = self.buffers.setdefault(key, ConversationBuffer())
        buffer.messages.append(text)
        buffer.immediate_uris.append(record_uri)
        buffer.token_count += estimate_tokens(text)
        if tool_calls:
            buffer.tool_calls_per_turn.append(list(tool_calls))

    def should_merge(self, key: SessionKey) -> bool:
        """Return whether buffered messages should be merged."""
        buffer = self.buffers.get(key)
        if buffer is None:
            return False
        return buffer.token_count >= self.merge_token_budget

    def snapshot(self, key: SessionKey) -> SessionBufferSnapshot | None:
        """Detach current buffered messages for synchronous merge."""
        buffer = self.buffers.get(key)
        if buffer is None or not buffer.messages:
            return None
        snapshot = SessionBufferSnapshot(
            messages=list(buffer.messages),
            token_count=buffer.token_count,
            start_msg_index=buffer.start_msg_index,
            immediate_uris=list(buffer.immediate_uris),
            tool_calls_per_turn=[list(item) for item in buffer.tool_calls_per_turn],
        )
        self.buffers[key] = ConversationBuffer(
            start_msg_index=buffer.start_msg_index + len(buffer.messages),
        )
        return snapshot

    def prune_idle(self) -> int:
        """Drop idle session state and return the number of removed sessions."""
        now = time.time()
        stale_keys = [
            key
            for key, last_seen in self.activity.items()
            if now - last_seen >= self.idle_ttl_seconds
        ]
        for key in stale_keys:
            self.drop(key)
        return len(stale_keys)

    def drop(self, key: SessionKey) -> None:
        """Drop all in-memory state for one session key."""
        self.locks.pop(key, None)
        self.activity.pop(key, None)
        self.project_ids.pop(key, None)
        self.buffers.pop(key, None)

    def clear(self) -> None:
        """Drop all in-memory session state."""
        self.locks.clear()
        self.activity.clear()
        self.project_ids.clear()
        self.buffers.clear()
