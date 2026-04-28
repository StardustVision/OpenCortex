# SPDX-License-Identifier: Apache-2.0
"""Commit-phase coordination service for ContextManager."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opencortex.http.request_context import (
    reset_request_identity,
    set_request_identity,
)

if TYPE_CHECKING:
    from opencortex.context.manager import ContextManager, SessionKey

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImmediateWriteItem:
    """One prepared message write for conversation immediate storage."""

    text: str
    msg_index: int
    tool_calls: Optional[List[Dict[str, Any]]]
    meta: Dict[str, Any]


class ContextCommitService:
    """Owns ContextManager commit-phase orchestration."""

    def __init__(self, manager: "ContextManager") -> None:
        self._manager = manager

    async def commit(
        self,
        session_id: str,
        turn_id: str,
        messages: List[Dict[str, Any]],
        tenant_id: str,
        user_id: str,
        cited_uris: Optional[List[str]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Commit a turn by recording messages and updating live buffers."""
        manager = self._manager
        sk = manager._make_session_key(tenant_id, user_id, session_id)
        manager._touch_session(sk)
        manager._remember_session_project(sk)
        lock = manager._session_locks.setdefault(sk, asyncio.Lock())

        async with lock:
            duplicate = self._duplicate_response_if_committed(
                sk,
                session_id=session_id,
                turn_id=turn_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            if duplicate is not None:
                return duplicate

            observer_ok = self._record_observer_batch(
                session_id=session_id,
                turn_id=turn_id,
                messages=messages,
                tenant_id=tenant_id,
                user_id=user_id,
                tool_calls=tool_calls,
            )
            manager._committed_turns.setdefault(sk, set()).add(turn_id)

            self._schedule_cited_rewards(cited_uris)
            await self._record_valid_skill_citations(
                sk=sk,
                session_id=session_id,
                turn_id=turn_id,
                tenant_id=tenant_id,
                user_id=user_id,
                cited_uris=cited_uris,
            )

            buffer = manager._conversation_buffers.setdefault(
                sk,
                manager._new_conversation_buffer(),
            )
            write_items = self._build_write_items(
                buffer=buffer,
                messages=messages,
                tool_calls=tool_calls,
            )
            if write_items:
                buffer = await self._write_immediate_and_append_buffer(
                    sk=sk,
                    buffer=buffer,
                    session_id=session_id,
                    turn_id=turn_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    write_items=write_items,
                    tool_calls=tool_calls,
                )

            if buffer.token_count >= manager._merge_trigger_threshold():
                manager._spawn_merge_task(sk, session_id, tenant_id, user_id)

            return self._commit_response(
                sk=sk,
                session_id=session_id,
                turn_id=turn_id,
                tenant_id=tenant_id,
                user_id=user_id,
                messages=messages,
                cited_uris=cited_uris,
                observer_ok=observer_ok,
            )

    def _duplicate_response_if_committed(
        self,
        sk: "SessionKey",
        *,
        session_id: str,
        turn_id: str,
        tenant_id: str,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        manager = self._manager
        if turn_id not in manager._committed_turns.get(sk, set()):
            return None
        logger.debug(
            "[ContextManager] commit DUPLICATE sid=%s turn=%s tenant=%s user=%s",
            session_id,
            turn_id,
            tenant_id,
            user_id,
        )
        return {
            "accepted": True,
            "write_status": "duplicate",
            "turn_id": turn_id,
        }

    def _record_observer_batch(
        self,
        *,
        session_id: str,
        turn_id: str,
        messages: List[Dict[str, Any]],
        tenant_id: str,
        user_id: str,
        tool_calls: Optional[List[Dict[str, Any]]],
    ) -> bool:
        manager = self._manager
        try:
            manager._observer.record_batch(
                manager._observer_session_id(
                    session_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                ),
                messages,
                tenant_id,
                user_id,
                tool_calls=tool_calls,
            )
            return True
        except Exception as exc:
            logger.warning(
                "[ContextManager] Observer record failed sid=%s turn=%s tenant=%s user=%s: %s "
                "— writing to fallback",
                session_id,
                turn_id,
                tenant_id,
                user_id,
                exc,
            )
            manager._write_fallback(session_id, turn_id, messages, tenant_id, user_id)
            return False

    def _schedule_cited_rewards(self, cited_uris: Optional[List[str]]) -> None:
        if not cited_uris:
            return
        valid_uris = [uri for uri in cited_uris if uri.startswith("opencortex://")]
        if not valid_uris:
            return
        manager = self._manager
        task = asyncio.create_task(manager._apply_cited_rewards(valid_uris))
        manager._pending_tasks.add(task)
        task.add_done_callback(manager._pending_tasks.discard)

    async def _record_valid_skill_citations(
        self,
        *,
        sk: "SessionKey",
        session_id: str,
        turn_id: str,
        tenant_id: str,
        user_id: str,
        cited_uris: Optional[List[str]],
    ) -> None:
        manager = self._manager
        if (
            not cited_uris
            or not hasattr(manager._orchestrator, "_skill_event_store")
            or not manager._orchestrator._skill_event_store
        ):
            return

        skill_uris = [uri for uri in cited_uris if "/skills/" in uri]
        server_selected = manager._selected_skill_uris.get((sk, turn_id), set())
        for uri in skill_uris:
            if uri not in server_selected:
                logger.debug("[ContextManager] Dropped forged skill citation: %s", uri)
                continue
            await manager._append_skill_event(
                session_id,
                turn_id,
                uri,
                tenant_id,
                user_id,
                "cited",
            )

    def _build_write_items(
        self,
        *,
        buffer: Any,
        messages: List[Dict[str, Any]],
        tool_calls: Optional[List[Dict[str, Any]]],
    ) -> List[ImmediateWriteItem]:
        manager = self._manager
        write_items: List[ImmediateWriteItem] = []
        for index, msg in enumerate(messages):
            text = msg.get(
                "content",
                msg.get("assistant_response", msg.get("user_message", "")),
            )
            if not text:
                continue
            msg_meta = dict(msg.get("meta") or {})
            stored_text = manager._decorate_message_text(text, msg_meta)
            role = msg.get("role", "")
            msg_index = buffer.start_msg_index + len(buffer.messages) + index
            msg_tool_calls = tool_calls if role == "assistant" else None
            write_items.append(
                ImmediateWriteItem(
                    text=stored_text,
                    msg_index=msg_index,
                    tool_calls=msg_tool_calls,
                    meta=msg_meta,
                )
            )
        return write_items

    async def _write_immediate_and_append_buffer(
        self,
        *,
        sk: "SessionKey",
        buffer: Any,
        session_id: str,
        turn_id: str,
        tenant_id: str,
        user_id: str,
        write_items: List[ImmediateWriteItem],
        tool_calls: Optional[List[Dict[str, Any]]],
    ) -> Any:
        manager = self._manager
        tokens_for_identity = set_request_identity(tenant_id, user_id)
        try:
            results = await asyncio.gather(
                *[
                    manager._orchestrator._write_immediate(
                        session_id=session_id,
                        msg_index=item.msg_index,
                        text=item.text,
                        tool_calls=item.tool_calls,
                        meta=item.meta,
                    )
                    for item in write_items
                ],
                return_exceptions=True,
            )
        finally:
            reset_request_identity(tokens_for_identity)

        merge_lock = manager._session_merge_locks.setdefault(sk, asyncio.Lock())
        async with merge_lock:
            active_buffer = manager._conversation_buffers.get(sk)
            if active_buffer is None:
                active_buffer = buffer
                manager._conversation_buffers[sk] = active_buffer
            elif active_buffer is not buffer:
                logger.debug(
                    "[ContextManager] commit detected buffer rollover "
                    "sid=%s turn=%s tenant=%s user=%s old_start=%d new_start=%d",
                    session_id,
                    turn_id,
                    tenant_id,
                    user_id,
                    buffer.start_msg_index,
                    active_buffer.start_msg_index,
                )

            for item, result in zip(
                write_items,
                results,
            ):
                if isinstance(result, Exception):
                    logger.warning(
                        (
                            "[ContextManager] Immediate write failed"
                            " sid=%s turn=%s msg_index=%d chars=%d"
                            " exc_type=%s exc=%r"
                        ),
                        session_id,
                        turn_id,
                        item.msg_index,
                        len(item.text),
                        type(result).__name__,
                        result,
                        exc_info=(
                            type(result),
                            result,
                            result.__traceback__,
                        ),
                    )
                    continue
                active_buffer.messages.append(item.text)
                active_buffer.immediate_uris.append(result)
                active_buffer.token_count += manager._estimate_tokens(item.text)

            if tool_calls:
                active_buffer.tool_calls_per_turn.append(tool_calls)

            return active_buffer

    def _commit_response(
        self,
        *,
        sk: "SessionKey",
        session_id: str,
        turn_id: str,
        tenant_id: str,
        user_id: str,
        messages: List[Dict[str, Any]],
        cited_uris: Optional[List[str]],
        observer_ok: bool,
    ) -> Dict[str, Any]:
        manager = self._manager
        write_status = "ok" if observer_ok else "fallback"
        if not observer_ok:
            logger.warning(
                "[ContextManager] commit FALLBACK sid=%s turn=%s tenant=%s user=%s",
                session_id,
                turn_id,
                tenant_id,
                user_id,
            )
        else:
            logger.info(
                "[ContextManager] commit sid=%s turn=%s tenant=%s user=%s messages=%d cited=%d",
                session_id,
                turn_id,
                tenant_id,
                user_id,
                len(messages),
                len(cited_uris) if cited_uris else 0,
            )

        return {
            "accepted": True,
            "write_status": write_status,
            "turn_id": turn_id,
            "session_turns": len(manager._committed_turns.get(sk, set())),
        }
