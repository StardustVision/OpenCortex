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

    @staticmethod
    def _make_state(owner_id: str, *, owner_type: OwnerType = OwnerType.MEMORY) -> CognitiveState:
        return CognitiveState(
            state_id=f"{owner_type.value}:{owner_id}",
            owner_type=owner_type,
            owner_id=owner_id,
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-1",
        )

    async def test_save_get_round_trip(self):
        state = CognitiveState(
            state_id="memory:mem-1",
            owner_type=OwnerType.MEMORY,
            owner_id="mem-1",
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-1",
            lifecycle_state=LifecycleState.ACTIVE,
            exposure_state=ExposureState.GUARDED,
            consolidation_state=ConsolidationState.CANDIDATE,
            activation_score=0.8,
            stability_score=0.4,
            risk_score=0.2,
            novelty_score=0.9,
            evidence_residual_score=0.3,
            access_count=6,
            retrieval_success_count=5,
            retrieval_failure_count=1,
            last_mutation_reason="reinforce",
            last_mutation_source="planner",
            version=3,
        )

        await self.store.save_state(state)
        loaded = await self.store.get_by_owner(OwnerType.MEMORY, "mem-1")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.owner_type, OwnerType.MEMORY)
        self.assertEqual(loaded.owner_id, "mem-1")
        self.assertEqual(loaded.state_id, "memory:mem-1")
        self.assertEqual(loaded.tenant_id, "tenant-1")
        self.assertEqual(loaded.project_id, "project-1")
        self.assertEqual(loaded.exposure_state, ExposureState.GUARDED)
        self.assertEqual(loaded.consolidation_state, ConsolidationState.CANDIDATE)
        self.assertEqual(loaded.version, 3)
        self.assertEqual(loaded.activation_score, 0.8)
        self.assertEqual(loaded.retrieval_success_count, 5)

    async def test_update_state_rejects_stale_version(self):
        await self.store.save_state(self._make_state("mem-2"))

        updated = await self.store.update_state(
            owner_type=OwnerType.MEMORY,
            owner_id="mem-2",
            expected_version=1,
            fields={"activation_score": 0.75},
        )
        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.activation_score, 0.75)

        with self.assertRaises(StaleStateVersionError):
            await self.store.update_state(
                owner_type=OwnerType.MEMORY,
                owner_id="mem-2",
                expected_version=1,
                fields={"activation_score": 0.10},
            )

    async def test_persist_batch_commits_state_and_ledger(self):
        await self.store.save_state(self._make_state("mem-3"))
        batch = MutationBatch(batch_id="batch-1", owner_ids=["mem-3"])

        committed = await self.store.persist_batch(
            batch=batch,
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-3",
                    "expected_version": 1,
                    "fields": {
                        "activation_score": 0.91,
                        "last_mutation_reason": "batch-commit",
                    },
                }
            ],
        )
        self.assertTrue(committed)

        state = await self.store.get_by_owner(OwnerType.MEMORY, "mem-3")
        self.assertIsNotNone(state)
        self.assertEqual(state.version, 2)
        self.assertEqual(state.activation_score, 0.91)
        self.assertEqual(state.last_mutation_reason, "batch-commit")

        ledger_rows = await self.storage.get(
            self.store._batch_collection,
            ["batch-1"],
        )
        self.assertEqual(len(ledger_rows), 1)
        self.assertEqual(
            ledger_rows[0]["status"],
            MutationBatchStatus.COMMITTED.value,
        )
        self.assertEqual(ledger_rows[0]["owner_ids"], ["mem-3"])
        self.assertIn("committed_at", ledger_rows[0])

    async def test_persist_batch_marks_failed_on_stale_write(self):
        await self.store.save_state(self._make_state("mem-4"))
        batch = MutationBatch(batch_id="batch-2", owner_ids=["mem-4"])

        committed = await self.store.persist_batch(
            batch=batch,
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-4",
                    "expected_version": 0,
                    "fields": {"activation_score": 0.1},
                }
            ],
        )
        self.assertFalse(committed)

        ledger_rows = await self.storage.get(
            self.store._batch_collection,
            ["batch-2"],
        )
        self.assertEqual(len(ledger_rows), 1)
        self.assertEqual(
            ledger_rows[0]["status"],
            MutationBatchStatus.FAILED.value,
        )

        state = await self.store.get_by_owner(OwnerType.MEMORY, "mem-4")
        self.assertIsNotNone(state)
        self.assertEqual(state.version, 1)

    async def test_persist_batch_failure_does_not_leave_partial_updates(self):
        await self.store.save_state(self._make_state("mem-a"))
        await self.store.save_state(self._make_state("mem-b"))
        batch = MutationBatch(batch_id="batch-rollback", owner_ids=["mem-a", "mem-b"])

        committed = await self.store.persist_batch(
            batch=batch,
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-a",
                    "expected_version": 1,
                    "fields": {"activation_score": 0.8},
                },
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-b",
                    "expected_version": 0,
                    "fields": {"activation_score": 0.2},
                },
            ],
        )
        self.assertFalse(committed)

        state_a = await self.store.get_by_owner(OwnerType.MEMORY, "mem-a")
        state_b = await self.store.get_by_owner(OwnerType.MEMORY, "mem-b")
        self.assertIsNotNone(state_a)
        self.assertIsNotNone(state_b)
        self.assertEqual(state_a.version, 1)
        self.assertEqual(state_b.version, 1)
        self.assertEqual(state_a.activation_score, 0.0)
        self.assertEqual(state_b.activation_score, 0.0)

    async def test_update_state_rejects_identity_field_mutation(self):
        await self.store.save_state(self._make_state("mem-identity"))

        with self.assertRaises(ValueError):
            await self.store.update_state(
                owner_type=OwnerType.MEMORY,
                owner_id="mem-identity",
                expected_version=1,
                fields={"owner_id": "other-user"},
            )
        with self.assertRaises(ValueError):
            await self.store.update_state(
                owner_type=OwnerType.MEMORY,
                owner_id="mem-identity",
                expected_version=1,
                fields={"tenant_id": "other-tenant"},
            )

    def test_mutation_batch_to_dict_omits_unset_committed_at(self):
        batch = MutationBatch(batch_id="batch-no-commit", owner_ids=["mem-1"])
        record = batch.to_dict()
        self.assertNotIn("committed_at", record)

    async def test_get_states_for_owners_returns_mapping(self):
        await self.store.save_state(self._make_state("mem-x"))
        await self.store.save_state(self._make_state("mem-y", owner_type=OwnerType.TRACE))

        states = await self.store.get_states_for_owners(["mem-x", "mem-y", "missing"])
        self.assertIsInstance(states, dict)
        self.assertIn("mem-x", states)
        self.assertIn("mem-y", states)
        self.assertEqual(states["mem-x"].owner_type, OwnerType.MEMORY)
        self.assertEqual(states["mem-y"].owner_type, OwnerType.TRACE)

    async def test_init_accepts_batch_collection_parameter(self):
        store = CognitiveStateStore(self.storage, batch_collection="custom_batches")
        await store.init()
        self.assertTrue(await self.storage.collection_exists("custom_batches"))


if __name__ == "__main__":
    unittest.main()
