import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.cognition.mutation_engine import RecallMutationEngine
from opencortex.cognition.state_types import CognitiveState, ExposureState, OwnerType


class TestRecallMutationEngine(unittest.TestCase):
    @staticmethod
    def _state(owner_id: str, activation: float = 0.0) -> CognitiveState:
        return CognitiveState(
            state_id=f"memory:{owner_id}",
            owner_type=OwnerType.MEMORY,
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


if __name__ == "__main__":
    unittest.main()
