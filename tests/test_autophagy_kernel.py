import os
import sys
import unittest
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.cognition.consolidation_gate import ConsolidationGateResult
from opencortex.cognition.kernel import AutophagyKernel
from opencortex.cognition.state_types import (
    CognitiveState,
    ConsolidationCandidate,
    MetabolismResult,
    MutationBatch,
    OwnerType,
    RecallMutationResult,
)


class _FakeStateStore:
    def __init__(self) -> None:
        self._states: Dict[Tuple[OwnerType, str], CognitiveState] = {}
        self.get_states_for_owners_calls: List[Sequence[str]] = []
        self.get_by_owner_calls: List[Tuple[OwnerType, str]] = []
        self.save_state_calls: List[CognitiveState] = []
        self.persist_batch_calls: List[Tuple[MutationBatch, Sequence[Mapping[str, Any]]]] = []

    def seed(self, state: CognitiveState) -> None:
        self._states[(state.owner_type, state.owner_id)] = state

    async def get_by_owner(self, owner_type: OwnerType, owner_id: str) -> Optional[CognitiveState]:
        self.get_by_owner_calls.append((owner_type, owner_id))
        return self._states.get((owner_type, owner_id))

    async def save_state(self, state: CognitiveState) -> CognitiveState:
        self.save_state_calls.append(state)
        self._states[(state.owner_type, state.owner_id)] = state
        return state

    async def get_states_for_owners(self, owner_ids: Sequence[str]) -> Dict[str, CognitiveState]:
        self.get_states_for_owners_calls.append(list(owner_ids))
        states: Dict[str, CognitiveState] = {}
        for (owner_type, owner_id), state in self._states.items():
            if owner_id in owner_ids:
                if owner_id in states and states[owner_id].owner_type != owner_type:
                    raise ValueError("ambiguous owner_id collision across owner types")
                states[owner_id] = state
        return states

    async def persist_batch(
        self, batch: MutationBatch, state_updates: Sequence[Mapping[str, Any]]
    ) -> bool:
        self.persist_batch_calls.append((batch, list(state_updates)))
        for update in state_updates:
            owner_type = update.get("owner_type")
            if not isinstance(owner_type, OwnerType):
                owner_type = OwnerType(str(owner_type))
            owner_id = str(update.get("owner_id"))
            expected_version = int(update.get("expected_version"))
            fields = dict(update.get("fields") or {})
            state = self._states.get((owner_type, owner_id))
            if state is None:
                return False
            if state.version != expected_version:
                return False
            for key, value in fields.items():
                if hasattr(state, key):
                    setattr(state, key, value)
                elif key == "metadata":
                    state.metadata = dict(value or {})
            state.version += 1
        return True


class _FakeCandidateStore:
    def __init__(self) -> None:
        self.save_many_calls: List[Sequence[ConsolidationCandidate]] = []

    async def save_many(self, candidates: Sequence[ConsolidationCandidate]) -> Sequence[str]:
        self.save_many_calls.append(list(candidates))
        return [c.candidate_id for c in candidates]


class _FakeRecallMutationEngine:
    def __init__(self, result: RecallMutationResult) -> None:
        self._result = result
        self.apply_calls: List[Tuple[str, Any, Any]] = []

    def apply(
        self,
        query: str,
        states: Mapping[str, CognitiveState] | Iterable[CognitiveState],
        recall_outcome: Mapping[str, Any] | None,
    ) -> RecallMutationResult:
        self.apply_calls.append((query, states, recall_outcome))
        return self._result


class _FakeConsolidationGate:
    def __init__(self, result_factory: Callable) -> None:
        self._result_factory = result_factory
        self.evaluate_calls: List[List[CognitiveState]] = []

    async def evaluate(self, states: Iterable[CognitiveState]) -> ConsolidationGateResult:
        snapshot = list(states)
        self.evaluate_calls.append(snapshot)
        return self._result_factory(snapshot)


class _FakeMetabolismController:
    def __init__(self, result: MetabolismResult) -> None:
        self._result = result
        self.tick_calls: List[Tuple[Any, Any]] = []

    def tick(
        self,
        states: Mapping[str, CognitiveState] | Iterable[CognitiveState],
        dominance_window: Mapping[Any, Any] | Iterable[Any] | None = None,
    ) -> MetabolismResult:
        self.tick_calls.append((states, dominance_window))
        return self._result


class TestAutophagyKernel(unittest.IsolatedAsyncioTestCase):
    def _make_state(self, owner_id: str, *, version: int = 1) -> CognitiveState:
        return CognitiveState(
            state_id=f"memory:{owner_id}",
            owner_type=OwnerType.MEMORY,
            owner_id=owner_id,
            tenant_id="t1",
            user_id="u1",
            project_id="p1",
            version=version,
        )

    async def test_initialize_owner_creates_missing_and_skips_existing(self) -> None:
        store = _FakeStateStore()
        candidate_store = _FakeCandidateStore()

        kernel = AutophagyKernel(
            state_store=store,
            candidate_store=candidate_store,
            mutation_engine=_FakeRecallMutationEngine(RecallMutationResult()),
            consolidation_gate=_FakeConsolidationGate(lambda _: ConsolidationGateResult()),
            metabolism_controller=_FakeMetabolismController(MetabolismResult()),
        )

        created = await kernel.initialize_owner(
            owner_type=OwnerType.MEMORY,
            owner_id="mem-1",
            tenant_id="t1",
            user_id="u1",
            project_id="p1",
        )
        self.assertEqual(created.owner_id, "mem-1")
        self.assertEqual(len(store.save_state_calls), 1)

        store.save_state_calls.clear()
        existing = await kernel.initialize_owner(
            owner_type=OwnerType.MEMORY,
            owner_id="mem-1",
            tenant_id="t1",
            user_id="u1",
            project_id="p1",
        )
        self.assertEqual(existing.owner_id, "mem-1")
        self.assertEqual(len(store.save_state_calls), 0)

    async def test_apply_recall_outcome_orchestrates_batches_and_candidates(self) -> None:
        store = _FakeStateStore()
        store.seed(self._make_state("mem-1", version=1))
        candidate_store = _FakeCandidateStore()

        mutation_result = RecallMutationResult(
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-1",
                    "expected_version": 1,
                    "fields": {"access_count": 1, "last_mutation_reason": "touch"},
                }
            ]
        )
        mutation_engine = _FakeRecallMutationEngine(mutation_result)

        def gate_result_factory(states: List[CognitiveState]) -> ConsolidationGateResult:
            # Gate derives expected_version from the states passed in; if the kernel does not
            # re-load (or otherwise refresh) versions after persisting the recall batch,
            # this will be stale and the second persist_batch should fail.
            state = states[0]
            candidate = ConsolidationCandidate(
                candidate_id="cand-1",
                source_owner_type=state.owner_type.value,
                source_owner_id=state.owner_id,
                tenant_id=state.tenant_id,
                user_id=state.user_id,
                project_id=state.project_id,
                candidate_kind="state_consolidation",
                statement="S",
                abstract="A",
                overview="O",
            )
            return ConsolidationGateResult(
                candidates=[candidate],
                state_updates=[
                    {
                        "owner_type": state.owner_type,
                        "owner_id": state.owner_id,
                        "expected_version": state.version,
                        "fields": {"last_mutation_reason": "submit_consolidation_candidate"},
                    }
                ],
            )

        gate = _FakeConsolidationGate(gate_result_factory)

        kernel = AutophagyKernel(
            state_store=store,
            candidate_store=candidate_store,
            mutation_engine=mutation_engine,
            consolidation_gate=gate,
            metabolism_controller=_FakeMetabolismController(MetabolismResult()),
        )

        result = await kernel.apply_recall_outcome(
            owner_ids=["mem-1"],
            query="q",
            recall_outcome={"selected_results": ["mem-1"]},
        )

        self.assertEqual(len(store.get_states_for_owners_calls), 2)
        self.assertEqual(len(mutation_engine.apply_calls), 1)
        self.assertEqual(len(store.persist_batch_calls), 2)

        self.assertEqual(len(gate.evaluate_calls), 1)
        # Ensure gate saw the refreshed state version (post-mutation batch).
        self.assertEqual(gate.evaluate_calls[0][0].version, 2)

        self.assertEqual(len(candidate_store.save_many_calls), 1)
        self.assertEqual(candidate_store.save_many_calls[0][0].candidate_id, "cand-1")

        self.assertTrue(result["mutation_committed"])
        self.assertTrue(result["consolidation_committed"])
        self.assertEqual(result["candidate_ids"], ["cand-1"])

    async def test_metabolize_states_persists_state_updates(self) -> None:
        store = _FakeStateStore()
        store.seed(self._make_state("mem-1", version=1))
        candidate_store = _FakeCandidateStore()

        metabolism_result = MetabolismResult(
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-1",
                    "expected_version": 1,
                    "fields": {"last_mutation_reason": "metabolism_cool"},
                }
            ]
        )
        metabolism = _FakeMetabolismController(metabolism_result)

        kernel = AutophagyKernel(
            state_store=store,
            candidate_store=candidate_store,
            mutation_engine=_FakeRecallMutationEngine(RecallMutationResult()),
            consolidation_gate=_FakeConsolidationGate(lambda _: ConsolidationGateResult()),
            metabolism_controller=metabolism,
        )

        result = await kernel.metabolize_states(owner_ids=["mem-1"], dominance_window=None)
        self.assertEqual(len(store.get_states_for_owners_calls), 1)
        self.assertEqual(len(metabolism.tick_calls), 1)
        self.assertEqual(len(store.persist_batch_calls), 1)
        self.assertTrue(result["committed"])
