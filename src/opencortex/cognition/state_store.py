# SPDX-License-Identifier: Apache-2.0
"""Durable store for cognitive states and mutation batches."""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Sequence

from opencortex.cognition.state_types import (
    CognitiveState,
    ConsolidationState,
    ExposureState,
    LifecycleState,
    MutationBatch,
    MutationBatchStatus,
    OwnerType,
)
from opencortex.storage.collection_schemas import (
    init_cognitive_mutation_batch_collection,
    init_cognitive_state_collection,
)
from opencortex.storage.storage_interface import StorageInterface


DEFAULT_COGNITIVE_STATE_COLLECTION = "cognitive_state"
DEFAULT_COGNITIVE_MUTATION_BATCH_COLLECTION = "cognitive_mutation_batch"


class StaleStateVersionError(ValueError):
    """Raised when expected_version does not match the current state version."""


class CognitiveStateStore:
    def __init__(
        self,
        storage: StorageInterface,
        state_collection: str = DEFAULT_COGNITIVE_STATE_COLLECTION,
        batch_collection: str = DEFAULT_COGNITIVE_MUTATION_BATCH_COLLECTION,
    ) -> None:
        self._storage = storage
        self._state_collection = state_collection
        self._batch_collection = batch_collection
        self._mutation_batch_collection = batch_collection
        self._owner_locks: Dict[str, asyncio.Lock] = {}
        self._owner_locks_guard = asyncio.Lock()

    async def init(self) -> None:
        await init_cognitive_state_collection(self._storage, self._state_collection)
        await init_cognitive_mutation_batch_collection(
            self._storage, self._batch_collection
        )

    async def save_state(self, state: CognitiveState) -> CognitiveState:
        async with await self._get_owner_lock(state.owner_type, state.owner_id):
            existing = await self.get_by_owner(state.owner_type, state.owner_id)
            if existing is not None:
                raise ValueError(
                    f"state already exists for {state.owner_type.value}:{state.owner_id}; "
                    "use update_state or persist_batch"
                )
            state.state_id = self._owner_state_id(state.owner_type, state.owner_id)
            state.version = 1
            await self._storage.upsert(self._state_collection, state.to_dict())
            return state

    async def get_by_owner(
        self, owner_type: OwnerType, owner_id: str
    ) -> CognitiveState | None:
        rows = await self._storage.filter(
            self._state_collection,
            {
                "op": "and",
                "conds": [
                    {"op": "must", "field": "owner_type", "conds": [owner_type.value]},
                    {"op": "must", "field": "owner_id", "conds": [owner_id]},
                ],
            },
            limit=100,
        )
        if not rows:
            return None
        best = max(rows, key=lambda row: int(row.get("version", 0)))
        return CognitiveState.from_dict(best)

    async def get_states_for_owners(self, owner_ids: Sequence[str]) -> Dict[str, CognitiveState]:
        ids = [oid for oid in owner_ids if oid]
        if not ids:
            return {}
        rows = await self._storage.filter(
            self._state_collection,
            {"op": "must", "field": "owner_id", "conds": ids},
            limit=max(1, len(ids) * 10),
        )
        states: Dict[str, CognitiveState] = {}
        for row in rows:
            state = CognitiveState.from_dict(row)
            prior = states.get(state.owner_id)
            if prior is None or state.version >= prior.version:
                states[state.owner_id] = state
        return states

    async def update_state(
        self,
        owner_type: OwnerType,
        owner_id: str,
        expected_version: int,
        fields: Mapping[str, Any],
    ) -> CognitiveState:
        async with await self._get_owner_lock(owner_type, owner_id):
            existing = await self.get_by_owner(owner_type, owner_id)
            if existing is None:
                raise KeyError(f"state not found for {owner_type.value}:{owner_id}")
            if existing.version != expected_version:
                raise StaleStateVersionError(
                    f"stale version for {owner_type.value}:{owner_id}: "
                    f"expected={expected_version}, actual={existing.version}"
                )

            self._validate_mutable_fields(fields)
            self._apply_state_fields(existing, fields)
            existing.version += 1
            await self._storage.upsert(self._state_collection, existing.to_dict())
            return existing

    async def persist_batch(
        self,
        batch: MutationBatch,
        state_updates: Sequence[Mapping[str, Any]],
    ) -> bool:
        if not batch.owner_ids:
            batch.owner_ids = [
                str(update["owner_id"])
                for update in state_updates
                if "owner_id" in update
            ]

        batch.status = MutationBatchStatus.PENDING
        batch.error = ""
        batch.updated_at = _utc_now_iso()
        await self._storage.upsert(self._batch_collection, batch.to_dict())

        try:
            normalized_updates = [
                {
                    "owner_type": _coerce_owner_type(update["owner_type"]),
                    "owner_id": str(update["owner_id"]),
                    "expected_version": int(update["expected_version"]),
                    "fields": dict(update.get("fields", {})),
                }
                for update in state_updates
            ]

            lock_keys = sorted(
                {
                    self._owner_state_id(update["owner_type"], update["owner_id"])
                    for update in normalized_updates
                }
            )

            async with _LockGroup([await self._get_lock_by_key(key) for key in lock_keys]):
                staged: Dict[str, CognitiveState] = {}
                snapshots: Dict[str, CognitiveState] = {}
                for update in normalized_updates:
                    owner_type = update["owner_type"]
                    owner_id = update["owner_id"]
                    state_key = self._owner_state_id(owner_type, owner_id)

                    current = staged.get(state_key)
                    if current is None:
                        current = await self.get_by_owner(owner_type, owner_id)
                        if current is not None:
                            snapshots[state_key] = CognitiveState.from_dict(current.to_dict())
                    if current is None:
                        raise KeyError(f"state not found for {state_key}")
                    if current.version != update["expected_version"]:
                        raise StaleStateVersionError(
                            f"stale version for {state_key}: "
                            f"expected={update['expected_version']}, actual={current.version}"
                        )

                    fields = update["fields"]
                    self._validate_mutable_fields(fields)
                    next_state = CognitiveState.from_dict(current.to_dict())
                    self._apply_state_fields(next_state, fields)
                    next_state.version += 1
                    next_state.last_mutation_at = _utc_now_iso()
                    staged[state_key] = next_state

                applied_state_keys: list[str] = []
                try:
                    for state_key in lock_keys:
                        state = staged.get(state_key)
                        if state is not None:
                            await self._storage.upsert(self._state_collection, state.to_dict())
                            applied_state_keys.append(state_key)
                except Exception as flush_exc:
                    rollback_exc = await self._rollback_states_from_snapshots(
                        applied_state_keys, snapshots
                    )
                    if rollback_exc is not None:
                        raise RuntimeError(
                            "state flush failed; rollback failed; "
                            f"flush_error={type(flush_exc).__name__}: {flush_exc}; "
                            f"rollback_error={type(rollback_exc).__name__}: {rollback_exc}"
                        ) from flush_exc
                    raise RuntimeError(
                        "state flush failed; rollback succeeded; "
                        f"flush_error={type(flush_exc).__name__}: {flush_exc}"
                    ) from flush_exc
        except Exception as exc:
            batch.status = MutationBatchStatus.FAILED
            batch.error = f"{type(exc).__name__}: {exc}"
            batch.updated_at = _utc_now_iso()
            await self._storage.upsert(self._batch_collection, batch.to_dict())
            return False

        try:
            now = _utc_now_iso()
            batch.status = MutationBatchStatus.COMMITTED
            batch.updated_at = now
            batch.committed_at = now
            await self._storage.upsert(self._batch_collection, batch.to_dict())
            return True
        except Exception as exc:
            batch.status = MutationBatchStatus.FAILED
            batch.error = f"{type(exc).__name__}: {exc}"
            batch.updated_at = _utc_now_iso()
            batch.committed_at = None
            try:
                await self._storage.upsert(self._batch_collection, batch.to_dict())
            except Exception:
                pass
            return False

    async def _get_owner_lock(self, owner_type: OwnerType, owner_id: str) -> asyncio.Lock:
        key = self._owner_state_id(owner_type, owner_id)
        async with self._owner_locks_guard:
            lock = self._owner_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._owner_locks[key] = lock
            return lock

    @staticmethod
    def _apply_state_fields(state: CognitiveState, fields: Mapping[str, Any]) -> None:
        for key, value in fields.items():
            if key == "lifecycle_state":
                state.lifecycle_state = LifecycleState(value)
            elif key == "exposure_state":
                state.exposure_state = ExposureState(value)
            elif key == "consolidation_state":
                state.consolidation_state = ConsolidationState(value)
            elif hasattr(state, key):
                setattr(state, key, value)

    @staticmethod
    def _validate_mutable_fields(fields: Mapping[str, Any]) -> None:
        immutable_keys = {
            "id",
            "state_id",
            "owner_id",
            "owner_type",
            "tenant_id",
            "user_id",
            "project_id",
            "version",
        }
        for key in fields:
            if key in immutable_keys:
                raise ValueError(f"identity field mutation is not allowed: {key}")

        allowed_mutable_keys = {
            "lifecycle_state",
            "exposure_state",
            "consolidation_state",
            "activation_score",
            "stability_score",
            "risk_score",
            "novelty_score",
            "evidence_residual_score",
            "access_count",
            "retrieval_success_count",
            "retrieval_failure_count",
            "last_accessed_at",
            "last_reinforced_at",
            "last_penalized_at",
            "last_mutation_at",
            "last_mutation_reason",
            "last_mutation_source",
            "metadata",
        }
        unknown_keys = [key for key in fields if key not in allowed_mutable_keys and key not in immutable_keys]
        if unknown_keys:
            raise ValueError(f"unknown mutable field(s): {', '.join(sorted(unknown_keys))}")

    @staticmethod
    def _owner_state_id(owner_type: OwnerType, owner_id: str) -> str:
        return f"{owner_type.value}:{owner_id}"

    async def _get_lock_by_key(self, key: str) -> asyncio.Lock:
        async with self._owner_locks_guard:
            lock = self._owner_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._owner_locks[key] = lock
            return lock

    async def _rollback_states_from_snapshots(
        self,
        applied_state_keys: Sequence[str],
        snapshots: Mapping[str, CognitiveState],
    ) -> Exception | None:
        try:
            for state_key in applied_state_keys:
                snapshot = snapshots.get(state_key)
                if snapshot is not None:
                    await self._storage.upsert(self._state_collection, snapshot.to_dict())
            return None
        except Exception as exc:
            return exc


def _coerce_owner_type(value: OwnerType | str) -> OwnerType:
    if isinstance(value, OwnerType):
        return value
    return OwnerType(value)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _LockGroup:
    """Acquire/release a precomputed list of locks in deterministic order."""

    def __init__(self, locks: Sequence[asyncio.Lock]) -> None:
        self._locks = list(locks)

    async def __aenter__(self) -> "_LockGroup":
        for lock in self._locks:
            await lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        for lock in reversed(self._locks):
            lock.release()
