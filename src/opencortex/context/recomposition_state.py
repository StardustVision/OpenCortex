# SPDX-License-Identifier: Apache-2.0
"""State and storage helpers for session recomposition."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opencortex.context.recomposition_input import RecompositionInputService

if TYPE_CHECKING:
    from opencortex.context.manager import (
        ContextManager,
        ConversationBuffer,
        SessionKey,
    )

logger = logging.getLogger(__name__)


class RecompositionStateService:
    """Own mutable buffer state and storage cleanup used by recomposition."""

    def __init__(
        self,
        manager: "ContextManager",
        input_service: RecompositionInputService,
    ) -> None:
        """Create a state service bound to one context manager."""
        self._manager = manager
        self._input = input_service

    def merge_trigger_threshold(self) -> int:
        """Return the token threshold that triggers a background merge."""
        cfg = getattr(self._manager._orchestrator, "_config", None)
        if cfg is None:
            return 6144
        return max(1, int(getattr(cfg, "conversation_merge_token_budget", 6144)))

    async def purge_records_and_fs_subtree(self, uris: List[str]) -> None:
        """Purge each URI's record and CortexFS subtree by URI prefix."""
        unique_uris: list[str] = []
        for uri in uris:
            normalized = str(uri or "").strip()
            if normalized and normalized not in unique_uris:
                unique_uris.append(normalized)

        orchestrator = self._manager._orchestrator
        fs = getattr(orchestrator, "_fs", None)
        for uri in unique_uris:
            await orchestrator._storage.remove_by_uri(
                orchestrator._get_collection(),
                uri,
            )
            if fs:
                try:
                    await fs.rm(uri, recursive=True)
                except Exception as exc:
                    logger.warning(
                        "[ContextManager] CortexFS cleanup failed for %s: %s",
                        uri,
                        exc,
                    )

    async def list_immediate_uris(self, session_id: str) -> List[str]:
        """Return current session immediate source URIs for fallback cleanup."""
        orchestrator = self._manager._orchestrator
        records = await orchestrator._storage.filter(
            orchestrator._get_collection(),
            {"op": "must", "field": "session_id", "conds": [session_id]},
            limit=10000,
        )
        return [
            str(record.get("uri", "")).strip()
            for record in records
            if (
                str(record.get("uri", "")).strip()
                and str((record.get("meta") or {}).get("layer", "") or "")
                == "immediate"
            )
        ]

    async def load_immediate_records(
        self,
        immediate_uris: List[str],
    ) -> List[Dict[str, Any]]:
        """Load immediate records and return them ordered by message index."""
        return await self._input.load_immediate_records(immediate_uris)

    async def take_merge_snapshot(
        self,
        sk: "SessionKey",
        *,
        flush_all: bool,
    ) -> Optional["ConversationBuffer"]:
        """Detach the current buffer snapshot for merge processing."""
        from opencortex.context.manager import ConversationBuffer

        merge_lock = self._manager._recomposition_tasks.merge_lock(sk)
        async with merge_lock:
            buffer = self._manager._conversation_buffers.get(sk)
            if not buffer or not buffer.messages:
                return None
            if not flush_all and buffer.token_count < self.merge_trigger_threshold():
                return None

            snapshot = ConversationBuffer(
                messages=list(buffer.messages),
                token_count=buffer.token_count,
                start_msg_index=buffer.start_msg_index,
                immediate_uris=list(buffer.immediate_uris),
                tool_calls_per_turn=[list(item) for item in buffer.tool_calls_per_turn],
            )
            next_start = buffer.start_msg_index + len(buffer.messages)
            self._manager._conversation_buffers[sk] = ConversationBuffer(
                start_msg_index=next_start,
            )
            return snapshot

    async def restore_merge_snapshot(
        self,
        sk: "SessionKey",
        snapshot: "ConversationBuffer",
    ) -> None:
        """Restore a detached buffer snapshot after merge failure."""
        from opencortex.context.manager import ConversationBuffer

        merge_lock = self._manager._recomposition_tasks.merge_lock(sk)
        async with merge_lock:
            current = self._manager._conversation_buffers.get(sk)
            if current is None:
                self._manager._conversation_buffers[sk] = snapshot
                return

            merged = ConversationBuffer(
                messages=list(snapshot.messages) + list(current.messages),
                token_count=snapshot.token_count + current.token_count,
                start_msg_index=snapshot.start_msg_index,
                immediate_uris=list(snapshot.immediate_uris)
                + list(current.immediate_uris),
                tool_calls_per_turn=list(snapshot.tool_calls_per_turn)
                + list(current.tool_calls_per_turn),
            )
            self._manager._conversation_buffers[sk] = merged
