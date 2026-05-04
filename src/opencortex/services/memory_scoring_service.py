# SPDX-License-Identifier: Apache-2.0
"""Memory scoring and lifecycle mutation service for OpenCortex."""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opencortex.services.memory_filters import FilterExpr

if TYPE_CHECKING:
    from opencortex.services.memory_service import MemoryService

logger = logging.getLogger(__name__)


class MemoryScoringService:
    """Own feedback, decay, protection, and scoring-profile operations."""

    def __init__(self, memory_service: "MemoryService") -> None:
        self._service = memory_service

    @property
    def _orch(self) -> Any:
        return self._service._orch

    # =========================================================================
    # Scoring + lifecycle (U4 of plan 011)
    # =========================================================================

    async def feedback(self, uri: str, reward: float) -> None:
        """Submit a reward signal for a context.

        Positive rewards reinforce retrieval; negative rewards penalize
        it. The reinforced score formula:
        ``reinforced_score = similarity * (1 + alpha * reward_factor) * decay_factor``

        Args:
            uri: URI of the context.
            reward: Scalar reward value (positive = good, negative = bad).
        """
        orch = self._orch
        orch._ensure_init()

        # Find the record ID for this URI in context collection
        records = await orch._storage.filter(
            orch._get_collection(),
            FilterExpr.eq("uri", uri).to_dict(),
            limit=1,
        )
        if not records:
            logger.warning("[MemoryService] feedback: URI not found: %s", uri)
            return

        record_id = records[0].get("id", "")
        if not record_id:
            return

        # Send reward via storage adapter
        if hasattr(orch._storage, "update_reward"):
            await orch._storage.update_reward(orch._get_collection(), record_id, reward)
            logger.info(
                "[MemoryService] Feedback sent: uri=%s, reward=%s",
                uri,
                reward,
            )
        else:
            logger.debug("[MemoryService] Storage backend does not support rewards")

        # Also update activity count
        ctx_data = records[0]
        active_count = ctx_data.get("active_count", 0)
        await orch._storage.update(
            orch._get_collection(),
            record_id,
            {"active_count": active_count + 1},
        )

    async def feedback_batch(self, rewards: List[Dict[str, Any]]) -> None:
        """Submit batch reward signals.

        Args:
            rewards: List of ``{"uri": str, "reward": float}`` dicts.
        """
        orch = self._orch
        orch._ensure_init()

        for item in rewards:
            await self._service.feedback(item["uri"], item["reward"])

    async def decay(self) -> Optional[Dict[str, Any]]:
        """Trigger time-decay across all records.

        Normal nodes decay at rate 0.95, protected nodes at rate 0.99.
        Records below threshold (0.01) may be archived.

        Returns:
            Decay summary dict with keys ``records_processed``,
            ``records_decayed``, ``records_below_threshold``,
            ``records_archived``, and optionally ``staging_cleaned``.
            ``None`` if the storage backend does not support decay.
        """
        orch = self._orch
        orch._ensure_init()

        if hasattr(orch._storage, "apply_decay"):
            result = await orch._storage.apply_decay()
            logger.info("[MemoryService] Decay applied: %s", result)
            decay_result = {
                "records_processed": result.records_processed,
                "records_decayed": result.records_decayed,
                "records_below_threshold": result.records_below_threshold,
                "records_archived": result.records_archived,
            }

            # Piggyback staging cleanup on decay
            try:
                cleaned = await self._service.cleanup_expired_staging()
                if cleaned:
                    decay_result["staging_cleaned"] = cleaned
            except Exception as exc:
                logger.warning("[MemoryService] Staging cleanup failed: %s", exc)

            return decay_result
        logger.debug("[MemoryService] Storage backend does not support decay")
        return None

    async def cleanup_expired_staging(self) -> int:
        """Delete records whose TTL has expired.

        Covers staging records, immediate-layer conversation records,
        and any other record with a non-empty ``ttl_expires_at`` field.

        Returns:
            Number of records deleted.
        """
        orch = self._orch
        orch._ensure_init()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Scan all records with non-empty ttl_expires_at
        expired = await orch._storage.filter(
            orch._get_collection(),
            FilterExpr.neq("ttl_expires_at", "").to_dict(),
            limit=1000,
        )
        cleaned = 0
        to_delete = []
        for record in expired:
            ttl = record.get("ttl_expires_at", "")
            if ttl and ttl < now:
                rid = record.get("id", "")
                if rid:
                    to_delete.append(rid)
                uri = record.get("uri", "")
                if uri:
                    with suppress(Exception):
                        await orch._fs.delete_temp(uri)
                cleaned += 1
        if to_delete:
            await orch._storage.delete(orch._get_collection(), to_delete)
        if cleaned:
            logger.info("[MemoryService] Cleaned %d expired records", cleaned)
        return cleaned

    async def protect(self, uri: str, protected: bool = True) -> None:
        """Mark a context as protected to slow its decay rate.

        Protected memories decay at rate 0.99 instead of 0.95,
        preserving important knowledge for longer.

        Args:
            uri: URI of the context.
            protected: ``True`` to protect, ``False`` to unprotect.
        """
        orch = self._orch
        orch._ensure_init()

        records = await orch._storage.filter(
            orch._get_collection(),
            FilterExpr.eq("uri", uri).to_dict(),
            limit=1,
        )
        if not records:
            logger.warning("[MemoryService] protect: URI not found: %s", uri)
            return

        record_id = records[0].get("id", "")
        if hasattr(orch._storage, "set_protected"):
            await orch._storage.set_protected(
                orch._get_collection(), record_id, protected
            )
            logger.info("[MemoryService] Set protected=%s for: %s", protected, uri)

    async def get_profile(self, uri: str) -> Optional[Dict[str, Any]]:
        """Get the feedback scoring profile for a context.

        Args:
            uri: URI of the context.

        Returns:
            Profile dict with keys ``reward_score``, ``retrieval_count``,
            ``positive_feedback_count``, ``negative_feedback_count``,
            ``effective_score``, ``is_protected``. ``None`` if the URI
            is not found or the backend does not support profiles.
        """
        orch = self._orch
        orch._ensure_init()

        records = await orch._storage.filter(
            orch._get_collection(),
            FilterExpr.eq("uri", uri).to_dict(),
            limit=1,
        )
        if not records:
            return None

        record_id = records[0].get("id", "")
        if hasattr(orch._storage, "get_profile"):
            profile = await orch._storage.get_profile(orch._get_collection(), record_id)
            if profile:
                return {
                    "id": profile.id,
                    "reward_score": profile.reward_score,
                    "retrieval_count": profile.retrieval_count,
                    "positive_feedback_count": profile.positive_feedback_count,
                    "negative_feedback_count": profile.negative_feedback_count,
                    "effective_score": profile.effective_score,
                    "is_protected": profile.is_protected,
                }
        return None
