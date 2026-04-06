import unittest
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

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


class _LocalInMemoryStorage:
    def __init__(self):
        self._collections = {}
        self._records = {}
        self.upsert_calls = []
        self.fail_on_committed_batch_ids = set()
        self._state_upsert_count = 0
        self.fail_on_state_upsert_number = None

    async def create_collection(self, name, schema):
        if name in self._collections:
            return False
        self._collections[name] = schema
        self._records[name] = {}
        return True

    async def collection_exists(self, name):
        return name in self._collections

    async def upsert(self, collection, data):
        if (
            collection in self._records
            and collection.endswith("batch")
            and data.get("batch_id") in self.fail_on_committed_batch_ids
            and data.get("status") == "committed"
        ):
            raise RuntimeError("simulated committed upsert failure")
        if collection in self._records and collection.endswith("state"):
            self._state_upsert_count += 1
            if (
                self.fail_on_state_upsert_number is not None
                and self._state_upsert_count == self.fail_on_state_upsert_number
            ):
                raise RuntimeError("simulated state upsert failure")
        record_id = data["id"]
        row = dict(data)
        self._records[collection][record_id] = row
        self.upsert_calls.append((collection, dict(row)))
        return record_id

    async def get(self, collection, ids):
        records = self._records.get(collection, {})
        return [dict(records[rid]) for rid in ids if rid in records]

    async def filter(self, collection, filter, limit=10, **kwargs):
        records = list(self._records.get(collection, {}).values())
        matched = [dict(r) for r in records if self._eval_filter(r, filter)]
        return matched[:limit]

    def _eval_filter(self, record, filt):
        if not filt:
            return True
        op = filt.get("op", "")
        if op == "must":
            return record.get(filt.get("field")) in filt.get("conds", [])
        if op == "and":
            return all(self._eval_filter(record, c) for c in filt.get("conds", []))
        return True


class TestCognitiveStateStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = _LocalInMemoryStorage()
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
        self.assertEqual(loaded.version, 1)
        self.assertEqual(loaded.activation_score, 0.8)
        self.assertEqual(loaded.retrieval_success_count, 5)

    async def test_save_state_rejects_existing_owner_row(self):
        await self.store.save_state(self._make_state("mem-dup"))
        with self.assertRaises(ValueError):
            await self.store.save_state(self._make_state("mem-dup"))

    async def test_save_state_canonicalizes_state_id_by_owner(self):
        await self.store.save_state(self._make_state("owner-a"))
        attacker_state = self._make_state("owner-b")
        attacker_state.state_id = "memory:owner-a"

        await self.store.save_state(attacker_state)

        owner_a = await self.store.get_by_owner(OwnerType.MEMORY, "owner-a")
        owner_b = await self.store.get_by_owner(OwnerType.MEMORY, "owner-b")
        self.assertIsNotNone(owner_a)
        self.assertIsNotNone(owner_b)
        self.assertEqual(owner_a.state_id, "memory:owner-a")
        self.assertEqual(owner_b.state_id, "memory:owner-b")

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
        self.assertEqual(json.loads(ledger_rows[0]["owner_ids"]), ["mem-3"])
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

    async def test_persist_batch_flush_failure_rolls_back_prior_state_writes(self):
        await self.store.save_state(self._make_state("mem-r1"))
        await self.store.save_state(self._make_state("mem-r2"))
        self.storage.fail_on_state_upsert_number = self.storage._state_upsert_count + 2
        batch = MutationBatch(batch_id="batch-flush-fail", owner_ids=["mem-r1", "mem-r2"])

        committed = await self.store.persist_batch(
            batch=batch,
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-r1",
                    "expected_version": 1,
                    "fields": {"activation_score": 0.7},
                },
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-r2",
                    "expected_version": 1,
                    "fields": {"activation_score": 0.9},
                },
            ],
        )
        self.assertFalse(committed)

        state_1 = await self.store.get_by_owner(OwnerType.MEMORY, "mem-r1")
        state_2 = await self.store.get_by_owner(OwnerType.MEMORY, "mem-r2")
        self.assertIsNotNone(state_1)
        self.assertIsNotNone(state_2)
        self.assertEqual(state_1.version, 1)
        self.assertEqual(state_2.version, 1)
        self.assertEqual(state_1.activation_score, 0.0)
        self.assertEqual(state_2.activation_score, 0.0)

        ledger_rows = await self.storage.get(self.store._batch_collection, ["batch-flush-fail"])
        self.assertEqual(len(ledger_rows), 1)
        self.assertEqual(ledger_rows[0]["status"], MutationBatchStatus.FAILED.value)
        self.assertIn("state flush failed", ledger_rows[0]["error"])

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

    async def test_update_state_rejects_version_field_mutation(self):
        await self.store.save_state(self._make_state("mem-version"))
        with self.assertRaises(ValueError):
            await self.store.update_state(
                owner_type=OwnerType.MEMORY,
                owner_id="mem-version",
                expected_version=1,
                fields={"version": 999},
            )

    async def test_update_state_rejects_unknown_field(self):
        await self.store.save_state(self._make_state("mem-unknown"))
        with self.assertRaises(ValueError):
            await self.store.update_state(
                owner_type=OwnerType.MEMORY,
                owner_id="mem-unknown",
                expected_version=1,
                fields={"typo_activation_score": 1.0},
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

    async def test_persist_batch_non_stale_error_marks_ledger_failed(self):
        await self.store.save_state(self._make_state("mem-invalid"))
        batch = MutationBatch(batch_id="batch-invalid")
        committed = await self.store.persist_batch(
            batch=batch,
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-invalid",
                    "expected_version": 1,
                    "fields": {"lifecycle_state": "not-a-state"},
                }
            ],
        )
        self.assertFalse(committed)
        ledger_rows = await self.storage.get(self.store._batch_collection, ["batch-invalid"])
        self.assertEqual(len(ledger_rows), 1)
        self.assertEqual(ledger_rows[0]["status"], MutationBatchStatus.FAILED.value)
        self.assertTrue(ledger_rows[0].get("error"))

    async def test_persist_batch_failure_row_keeps_owner_ids(self):
        await self.store.save_state(self._make_state("mem-owner-link"))
        batch = MutationBatch(batch_id="batch-owner-ids", owner_ids=[])
        committed = await self.store.persist_batch(
            batch=batch,
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-owner-link",
                    "expected_version": 0,
                    "fields": {"activation_score": 0.3},
                }
            ],
        )
        self.assertFalse(committed)
        ledger_rows = await self.storage.get(self.store._batch_collection, ["batch-owner-ids"])
        self.assertEqual(len(ledger_rows), 1)
        self.assertEqual(ledger_rows[0]["status"], MutationBatchStatus.FAILED.value)
        self.assertEqual(json.loads(ledger_rows[0]["owner_ids"]), ["mem-owner-link"])
        first_pending = next(
            row for col, row in self.storage.upsert_calls
            if col == self.store._batch_collection and row["batch_id"] == "batch-owner-ids"
        )
        self.assertEqual(first_pending["status"], MutationBatchStatus.PENDING.value)
        self.assertEqual(json.loads(first_pending["owner_ids"]), ["mem-owner-link"])

    async def test_persist_batch_commit_upsert_failure_marks_failed(self):
        await self.store.save_state(self._make_state("mem-commit-fail"))
        self.storage.fail_on_committed_batch_ids.add("batch-commit-fail")
        batch = MutationBatch(batch_id="batch-commit-fail")
        committed = await self.store.persist_batch(
            batch=batch,
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-commit-fail",
                    "expected_version": 1,
                    "fields": {"activation_score": 0.42},
                }
            ],
        )
        self.assertFalse(committed)
        ledger_rows = await self.storage.get(self.store._batch_collection, ["batch-commit-fail"])
        self.assertEqual(len(ledger_rows), 1)
        self.assertEqual(ledger_rows[0]["status"], MutationBatchStatus.FAILED.value)
        self.assertIn("RuntimeError", ledger_rows[0]["error"])


if __name__ == "__main__":
    unittest.main()
