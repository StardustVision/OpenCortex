import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.cognition.mutation_engine import RecallMutationEngine
from opencortex.cognition.state_types import CognitiveState, ExposureState, OwnerType


class TestRecallMutationEngine(unittest.TestCase):
    @staticmethod
    def _state(
        owner_id: str,
        activation: float = 0.0,
        owner_type: OwnerType = OwnerType.MEMORY,
    ) -> CognitiveState:
        return CognitiveState(
            state_id=f"{owner_type.value}:{owner_id}",
            owner_type=owner_type,
            owner_id=owner_id,
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-1",
            activation_score=activation,
            access_count=3,
            version=4,
        )

    @staticmethod
    def _find_update(result, owner_id: str):
        for update in result.state_updates:
            if update["owner_id"] == owner_id:
                return update
        raise AssertionError(f"state update for owner_id={owner_id} not found")

    @staticmethod
    def _find_update_by_owner(result, owner_type: OwnerType, owner_id: str):
        for update in result.state_updates:
            if update["owner_type"] == owner_type and update["owner_id"] == owner_id:
                return update
        raise AssertionError(
            f"state update for owner_type={owner_type.value} owner_id={owner_id} not found"
        )

    def test_final_answer_usage_reinforces_state(self):
        used = self._state("mem-used", activation=0.6)
        engine = RecallMutationEngine()

        recall_outcome = {
            "selected_results": [{"owner_type": "memory", "owner_id": "mem-used"}],
            "final_answer_used_memories": ["mem-used"],
        }
        result = engine.apply(
            query="How did we fix this before?",
            states=[used],
            recall_outcome=recall_outcome,
        )

        update = self._find_update(result, "mem-used")
        fields = update["fields"]
        self.assertGreater(fields["activation_score"], used.activation_score)
        self.assertEqual(fields["access_count"], used.access_count + 1)
        self.assertIsNotNone(fields["last_accessed_at"])
        self.assertIsNotNone(fields["last_reinforced_at"])
        self.assertEqual(fields["last_mutation_reason"], "reinforce")
        self.assertEqual(fields["last_mutation_source"], "recall_mutation_engine")
        self.assertEqual(len(result.explanations), 1)
        self.assertIsInstance(result.explanations[0], dict)
        self.assertEqual(result.explanations[0]["kind"], "reinforce")
        self.assertEqual(result.explanations[0]["owner_type"], "memory")
        self.assertEqual(result.explanations[0]["owner_id"], "mem-used")
        self.assertIn("gain", result.explanations[0])

    def test_recalled_but_unused_penalizes_hot_candidate(self):
        hot = self._state("mem-hot", activation=0.92)
        engine = RecallMutationEngine()

        recall_outcome = {
            "selected_results": [{"owner_type": "memory", "owner_id": "mem-hot"}],
            "final_answer_used_memories": [],
        }
        result = engine.apply(
            query="Need candidates",
            states=[hot],
            recall_outcome=recall_outcome,
        )

        update = self._find_update(result, "mem-hot")
        fields = update["fields"]
        self.assertLess(fields["activation_score"], hot.activation_score)
        self.assertEqual(fields["access_count"], hot.access_count + 1)
        self.assertIsNotNone(fields["last_accessed_at"])
        self.assertIsNotNone(fields["last_penalized_at"])
        self.assertEqual(fields["last_mutation_reason"], "penalize")
        self.assertEqual(fields["last_mutation_source"], "recall_mutation_engine")
        self.assertEqual(len(result.explanations), 1)
        self.assertEqual(result.explanations[0]["kind"], "penalize")
        self.assertEqual(result.explanations[0]["owner_id"], "mem-hot")
        self.assertIn("decay", result.explanations[0])

    def test_conflict_signal_marks_state_contested(self):
        target = self._state("mem-conflict", activation=0.4)
        engine = RecallMutationEngine()

        recall_outcome = {
            "conflict_signals": [
                {
                    "owner_type": "memory",
                    "owner_id": "mem-conflict",
                    "reason": "answer conflict",
                }
            ]
        }
        result = engine.apply(
            query="Which version is correct?",
            states=[target],
            recall_outcome=recall_outcome,
        )

        update = self._find_update(result, "mem-conflict")
        fields = update["fields"]
        self.assertEqual(fields["exposure_state"], ExposureState.CONTESTED.value)
        self.assertEqual(fields["last_mutation_reason"], "contest")
        self.assertEqual(fields["last_mutation_source"], "recall_mutation_engine")
        self.assertEqual(len(result.contestation_events), 1)
        self.assertEqual(result.contestation_events[0]["owner_id"], "mem-conflict")
        self.assertIn("reason", result.contestation_events[0])
        self.assertEqual(len(result.explanations), 1)
        self.assertEqual(result.explanations[0]["kind"], "contest")
        self.assertEqual(result.explanations[0]["owner_id"], "mem-conflict")
        self.assertEqual(result.explanations[0]["reason"], "answer conflict")

    def test_same_owner_id_for_memory_and_trace_produces_distinct_updates(self):
        memory_state = self._state("shared", activation=0.5, owner_type=OwnerType.MEMORY)
        trace_state = self._state("shared", activation=0.2, owner_type=OwnerType.TRACE)
        engine = RecallMutationEngine()

        recall_outcome = {
            "selected_results": [
                {"owner_type": "memory", "owner_id": "shared"},
                {"owner_type": "trace", "owner_id": "shared"},
            ],
            "final_answer_used_memories": ["shared"],
        }
        result = engine.apply(
            query="recall shared owner ids",
            states=[memory_state, trace_state],
            recall_outcome=recall_outcome,
        )

        self.assertEqual(len(result.state_updates), 2)
        memory_update = self._find_update_by_owner(result, OwnerType.MEMORY, "shared")
        trace_update = self._find_update_by_owner(result, OwnerType.TRACE, "shared")
        self.assertGreater(
            memory_update["fields"]["activation_score"], memory_state.activation_score
        )
        self.assertEqual(
            trace_update["fields"]["access_count"], trace_state.access_count + 1
        )

    def test_apply_accepts_owner_keyed_state_mapping(self):
        used = self._state("mapped-used", activation=0.65)
        warm = self._state("mapped-neutral", activation=0.4)
        engine = RecallMutationEngine()

        recall_outcome = {
            "selected_results": [
                {"owner_type": "memory", "owner_id": "mapped-used"},
                {"owner_type": "memory", "owner_id": "mapped-neutral"},
            ],
            "final_answer_used_memories": ["mapped-used"],
        }
        result = engine.apply(
            query="dict-shaped states",
            states={
                "mapped-used": used,
                "mapped-neutral": warm,
            },
            recall_outcome=recall_outcome,
        )

        self.assertEqual(len(result.state_updates), 2)
        used_update = self._find_update(result, "mapped-used")
        neutral_update = self._find_update(result, "mapped-neutral")
        self.assertGreater(
            used_update["fields"]["activation_score"], used.activation_score
        )
        self.assertEqual(
            neutral_update["fields"]["last_mutation_reason"],
            "touch",
        )

    def test_touched_neutral_state_sets_mutation_metadata(self):
        warm = self._state("mem-neutral", activation=0.5)
        engine = RecallMutationEngine()

        recall_outcome = {
            "selected_results": [{"owner_type": "memory", "owner_id": "mem-neutral"}],
            "final_answer_used_memories": [],
        }
        result = engine.apply(
            query="neutral touch",
            states=[warm],
            recall_outcome=recall_outcome,
        )

        update = self._find_update(result, "mem-neutral")
        fields = update["fields"]
        self.assertEqual(fields["access_count"], warm.access_count + 1)
        self.assertIsNotNone(fields["last_accessed_at"])
        self.assertEqual(fields["last_mutation_reason"], "touch")
        self.assertEqual(fields["last_mutation_source"], "recall_mutation_engine")
        self.assertIsNotNone(fields["last_mutation_at"])


if __name__ == "__main__":
    unittest.main()
