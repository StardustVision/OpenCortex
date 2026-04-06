# SPDX-License-Identifier: Apache-2.0

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.cognition.state_types import CognitiveState, LifecycleState, OwnerType


class _FakeClock:
    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        self._now = now.astimezone(timezone.utc)

    def now_iso(self) -> str:
        return self._now.isoformat()


class TestCognitiveMetabolismController(unittest.TestCase):
    @staticmethod
    def _state(
        owner_id: str,
        *,
        activation: float = 0.0,
        stability: float = 0.0,
        risk: float = 0.0,
        lifecycle: LifecycleState = LifecycleState.ACTIVE,
        owner_type: OwnerType = OwnerType.MEMORY,
        version: int = 1,
    ) -> CognitiveState:
        return CognitiveState(
            state_id=f"{owner_type.value}:{owner_id}",
            owner_type=owner_type,
            owner_id=owner_id,
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-1",
            lifecycle_state=lifecycle,
            activation_score=activation,
            stability_score=stability,
            risk_score=risk,
            version=version,
        )

    @staticmethod
    def _find_update(result, owner_type: OwnerType, owner_id: str):
        for update in result.state_updates:
            if update["owner_type"] == owner_type and update["owner_id"] == owner_id:
                return update
        raise AssertionError(
            f"state update for owner_type={owner_type.value} owner_id={owner_id} not found"
        )

    def test_dominant_hot_state_is_cooled(self) -> None:
        from opencortex.cognition.metabolism import CognitiveMetabolismController

        clock = _FakeClock(datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc))
        controller = CognitiveMetabolismController(
            now_iso_fn=clock.now_iso,
            hot_activation_threshold=0.8,
            dominance_count_threshold=3,
            cooling_decay=0.1,
        )

        hot = self._state("mem-hot", activation=0.95, stability=0.7, risk=0.2, version=2)
        dominance_window = {("memory", "mem-hot"): 3}
        result = controller.tick([hot], dominance_window=dominance_window)

        update = self._find_update(result, OwnerType.MEMORY, "mem-hot")
        self.assertEqual(update["expected_version"], 2)
        self.assertIn("activation_score", update["fields"])
        self.assertLess(update["fields"]["activation_score"], hot.activation_score)
        self.assertEqual(update["fields"]["last_mutation_reason"], "metabolism_cool")
        self.assertEqual(update["fields"]["last_mutation_source"], "cognitive_metabolism_controller")
        self.assertEqual(update["fields"]["last_mutation_at"], clock.now_iso())

    def test_cold_low_value_active_state_is_compressed_and_emits_review_event(self) -> None:
        from opencortex.cognition.metabolism import CognitiveMetabolismController

        clock = _FakeClock(datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc))
        controller = CognitiveMetabolismController(
            now_iso_fn=clock.now_iso,
            compress_activation_threshold=0.1,
            compress_value_threshold=0.0,
        )

        cold = self._state(
            "mem-cold",
            activation=0.05,
            stability=0.1,
            risk=0.6,
            lifecycle=LifecycleState.ACTIVE,
            version=5,
        )
        result = controller.tick([cold])

        update = self._find_update(result, OwnerType.MEMORY, "mem-cold")
        self.assertEqual(update["expected_version"], 5)
        self.assertEqual(update["fields"]["lifecycle_state"], LifecycleState.COMPRESSED.value)
        self.assertEqual(update["fields"]["last_mutation_reason"], "metabolism_compress")
        self.assertEqual(len(result.review_events), 1)
        event = result.review_events[0]
        self.assertEqual(event["kind"], "compress")
        self.assertEqual(event["owner_type"], "memory")
        self.assertEqual(event["owner_id"], "mem-cold")
        self.assertEqual(event["lifecycle_before"], LifecycleState.ACTIVE.value)
        self.assertEqual(event["lifecycle_after"], LifecycleState.COMPRESSED.value)

    def test_compressed_state_can_be_archived_at_deeper_threshold(self) -> None:
        from opencortex.cognition.metabolism import CognitiveMetabolismController

        clock = _FakeClock(datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc))
        controller = CognitiveMetabolismController(
            now_iso_fn=clock.now_iso,
            archive_activation_threshold=0.02,
            archive_value_threshold=0.0,
        )

        compressed = self._state(
            "mem-arch",
            activation=0.01,
            stability=0.05,
            risk=0.4,
            lifecycle=LifecycleState.COMPRESSED,
            version=9,
        )
        result = controller.tick([compressed])

        update = self._find_update(result, OwnerType.MEMORY, "mem-arch")
        self.assertEqual(update["expected_version"], 9)
        self.assertEqual(update["fields"]["lifecycle_state"], LifecycleState.ARCHIVED.value)
        self.assertEqual(update["fields"]["last_mutation_reason"], "metabolism_archive")
        self.assertEqual(len(result.review_events), 1)
        self.assertEqual(result.review_events[0]["kind"], "archive")

    def test_controller_is_conservative_does_not_compress_high_value_cold_state(self) -> None:
        from opencortex.cognition.metabolism import CognitiveMetabolismController

        controller = CognitiveMetabolismController(
            compress_activation_threshold=0.1,
            compress_value_threshold=0.0,
        )
        cold_but_valuable = self._state(
            "mem-valuable",
            activation=0.05,
            stability=0.9,
            risk=0.1,
            lifecycle=LifecycleState.ACTIVE,
        )
        result = controller.tick([cold_but_valuable])
        self.assertEqual(result.state_updates, [])
        self.assertEqual(result.review_events, [])

    def test_tick_accepts_mapping_input(self) -> None:
        from opencortex.cognition.metabolism import CognitiveMetabolismController

        controller = CognitiveMetabolismController(
            compress_activation_threshold=0.1,
            compress_value_threshold=0.0,
        )
        cold = self._state(
            "mapped-cold",
            activation=0.01,
            stability=0.1,
            risk=0.9,
            lifecycle=LifecycleState.ACTIVE,
            version=3,
        )
        result = controller.tick({"mapped-cold": cold})
        update = self._find_update(result, OwnerType.MEMORY, "mapped-cold")
        self.assertEqual(update["expected_version"], 3)


if __name__ == "__main__":
    unittest.main()

