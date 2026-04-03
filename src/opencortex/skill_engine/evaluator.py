"""
SkillEvaluator — correlates skill events with trace outcomes.

Updates selection/application counters and reward scores.
Includes startup sweeper for crash recovery.
Per-tenant asyncio.Lock prevents concurrent evaluation.
"""

import asyncio
import logging
from collections import defaultdict
from typing import Dict

logger = logging.getLogger(__name__)


class SkillEvaluator:
    def __init__(self, event_store, skill_store, trace_store,
                 skill_storage=None, llm=None):
        self._event_store = event_store
        self._skill_store = skill_store
        self._trace_store = trace_store
        self._skill_storage = skill_storage
        self._llm = llm
        self._tenant_locks: Dict[str, asyncio.Lock] = {}

    def _get_lock(self, tenant_id: str) -> asyncio.Lock:
        if tenant_id not in self._tenant_locks:
            self._tenant_locks[tenant_id] = asyncio.Lock()
        return self._tenant_locks[tenant_id]

    async def evaluate_session(
        self, tenant_id: str, user_id: str, session_id: str,
    ) -> None:
        """Evaluate skill usage for a completed session. Per-tenant locked."""
        async with self._get_lock(tenant_id):
            try:
                await self._evaluate_session_inner(tenant_id, user_id, session_id)
            except Exception as exc:
                logger.warning("[SkillEvaluator] Failed for session %s: %s", session_id, exc)

    async def _evaluate_session_inner(
        self, tenant_id: str, user_id: str, session_id: str,
    ) -> None:
        # 1. Fetch unevaluated events
        events = await self._event_store.list_by_session(session_id, tenant_id, user_id)
        unevaluated = [e for e in events if not e.evaluated]
        if not unevaluated:
            return

        # 2. Fetch traces for outcome correlation
        traces = []
        if self._trace_store:
            try:
                traces = await self._trace_store.list_by_session(session_id, tenant_id, user_id)
            except Exception:
                pass

        session_outcome = "success" if any(
            t.get("outcome") == "success" for t in traces
        ) else "failure" if traces else ""

        # 3. Group events by skill_id
        skill_events: Dict[str, list] = defaultdict(list)
        for e in unevaluated:
            skill_events[e.skill_id].append(e)

        # 4. Update metrics per skill
        for skill_id, events_for_skill in skill_events.items():
            was_selected = any(e.event_type == "selected" for e in events_for_skill)
            was_cited = any(e.event_type == "cited" for e in events_for_skill)

            if was_selected:
                await self._skill_store.record_selection(skill_id)

            if was_cited:
                completed = session_outcome == "success"
                await self._skill_store.record_application(skill_id, completed)

                # Reward scoring
                if self._skill_storage:
                    reward = 0.1 if completed else -0.05
                    await self._skill_storage.update_reward(skill_id, reward)

        # 5. Mark events as evaluated (idempotency)
        await self._event_store.mark_evaluated(
            [e.event_id for e in unevaluated]
        )

        logger.info(
            "[SkillEvaluator] Session %s: %d events, %d skills, outcome=%s",
            session_id, len(unevaluated), len(skill_events), session_outcome,
        )

    async def sweep_unevaluated(self, tenant_id: str = "") -> int:
        """Startup sweeper — process backlog from crash/restart.

        If tenant_id is empty, sweeps ALL unevaluated events across tenants.
        """
        try:
            backlog = await self._event_store.list_unevaluated(tenant_id, limit=200)
            if not backlog:
                return 0

            groups: Dict[tuple, list] = defaultdict(list)
            for e in backlog:
                groups[(e.session_id, e.user_id)].append(e)

            for (sid, uid), _ in groups.items():
                await self.evaluate_session(tenant_id, uid, sid)

            logger.info(
                "[SkillEvaluator] Swept %d backlog events across %d sessions",
                len(backlog), len(groups),
            )
            return len(backlog)
        except Exception as exc:
            logger.warning("[SkillEvaluator] Startup sweep failed: %s", exc)
            return 0
