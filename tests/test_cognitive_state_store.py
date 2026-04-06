import unittest

from tests.test_e2e_phase1 import InMemoryStorage

from opencortex.cognition.state_store import (
    CognitiveStateStore,
    StaleStateVersionError,
)
from opencortex.cognition.state_types import (
    CognitiveState,
    ConsolidationState,
    ExposureState,
    LifecycleState,
    MutationBatch,
    MutationBatchStatus,
    OwnerType,
)


class TestCognitiveStateStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = InMemoryStorage()
        self.store = CognitiveStateStore(self.storage)
        await self.store.init()

    async def test_save_get_round_trip(self):
        state = CognitiveState(
            owner_type=OwnerType.USER,
            owner_id="user-1",
            lifecycle_state=LifecycleState.ACTIVE,
            exposure_state=ExposureState.PRIVATE,
            consolidation_state=ConsolidationState.UNCONSOLIDATED,
            version=3,
            payload={"topic": "systems"},
        )

        await self.store.save_state(state)
        loaded = await self.store.get_by_owner(OwnerType.USER, "user-1")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.owner_type, OwnerType.USER)
        self.assertEqual(loaded.owner_id, "user-1")
        self.assertEqual(loaded.version, 3)
        self.assertEqual(loaded.payload["topic"], "systems")

    async def test_update_state_rejects_stale_version(self):
        await self.store.save_state(
            CognitiveState(
                owner_type=OwnerType.USER,
                owner_id="user-2",
                payload={"topic": "initial"},
            )
        )

        updated = await self.store.update_state(
            owner_type=OwnerType.USER,
            owner_id="user-2",
            expected_version=1,
            fields={"payload": {"topic": "fresh"}},
        )
        self.assertEqual(updated.version, 2)

        with self.assertRaises(StaleStateVersionError):
            await self.store.update_state(
                owner_type=OwnerType.USER,
                owner_id="user-2",
                expected_version=1,
                fields={"payload": {"topic": "stale"}},
            )

    async def test_persist_batch_commits_state_and_ledger(self):
        await self.store.save_state(
            CognitiveState(owner_type=OwnerType.USER, owner_id="user-3")
        )
        batch = MutationBatch(batch_id="batch-1")

        committed = await self.store.persist_batch(
            batch=batch,
            state_updates=[
                {
                    "owner_type": OwnerType.USER,
                    "owner_id": "user-3",
                    "expected_version": 1,
                    "fields": {"payload": {"topic": "committed"}},
                }
            ],
        )
        self.assertTrue(committed)

        state = await self.store.get_by_owner(OwnerType.USER, "user-3")
        self.assertIsNotNone(state)
        self.assertEqual(state.version, 2)
        self.assertEqual(state.payload["topic"], "committed")

        ledger_rows = await self.storage.get(
            self.store._mutation_batch_collection,
            ["batch-1"],
        )
        self.assertEqual(len(ledger_rows), 1)
        self.assertEqual(
            ledger_rows[0]["status"],
            MutationBatchStatus.COMMITTED.value,
        )

    async def test_persist_batch_marks_failed_on_stale_write(self):
        await self.store.save_state(
            CognitiveState(owner_type=OwnerType.USER, owner_id="user-4")
        )
        batch = MutationBatch(batch_id="batch-2")

        committed = await self.store.persist_batch(
            batch=batch,
            state_updates=[
                {
                    "owner_type": OwnerType.USER,
                    "owner_id": "user-4",
                    "expected_version": 0,
                    "fields": {"payload": {"topic": "stale-write"}},
                }
            ],
        )
        self.assertFalse(committed)

        ledger_rows = await self.storage.get(
            self.store._mutation_batch_collection,
            ["batch-2"],
        )
        self.assertEqual(len(ledger_rows), 1)
        self.assertEqual(
            ledger_rows[0]["status"],
            MutationBatchStatus.FAILED.value,
        )

        state = await self.store.get_by_owner(OwnerType.USER, "user-4")
        self.assertIsNotNone(state)
        self.assertEqual(state.version, 1)


if __name__ == "__main__":
    unittest.main()
