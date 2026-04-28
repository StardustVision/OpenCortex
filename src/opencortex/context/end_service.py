# SPDX-License-Identifier: Apache-2.0
"""End-phase coordination service for ContextManager."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opencortex.http.request_context import (
    get_effective_project_id,
    reset_request_project_id,
    set_request_project_id,
)

if TYPE_CHECKING:
    from opencortex.context.manager import ContextManager, SessionKey

logger = logging.getLogger(__name__)


@dataclass
class EndRunState:
    """Mutable state for one context end run."""

    start_time: float
    total_turns: int
    fail_fast: bool
    status: str = "closed"
    traces: int = 0
    knowledge_candidates: int = 0
    source_uri: Optional[str] = None

    @property
    def duration_ms(self) -> int:
        """Return elapsed duration in milliseconds."""
        return int((time.monotonic() - self.start_time) * 1000)


class ContextEndService:
    """Owns ContextManager end-phase orchestration."""

    def __init__(self, manager: "ContextManager") -> None:
        self._manager = manager

    async def end(
        self,
        session_id: str,
        tenant_id: str,
        user_id: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """End a session, flushing buffers and triggering post-processing."""
        manager = self._manager
        sk = manager._make_session_key(tenant_id, user_id, session_id)
        session_project_id = (
            manager._session_project_ids.get(sk) or get_effective_project_id()
        )
        project_token = set_request_project_id(session_project_id)
        lock = manager._session_locks.setdefault(sk, asyncio.Lock())

        try:
            async with lock:
                state = EndRunState(
                    start_time=time.monotonic(),
                    total_turns=len(manager._committed_turns.get(sk, set())),
                    fail_fast=bool((config or {}).get("fail_fast_end", False)),
                )
                try:
                    await self._wait_for_background_merge(
                        sk,
                        state,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                    await self._flush_buffer(
                        sk,
                        state,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                    await self._cleanup_pending_immediates(
                        sk,
                        state,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                    await self._persist_source_and_end_session(
                        state,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                    await self._wait_for_merge_followups(
                        sk,
                        state,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                    await self._run_full_recomposition(
                        sk,
                        state,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                    await self._generate_session_summary(
                        state,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                    layer_counts = await self._inspect_layer_counts(
                        state,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                    self._check_layer_integrity(
                        state,
                        layer_counts,
                        session_id=session_id,
                    )
                    return self._success_response(
                        state,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                except Exception as exc:
                    self._log_end_failure(
                        state,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        exc=exc,
                    )
                    raise
                finally:
                    manager._cleanup_session(sk)
        finally:
            reset_request_project_id(project_token)

    def _handle_failure(
        self,
        state: EndRunState,
        message: str,
        exc: Optional[BaseException] = None,
    ) -> None:
        """Log or raise an end-phase error depending on fail-fast mode."""
        if state.fail_fast:
            raise RuntimeError(message) from exc
        state.status = "partial"
        if exc is None:
            logger.warning("[ContextManager] %s", message)
        else:
            logger.warning(
                "[ContextManager] %s: %s",
                message,
                exc,
                exc_info=(
                    type(exc),
                    exc,
                    exc.__traceback__,
                ),
            )

    async def _wait_for_background_merge(
        self,
        sk: "SessionKey",
        state: EndRunState,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        merge_failures = await self._manager._wait_for_merge_task(sk)
        if state.fail_fast and merge_failures:
            self._handle_failure(
                state,
                "Background merge task failed "
                f"sid={session_id} tenant={tenant_id} user={user_id} "
                f"failures={len(merge_failures)}",
                merge_failures[0],
            )

    async def _flush_buffer(
        self,
        sk: "SessionKey",
        state: EndRunState,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        manager = self._manager
        buffer = manager._conversation_buffers.get(sk)
        if not buffer or not buffer.messages:
            return
        try:
            await manager._merge_buffer(
                sk,
                session_id,
                tenant_id,
                user_id,
                flush_all=True,
                raise_on_error=True,
            )
        except Exception as exc:
            self._handle_failure(
                state,
                "End-of-session buffer flush failed "
                f"sid={session_id} tenant={tenant_id} user={user_id}",
                exc,
            )

    async def _cleanup_pending_immediates(
        self,
        sk: "SessionKey",
        state: EndRunState,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        manager = self._manager
        if not manager._session_pending_immediate_cleanup.pop(sk, False):
            return
        try:
            immediate_uris = await manager._list_immediate_uris(session_id)
            await manager._purge_records_and_fs_subtree(immediate_uris)
        except Exception as exc:
            self._handle_failure(
                state,
                "End cleanup immediates failed "
                f"sid={session_id} tenant={tenant_id} user={user_id}",
                exc,
            )

    async def _persist_source_and_end_session(
        self,
        state: EndRunState,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        manager = self._manager
        try:
            state.source_uri = await manager._persist_conversation_source(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            result = await manager._orchestrator.session_end(
                session_id=session_id,
                quality_score=0.5,
            )
            state.traces = result.get("alpha_traces", 0)
            state.knowledge_candidates = result.get("knowledge_candidates", 0)
        except Exception as exc:
            self._handle_failure(
                state,
                "session_end failed "
                f"sid={session_id} tenant={tenant_id} user={user_id}",
                exc,
            )

    async def _wait_for_merge_followups(
        self,
        sk: "SessionKey",
        state: EndRunState,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        followup_failures = await self._manager._wait_for_merge_followup_tasks(sk)
        if state.fail_fast and followup_failures:
            self._handle_failure(
                state,
                "Merge follow-up task failed "
                f"sid={session_id} tenant={tenant_id} user={user_id} "
                f"failures={len(followup_failures)}",
                followup_failures[0],
            )

    async def _run_full_recomposition(
        self,
        sk: "SessionKey",
        state: EndRunState,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        manager = self._manager
        manager._spawn_full_recompose_task(
            sk,
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            source_uri=state.source_uri,
            raise_on_error=state.fail_fast,
        )
        recompose_task = manager._session_full_recompose_tasks.get(sk)
        if recompose_task is None:
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(recompose_task),
                timeout=120.0,
            )
        except asyncio.TimeoutError as exc:
            recompose_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recompose_task
            self._handle_failure(
                state,
                "Full-session recomposition timed out "
                f"sid={session_id} tenant={tenant_id} user={user_id} timeout=120s",
                exc,
            )
        except Exception as exc:
            self._handle_failure(
                state,
                "Full-session recomposition wait failed "
                f"sid={session_id} tenant={tenant_id} user={user_id}",
                exc,
            )

    async def _generate_session_summary(
        self,
        state: EndRunState,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        try:
            await self._manager._generate_session_summary(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                source_uri=state.source_uri,
            )
        except Exception as exc:
            self._handle_failure(
                state,
                f"Session summary generation failed sid={session_id}",
                exc,
            )

    async def _inspect_layer_counts(
        self,
        state: EndRunState,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> Optional[Dict[str, int]]:
        try:
            layer_counts = await self._manager._session_records.layer_counts(
                session_id,
                source_uri=state.source_uri,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            logger.info(
                "[ContextManager] End state sid=%s source_uri=%s layer_counts=%s",
                session_id,
                state.source_uri,
                layer_counts,
            )
            return layer_counts
        except Exception as exc:
            self._handle_failure(
                state,
                f"Failed to inspect end state sid={session_id}",
                exc,
            )
            return None

    def _check_layer_integrity(
        self,
        state: EndRunState,
        layer_counts: Optional[Dict[str, int]],
        *,
        session_id: str,
    ) -> None:
        if layer_counts is None:
            return
        manager = self._manager
        integrity_errors: List[str] = []
        if state.total_turns > 0 and layer_counts.get("merged", 0) == 0:
            integrity_errors.append("merged=0")
        if layer_counts.get("immediate", 0) > 0:
            integrity_errors.append(f"immediate={layer_counts.get('immediate', 0)}")
        if (
            layer_counts.get("merged", 0) >= 2
            and manager._orchestrator._llm_completion is not None
            and layer_counts.get("session_summary", 0) == 0
        ):
            integrity_errors.append("session_summary=0")
        if not integrity_errors:
            return
        if state.fail_fast:
            self._handle_failure(
                state,
                (
                    "End degraded"
                    f" sid={session_id}"
                    f" source_uri={state.source_uri}"
                    f" layer_counts={layer_counts}"
                    f" integrity_errors={integrity_errors}"
                ),
            )
            return
        logger.warning(
            (
                "[ContextManager] End degraded"
                " sid=%s source_uri=%s"
                " layer_counts=%s integrity_errors=%s"
            ),
            session_id,
            state.source_uri,
            layer_counts,
            integrity_errors,
        )

    def _success_response(
        self,
        state: EndRunState,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        duration_ms = state.duration_ms
        logger.info(
            (
                "[ContextManager] end"
                " sid=%s tenant=%s user=%s"
                " turns=%d traces=%d latency=%dms"
            ),
            session_id,
            tenant_id,
            user_id,
            state.total_turns,
            state.traces,
            duration_ms,
        )
        return {
            "session_id": session_id,
            "status": state.status,
            "total_turns": state.total_turns,
            "traces": state.traces,
            "knowledge_candidates": state.knowledge_candidates,
            "duration_ms": duration_ms,
            "source_uri": state.source_uri,
        }

    def _log_end_failure(
        self,
        state: EndRunState,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        exc: BaseException,
    ) -> None:
        logger.warning(
            (
                "[ContextManager] end failed"
                " sid=%s tenant=%s user=%s"
                " latency=%dms fail_fast=%s: %s"
            ),
            session_id,
            tenant_id,
            user_id,
            state.duration_ms,
            state.fail_fast,
            exc,
            exc_info=(
                type(exc),
                exc,
                exc.__traceback__,
            ),
        )
