# SPDX-License-Identifier: Apache-2.0
"""Thin orchestration facade for autophagy cognition flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .candidate_store import CandidateStore
from .consolidation_gate import ConsolidationGate, ConsolidationGateResult
from .metabolism import CognitiveMetabolismController
from .mutation_engine import RecallMutationEngine
from .state_store import CognitiveStateStore
from .state_types import (
    CognitiveState,
    MetabolismResult,
    MutationBatch,
    OwnerType,
    RecallMutationResult,
)


@dataclass
class RecallOutcomeApplicationResult:
    states: dict[str, CognitiveState]
    recall_result: RecallMutationResult
    recall_batch: MutationBatch
    recall_batch_committed: bool
    consolidation_result: ConsolidationGateResult
    persisted_candidate_ids: Sequence[str]
    consolidation_batch: MutationBatch | None = None
    consolidation_batch_committed: bool | None = None


class AutophagyKernel:
    """Store-aware facade that wires existing cognition contracts together."""

    def __init__(
        self,
        *,
        state_store: CognitiveStateStore,
        mutation_engine: RecallMutationEngine,
        consolidation_gate: ConsolidationGate,
        candidate_store: CandidateStore,
        metabolism_controller: CognitiveMetabolismController,
    ) -> None:
        self._state_store = state_store
        self._mutation_engine = mutation_engine
        self._consolidation_gate = consolidation_gate
        self._candidate_store = candidate_store
        self._metabolism_controller = metabolism_controller

    async def initialize_owner(
        self,
        *,
        owner_type: OwnerType,
        owner_id: str,
        tenant_id: str,
        user_id: str,
        project_id: str,
    ) -> CognitiveState:
        existing = await self._state_store.get_by_owner(owner_type, owner_id)
        if existing is not None:
            return existing

        state = CognitiveState(
            state_id=f"{owner_type.value}:{owner_id}",
            owner_type=owner_type,
            owner_id=owner_id,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
        )
        return await self._state_store.save_state(state)

    async def apply_recall_outcome(
        self,
        owner_ids: Sequence[str],
        query: str,
        recall_outcome: Mapping[str, Any] | None,
    ) -> RecallOutcomeApplicationResult:
        states = await self._state_store.get_states_for_owners(owner_ids)
        recall_result = self._mutation_engine.apply(
            query=query,
            states=states,
            recall_outcome=recall_outcome,
        )

        recall_batch = MutationBatch(
            batch_id=f"recall-{uuid4()}",
            owner_ids=list(owner_ids),
            metadata={"kind": "recall_outcome"},
        )
        recall_batch_committed = await self._state_store.persist_batch(
            recall_batch,
            recall_result.state_updates,
        )
        if not recall_batch_committed:
            raise RuntimeError(
                f"failed to persist recall mutation batch: {recall_batch.batch_id}"
            )

        refreshed_states = await self._state_store.get_states_for_owners(owner_ids)
        consolidation_result = await self._consolidation_gate.evaluate(
            list(refreshed_states.values())
        )
        persisted_candidate_ids = await self._candidate_store.save_many(
            consolidation_result.candidates
        )

        consolidation_batch: MutationBatch | None = None
        consolidation_batch_committed: bool | None = None
        if consolidation_result.state_updates:
            consolidation_batch = MutationBatch(
                batch_id=f"consolidation-{uuid4()}",
                owner_ids=list(owner_ids),
                metadata={"kind": "consolidation_gate"},
            )
            consolidation_batch_committed = await self._state_store.persist_batch(
                consolidation_batch,
                consolidation_result.state_updates,
            )
            if not consolidation_batch_committed:
                if persisted_candidate_ids:
                    await self._candidate_store.delete_many(persisted_candidate_ids)
                raise RuntimeError(
                    "failed to persist consolidation mutation batch after saving candidates: "
                    f"{consolidation_batch.batch_id}"
                )

            refreshed_states = await self._state_store.get_states_for_owners(owner_ids)

        return RecallOutcomeApplicationResult(
            states=refreshed_states,
            recall_result=recall_result,
            recall_batch=recall_batch,
            recall_batch_committed=recall_batch_committed,
            consolidation_result=consolidation_result,
            persisted_candidate_ids=persisted_candidate_ids,
            consolidation_batch=consolidation_batch,
            consolidation_batch_committed=consolidation_batch_committed,
        )

    async def metabolize_states(
        self,
        owner_ids: Sequence[str],
        dominance_window: Mapping[Any, Any] | Sequence[Any] | None = None,
    ) -> MetabolismResult:
        states = await self._state_store.get_states_for_owners(owner_ids)
        result = self._metabolism_controller.tick(
            states,
            dominance_window=dominance_window,
        )
        batch = MutationBatch(
            batch_id=f"metabolism-{uuid4()}",
            owner_ids=list(owner_ids),
            metadata={"kind": "metabolism"},
        )
        committed = await self._state_store.persist_batch(batch, result.state_updates)
        if not committed:
            raise RuntimeError(f"failed to persist metabolism batch: {batch.batch_id}")
        return result
