import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.cognition import AutophagyKernel
from opencortex.cognition.consolidation_gate import ConsolidationGateResult
from opencortex.cognition.state_types import (
    CognitiveState,
    ConsolidationCandidate,
    ConsolidationState,
    MetabolismResult,
    MutationBatch,
    OwnerType,
    RecallMutationResult,
)


class _StateStoreSpy:
    def __init__(self) -> None:
        self.states_by_owner = {}
        self.saved_states = []
        self.persist_batch_calls = []
        self.persist_batch_results = []
        self.get_states_for_owners_calls = []

    async def get_by_owner(self, owner_type: OwnerType, owner_id: str):
        return self.states_by_owner.get((owner_type, owner_id))

    async def save_state(self, state: CognitiveState):
        self.states_by_owner[(state.owner_type, state.owner_id)] = state
        self.saved_states.append(state)
        return state

    async def get_states_for_owners(self, owner_ids):
        self.get_states_for_owners_calls.append(list(owner_ids))
        return {
            owner_id: state
            for (owner_type, owner_id), state in self.states_by_owner.items()
            if owner_id in owner_ids
        }

    async def persist_batch(self, batch: MutationBatch, state_updates):
        self.persist_batch_calls.append((batch, list(state_updates)))
        committed = (
            self.persist_batch_results.pop(0) if self.persist_batch_results else True
        )
        if not committed:
            return False
        for update in state_updates:
            key = (update["owner_type"], update["owner_id"])
            current = self.states_by_owner.get(key)
            if current is None:
                continue
            record = current.to_dict()
            record.update(update.get("fields", {}))
            record["version"] = int(update["expected_version"]) + 1
            self.states_by_owner[key] = CognitiveState.from_dict(record)
        return True


class _MutationEngineSpy:
    def __init__(self, result: RecallMutationResult) -> None:
        self.result = result
        self.calls = []

    def apply(self, query, states, recall_outcome):
        self.calls.append(
            {
                "query": query,
                "states": states,
                "recall_outcome": recall_outcome,
            }
        )
        return self.result


class _ConsolidationGateSpy:
    def __init__(self, result: ConsolidationGateResult) -> None:
        self.result = result
        self.calls = []

    async def evaluate(self, states):
        self.calls.append(list(states))
        return self.result


class _FreshVersionConsolidationGate:
    def __init__(self) -> None:
        self.calls = []

    async def evaluate(self, states):
        captured = list(states)
        self.calls.append(captured)
        state = captured[0]
        return ConsolidationGateResult(
            candidates=[],
            state_updates=[
                {
                    "owner_type": state.owner_type,
                    "owner_id": state.owner_id,
                    "expected_version": state.version,
                    "fields": {
                        "consolidation_state": ConsolidationState.SUBMITTED.value,
                    },
                }
            ],
        )


class _CandidateStoreSpy:
    def __init__(self, saved_ids=None) -> None:
        self.saved_ids = list(saved_ids or [])
        self.save_calls = []
        self.delete_calls = []

    async def save_many(self, candidates):
        self.save_calls.append(list(candidates))
        return list(self.saved_ids)

    async def delete_many(self, candidate_ids):
        self.delete_calls.append(list(candidate_ids))
        return len(candidate_ids)


class _MetabolismControllerSpy:
    def __init__(self, result: MetabolismResult) -> None:
        self.result = result
        self.calls = []

    def tick(self, states, dominance_window=None):
        self.calls.append(
            {
                "states": states,
                "dominance_window": dominance_window,
            }
        )
        return self.result


class TestAutophagyKernel(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _state(owner_id: str, *, version: int = 1) -> CognitiveState:
        return CognitiveState(
            state_id=f"memory:{owner_id}",
            owner_type=OwnerType.MEMORY,
            owner_id=owner_id,
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-1",
            version=version,
        )

    @staticmethod
    def _candidate(owner_id: str) -> ConsolidationCandidate:
        return ConsolidationCandidate(
            candidate_id=f"cand-{owner_id}",
            source_owner_type=OwnerType.MEMORY.value,
            source_owner_id=owner_id,
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-1",
            candidate_kind="state_consolidation",
            statement=f"statement-{owner_id}",
            abstract="abstract",
            overview="overview",
        )

    async def test_initialize_owner_creates_missing_cognitive_state(self) -> None:
        state_store = _StateStoreSpy()
        kernel = AutophagyKernel(
            state_store=state_store,
            mutation_engine=_MutationEngineSpy(RecallMutationResult()),
            consolidation_gate=_ConsolidationGateSpy(ConsolidationGateResult()),
            candidate_store=_CandidateStoreSpy(),
            metabolism_controller=_MetabolismControllerSpy(MetabolismResult()),
        )

        state = await kernel.initialize_owner(
            owner_type=OwnerType.MEMORY,
            owner_id="mem-new",
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-1",
        )

        self.assertEqual(len(state_store.saved_states), 1)
        self.assertEqual(state.owner_type, OwnerType.MEMORY)
        self.assertEqual(state.owner_id, "mem-new")
        self.assertEqual(state.state_id, "memory:mem-new")

    async def test_initialize_owner_skips_existing_state(self) -> None:
        existing = self._state("mem-existing", version=3)
        state_store = _StateStoreSpy()
        state_store.states_by_owner[(OwnerType.MEMORY, "mem-existing")] = existing
        kernel = AutophagyKernel(
            state_store=state_store,
            mutation_engine=_MutationEngineSpy(RecallMutationResult()),
            consolidation_gate=_ConsolidationGateSpy(ConsolidationGateResult()),
            candidate_store=_CandidateStoreSpy(),
            metabolism_controller=_MetabolismControllerSpy(MetabolismResult()),
        )

        state = await kernel.initialize_owner(
            owner_type=OwnerType.MEMORY,
            owner_id="mem-existing",
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-1",
        )

        self.assertIs(state, existing)
        self.assertEqual(state_store.saved_states, [])

    async def test_apply_recall_outcome_persists_mutations_candidates_and_gate_updates(self) -> None:
        state = self._state("mem-1", version=4)
        state.consolidation_state = ConsolidationState.CANDIDATE
        state_store = _StateStoreSpy()
        state_store.states_by_owner[(OwnerType.MEMORY, "mem-1")] = state

        mutation_result = RecallMutationResult(
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-1",
                    "expected_version": 4,
                    "fields": {"activation_score": 0.9},
                }
            ]
        )
        candidate = self._candidate("mem-1")
        gate_result = ConsolidationGateResult(
            candidates=[candidate],
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-1",
                    "expected_version": 4,
                    "fields": {"consolidation_state": ConsolidationState.SUBMITTED.value},
                }
            ],
        )
        mutation_engine = _MutationEngineSpy(mutation_result)
        consolidation_gate = _ConsolidationGateSpy(gate_result)
        candidate_store = _CandidateStoreSpy(saved_ids=["cand-1"])
        kernel = AutophagyKernel(
            state_store=state_store,
            mutation_engine=mutation_engine,
            consolidation_gate=consolidation_gate,
            candidate_store=candidate_store,
            metabolism_controller=_MetabolismControllerSpy(MetabolismResult()),
        )

        result = await kernel.apply_recall_outcome(
            owner_ids=["mem-1"],
            query="what do we know",
            recall_outcome={"selected_results": ["mem-1"]},
        )

        self.assertIs(result.recall_result, mutation_result)
        self.assertIs(result.consolidation_result, gate_result)
        self.assertEqual(result.persisted_candidate_ids, ["cand-1"])
        self.assertEqual(len(mutation_engine.calls), 1)
        self.assertEqual(mutation_engine.calls[0]["query"], "what do we know")
        self.assertIn("mem-1", mutation_engine.calls[0]["states"])
        self.assertEqual(len(consolidation_gate.calls), 1)
        self.assertEqual(len(consolidation_gate.calls[0]), 1)
        self.assertEqual(consolidation_gate.calls[0][0].owner_id, "mem-1")
        self.assertEqual(consolidation_gate.calls[0][0].version, 5)
        self.assertEqual(consolidation_gate.calls[0][0].activation_score, 0.9)
        self.assertEqual(candidate_store.save_calls, [[candidate]])
        self.assertEqual(len(state_store.persist_batch_calls), 2)
        first_batch, first_updates = state_store.persist_batch_calls[0]
        second_batch, second_updates = state_store.persist_batch_calls[1]
        self.assertIsInstance(first_batch, MutationBatch)
        self.assertIsInstance(second_batch, MutationBatch)
        self.assertNotEqual(first_batch.batch_id, second_batch.batch_id)
        self.assertEqual(first_updates, mutation_result.state_updates)
        self.assertEqual(second_updates, gate_result.state_updates)

    async def test_apply_recall_outcome_reloads_fresh_state_versions_before_consolidation(self) -> None:
        state = self._state("mem-fresh", version=4)
        state.consolidation_state = ConsolidationState.CANDIDATE
        state_store = _StateStoreSpy()
        state_store.states_by_owner[(OwnerType.MEMORY, "mem-fresh")] = state
        mutation_result = RecallMutationResult(
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-fresh",
                    "expected_version": 4,
                    "fields": {"activation_score": 0.9},
                }
            ]
        )
        consolidation_gate = _FreshVersionConsolidationGate()
        kernel = AutophagyKernel(
            state_store=state_store,
            mutation_engine=_MutationEngineSpy(mutation_result),
            consolidation_gate=consolidation_gate,
            candidate_store=_CandidateStoreSpy(),
            metabolism_controller=_MetabolismControllerSpy(MetabolismResult()),
        )

        await kernel.apply_recall_outcome(
            owner_ids=["mem-fresh"],
            query="freshen state versions",
            recall_outcome={"selected_results": ["mem-fresh"]},
        )

        self.assertEqual(len(consolidation_gate.calls), 1)
        self.assertEqual(consolidation_gate.calls[0][0].version, 5)
        self.assertEqual(len(state_store.persist_batch_calls), 2)
        _, consolidation_updates = state_store.persist_batch_calls[1]
        self.assertEqual(consolidation_updates[0]["expected_version"], 5)

    async def test_apply_recall_outcome_stops_on_recall_batch_failure(self) -> None:
        state = self._state("mem-stop", version=2)
        state_store = _StateStoreSpy()
        state_store.states_by_owner[(OwnerType.MEMORY, "mem-stop")] = state
        state_store.persist_batch_results = [False]
        mutation_result = RecallMutationResult(
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-stop",
                    "expected_version": 2,
                    "fields": {"activation_score": 0.8},
                }
            ]
        )
        consolidation_gate = _ConsolidationGateSpy(ConsolidationGateResult())
        candidate_store = _CandidateStoreSpy(saved_ids=["cand-stop"])
        kernel = AutophagyKernel(
            state_store=state_store,
            mutation_engine=_MutationEngineSpy(mutation_result),
            consolidation_gate=consolidation_gate,
            candidate_store=candidate_store,
            metabolism_controller=_MetabolismControllerSpy(MetabolismResult()),
        )

        with self.assertRaises(RuntimeError):
            await kernel.apply_recall_outcome(
                owner_ids=["mem-stop"],
                query="stop on recall failure",
                recall_outcome={"selected_results": ["mem-stop"]},
            )

        self.assertEqual(len(state_store.persist_batch_calls), 1)
        self.assertEqual(state_store.get_states_for_owners_calls, [["mem-stop"]])
        self.assertEqual(consolidation_gate.calls, [])
        self.assertEqual(candidate_store.save_calls, [])
        self.assertEqual(candidate_store.delete_calls, [])

    async def test_apply_recall_outcome_rolls_back_candidates_when_consolidation_persist_fails(self) -> None:
        state = self._state("mem-rollback", version=4)
        state.consolidation_state = ConsolidationState.CANDIDATE
        state_store = _StateStoreSpy()
        state_store.states_by_owner[(OwnerType.MEMORY, "mem-rollback")] = state
        state_store.persist_batch_results = [True, False]
        mutation_result = RecallMutationResult(
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-rollback",
                    "expected_version": 4,
                    "fields": {"activation_score": 0.9},
                }
            ]
        )
        candidate = self._candidate("mem-rollback")
        consolidation_gate = _ConsolidationGateSpy(
            ConsolidationGateResult(
                candidates=[candidate],
                state_updates=[
                    {
                        "owner_type": OwnerType.MEMORY,
                        "owner_id": "mem-rollback",
                        "expected_version": 5,
                        "fields": {
                            "consolidation_state": ConsolidationState.SUBMITTED.value,
                        },
                    }
                ],
            )
        )
        candidate_store = _CandidateStoreSpy(saved_ids=["cand-rollback"])
        kernel = AutophagyKernel(
            state_store=state_store,
            mutation_engine=_MutationEngineSpy(mutation_result),
            consolidation_gate=consolidation_gate,
            candidate_store=candidate_store,
            metabolism_controller=_MetabolismControllerSpy(MetabolismResult()),
        )

        with self.assertRaises(RuntimeError):
            await kernel.apply_recall_outcome(
                owner_ids=["mem-rollback"],
                query="rollback candidates",
                recall_outcome={"selected_results": ["mem-rollback"]},
            )

        self.assertEqual(candidate_store.save_calls, [[candidate]])
        self.assertEqual(candidate_store.delete_calls, [["cand-rollback"]])

    async def test_apply_recall_outcome_returns_final_post_consolidation_state_snapshot(self) -> None:
        state = self._state("mem-final", version=4)
        state.consolidation_state = ConsolidationState.CANDIDATE
        state_store = _StateStoreSpy()
        state_store.states_by_owner[(OwnerType.MEMORY, "mem-final")] = state
        mutation_result = RecallMutationResult(
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-final",
                    "expected_version": 4,
                    "fields": {"activation_score": 0.9},
                }
            ]
        )
        consolidation_gate = _FreshVersionConsolidationGate()
        kernel = AutophagyKernel(
            state_store=state_store,
            mutation_engine=_MutationEngineSpy(mutation_result),
            consolidation_gate=consolidation_gate,
            candidate_store=_CandidateStoreSpy(),
            metabolism_controller=_MetabolismControllerSpy(MetabolismResult()),
        )

        result = await kernel.apply_recall_outcome(
            owner_ids=["mem-final"],
            query="return final state snapshot",
            recall_outcome={"selected_results": ["mem-final"]},
        )

        self.assertEqual(result.states["mem-final"].version, 6)
        self.assertEqual(
            result.states["mem-final"].consolidation_state,
            ConsolidationState.SUBMITTED,
        )

    async def test_metabolize_states_raises_when_persist_batch_fails(self) -> None:
        state = self._state("mem-fail", version=6)
        state_store = _StateStoreSpy()
        state_store.states_by_owner[(OwnerType.MEMORY, "mem-fail")] = state
        state_store.persist_batch_results = [False]
        metabolism_result = MetabolismResult(
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-fail",
                    "expected_version": 6,
                    "fields": {"activation_score": 0.75},
                }
            ],
            review_events=[{"kind": "cool"}],
        )
        kernel = AutophagyKernel(
            state_store=state_store,
            mutation_engine=_MutationEngineSpy(RecallMutationResult()),
            consolidation_gate=_ConsolidationGateSpy(ConsolidationGateResult()),
            candidate_store=_CandidateStoreSpy(),
            metabolism_controller=_MetabolismControllerSpy(metabolism_result),
        )

        with self.assertRaises(RuntimeError):
            await kernel.metabolize_states(
                owner_ids=["mem-fail"],
                dominance_window={"mem-fail": {"wins": 3}},
            )

    async def test_metabolize_states_persists_state_updates_and_returns_controller_result(self) -> None:
        state = self._state("mem-hot", version=6)
        state_store = _StateStoreSpy()
        state_store.states_by_owner[(OwnerType.MEMORY, "mem-hot")] = state
        metabolism_result = MetabolismResult(
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-hot",
                    "expected_version": 6,
                    "fields": {"activation_score": 0.75},
                }
            ],
            review_events=[{"kind": "cool"}],
        )
        controller = _MetabolismControllerSpy(metabolism_result)
        kernel = AutophagyKernel(
            state_store=state_store,
            mutation_engine=_MutationEngineSpy(RecallMutationResult()),
            consolidation_gate=_ConsolidationGateSpy(ConsolidationGateResult()),
            candidate_store=_CandidateStoreSpy(),
            metabolism_controller=controller,
        )

        result = await kernel.metabolize_states(
            owner_ids=["mem-hot"],
            dominance_window={"mem-hot": {"wins": 3}},
        )

        self.assertIs(result, metabolism_result)
        self.assertEqual(len(controller.calls), 1)
        self.assertEqual(controller.calls[0]["states"], {"mem-hot": state})
        self.assertEqual(controller.calls[0]["dominance_window"], {"mem-hot": {"wins": 3}})
        self.assertEqual(len(state_store.persist_batch_calls), 1)
        batch, updates = state_store.persist_batch_calls[0]
        self.assertIsInstance(batch, MutationBatch)
        self.assertEqual(updates, metabolism_result.state_updates)


if __name__ == "__main__":
    unittest.main()
