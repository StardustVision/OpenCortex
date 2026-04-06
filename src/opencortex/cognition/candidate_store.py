# SPDX-License-Identifier: Apache-2.0
"""Storage-backed store for governance consolidation candidates."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Callable, Optional, Sequence

from opencortex.cognition.state_types import ConsolidationCandidate
from opencortex.storage.collection_schemas import init_consolidation_candidate_collection
from opencortex.storage.storage_interface import StorageInterface


_WS_RE = re.compile(r"\s+")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_text(value: str) -> str:
    if not isinstance(value, str):
        return ""
    return _WS_RE.sub(" ", value.strip().lower())


class CandidateStore:
    def __init__(
        self,
        storage: StorageInterface,
        *,
        collection: str = "consolidation_candidate",
        now_iso_fn: Callable[[], str] = _utc_now_iso,
    ) -> None:
        self._storage = storage
        self._collection = collection
        self._now_iso_fn = now_iso_fn

    async def init(self) -> None:
        await init_consolidation_candidate_collection(self._storage, self._collection)

    def build_fingerprint(self, candidate: ConsolidationCandidate) -> str:
        payload = {
            "tenant_id": candidate.tenant_id,
            "user_id": candidate.user_id,
            "project_id": candidate.project_id,
            "candidate_kind": candidate.candidate_kind,
            "source_owner_type": candidate.source_owner_type,
            "source_owner_id": candidate.source_owner_id,
            "statement": _normalize_text(candidate.statement),
            "abstract": _normalize_text(candidate.abstract),
            "overview": _normalize_text(candidate.overview),
            "supporting_memory_ids": sorted(str(x) for x in (candidate.supporting_memory_ids or [])),
            "supporting_trace_ids": sorted(str(x) for x in (candidate.supporting_trace_ids or [])),
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def is_duplicate_within_cooldown(
        self,
        *,
        dedupe_fingerprint: str,
        tenant_id: str,
        user_id: str,
        project_id: str,
        cooldown_seconds: int,
        now_iso: Optional[str] = None,
    ) -> bool:
        if not dedupe_fingerprint or cooldown_seconds <= 0:
            return False

        rows = await self._storage.filter(
            self._collection,
            {
                "op": "and",
                "conds": [
                    {"op": "must", "field": "dedupe_fingerprint", "conds": [dedupe_fingerprint]},
                    {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
                    {"op": "must", "field": "user_id", "conds": [user_id]},
                    {"op": "must", "field": "project_id", "conds": [project_id]},
                ],
            },
            limit=1,
            order_by="created_at",
            order_desc=True,
        )
        if not rows:
            return False

        created_at = _parse_iso(rows[0].get("created_at", ""))
        now_dt = _parse_iso(now_iso or self._now_iso_fn())
        if created_at is None or now_dt is None:
            return False
        age = (now_dt - created_at).total_seconds()
        return 0 <= age < float(cooldown_seconds)

    async def save_many(self, candidates: Sequence[ConsolidationCandidate]) -> Sequence[str]:
        if not candidates:
            return []
        now = self._now_iso_fn()
        records = []
        for candidate in candidates:
            if not candidate.dedupe_fingerprint:
                candidate.dedupe_fingerprint = self.build_fingerprint(candidate)
            record = candidate.to_dict()
            record["created_at"] = now
            record["updated_at"] = now
            records.append(record)
        return await self._storage.batch_upsert(self._collection, records)
