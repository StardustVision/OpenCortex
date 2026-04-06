# SPDX-License-Identifier: Apache-2.0

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.cognition.state_types import (
    CognitiveState,
    ConsolidationState,
    GovernanceFeedback,
    GovernanceFeedbackKind,
    OwnerType,
)
from opencortex.cognition.candidate_store import CandidateStore
from opencortex.cognition.consolidation_gate import ConsolidationGate


class _LocalInMemoryStorage:
    def __init__(self) -> None:
        self._collections = {}
        self._records = {}

    async def create_collection(self, name, schema):
        if name in self._collections:
            return False
        self._collections[name] = schema
        self._records[name] = {}
        return True

    async def collection_exists(self, name):
        return name in self._collections

    async def upsert(self, collection, data):
        record_id = data["id"]
        self._records.setdefault(collection, {})
        self._records[collection][record_id] = dict(data)
        return record_id

    async def batch_upsert(self, collection, data):
        ids = []
        for row in data:
            ids.append(await self.upsert(collection, row))
        return ids

    async def filter(
        self,
        collection,
        filter,
        limit=10,
        offset=0,
        order_by=None,
        order_desc=False,
        **kwargs,
    ):
        records = list(self._records.get(collection, {}).values())
        matched = [dict(r) for r in records if self._eval_filter(r, filter)]
        if order_by:
            matched.sort(key=lambda r: r.get(order_by, ""), reverse=order_desc)
        return matched[offset : offset + limit]

    def _eval_filter(self, record, filt):
        if not filt:
            return True
        op = filt.get("op", "")
        if op == "must":
            return record.get(filt.get("field")) in filt.get("conds", [])
        if op == "and":
            return all(self._eval_filter(record, c) for c in filt.get("conds", []))
        return True


class _FakeClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._now = start.astimezone(timezone.utc)

    def now_iso(self) -> str:
        return self._now.isoformat()

    def advance(self, seconds: int) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class TestConsolidationGate(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.storage = _LocalInMemoryStorage()
        self.clock = _FakeClock(datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc))
        self.store = CandidateStore(
            self.storage,
            collection="consolidation_candidate",
            now_iso_fn=self.clock.now_iso,
        )
        await self.store.init()
        self.gate = ConsolidationGate(
            candidate_store=self.store,
            cooldown_seconds=60,
            now_iso_fn=self.clock.now_iso,
        )

    @staticmethod
    def _make_state(owner_id: str, *, version: int = 1) -> CognitiveState:
        return CognitiveState(
            state_id=f"memory:{owner_id}",
            owner_type=OwnerType.MEMORY,
            owner_id=owner_id,
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-1",
            consolidation_state=ConsolidationState.CANDIDATE,
            stability_score=0.8,
            risk_score=0.2,
            evidence_residual_score=0.6,
            version=version,
            metadata={
                "statement": "Keep batteries cool for longevity.",
                "abstract": "Battery heat accelerates degradation.",
                "overview": "Avoid heat and high charge; store at moderate charge.",
                "supporting_memory_ids": ["mem-a", "mem-b"],
                "supporting_trace_ids": ["trace-a"],
                "submission_reason": "high_stability_low_risk",
            },
        )

    async def test_gate_emits_candidate_and_marks_state_submitted(self) -> None:
        state = self._make_state("mem-1", version=3)

        result = await self.gate.evaluate([state])

        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.source_owner_type, OwnerType.MEMORY.value)
        self.assertEqual(candidate.source_owner_id, "mem-1")
        self.assertEqual(candidate.tenant_id, "tenant-1")
        self.assertEqual(candidate.statement, "Keep batteries cool for longevity.")
        self.assertEqual(candidate.supporting_memory_ids, ["mem-a", "mem-b"])
        self.assertEqual(candidate.supporting_trace_ids, ["trace-a"])
        self.assertTrue(candidate.dedupe_fingerprint)

        self.assertEqual(len(result.state_updates), 1)
        update = result.state_updates[0]
        self.assertEqual(update["owner_type"], OwnerType.MEMORY)
        self.assertEqual(update["owner_id"], "mem-1")
        self.assertEqual(update["expected_version"], 3)
        self.assertEqual(update["fields"]["consolidation_state"], ConsolidationState.SUBMITTED.value)

    async def test_evaluate_is_pure_does_not_persist_candidates(self) -> None:
        async def _boom(_candidates):
            raise AssertionError("evaluate() must not persist candidates")

        # If evaluate() calls the store persistence hook, this test should fail.
        self.store.save_many = _boom  # type: ignore[assignment]

        state = self._make_state("mem-pure", version=1)
        result = await self.gate.evaluate([state])
        self.assertEqual(len(result.candidates), 1)

    async def test_gate_suppresses_duplicate_candidates_within_cooldown(self) -> None:
        state = self._make_state("mem-2", version=1)

        first = await self.gate.evaluate([state])
        self.assertEqual(len(first.candidates), 1)
        # Caller owns persistence; without saving, cross-call cooldown dedupe cannot work.
        await self.store.save_many(first.candidates)

        self.clock.advance(30)
        second = await self.gate.evaluate([state])
        self.assertEqual(len(second.candidates), 0)
        self.assertEqual(len(second.state_updates), 0)

        self.clock.advance(31)
        third = await self.gate.evaluate([state])
        self.assertEqual(len(third.candidates), 1)

    async def test_gate_suppresses_duplicate_fingerprint_within_single_evaluate_call(self) -> None:
        state_a = self._make_state("mem-2x", version=1)
        state_b = self._make_state("mem-2x", version=1)

        result = await self.gate.evaluate([state_a, state_b])

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(len(result.state_updates), 1)

    async def test_gate_dedupe_fingerprint_is_semantic_not_owner_identity(self) -> None:
        state_a = self._make_state("mem-sem-a", version=1)
        state_b = self._make_state("mem-sem-b", version=1)

        result = await self.gate.evaluate([state_a, state_b])

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(len(result.state_updates), 1)

    async def test_feedback_mapping_rejected_with_material_new_evidence_resets_to_none(self) -> None:
        state = self._make_state("mem-3", version=7)
        state.consolidation_state = ConsolidationState.REJECTED
        state.metadata["last_consolidation_candidate_id"] = "cand-1"

        feedback = GovernanceFeedback(
            candidate_id="cand-1",
            owner_type=OwnerType.MEMORY,
            owner_id="mem-3",
            kind=GovernanceFeedbackKind.REJECTED,
            has_material_new_evidence=True,
        )

        updates = self.gate.map_governance_feedback([feedback], states=[state])
        self.assertEqual(len(updates), 1)
        update = updates[0]
        self.assertEqual(update["owner_type"], OwnerType.MEMORY)
        self.assertEqual(update["owner_id"], "mem-3")
        self.assertEqual(update["expected_version"], 7)
        self.assertEqual(update["fields"]["consolidation_state"], ConsolidationState.NONE.value)

    async def test_feedback_mapping_accepted_sets_state_accepted(self) -> None:
        state = self._make_state("mem-4", version=2)
        state.consolidation_state = ConsolidationState.SUBMITTED
        state.metadata["last_consolidation_candidate_id"] = "cand-2"

        feedback = GovernanceFeedback(
            candidate_id="cand-2",
            owner_type=OwnerType.MEMORY,
            owner_id="mem-4",
            kind=GovernanceFeedbackKind.ACCEPTED,
            has_material_new_evidence=False,
        )

        updates = self.gate.map_governance_feedback([feedback], states=[state])
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["fields"]["consolidation_state"], ConsolidationState.ACCEPTED.value)
        self.assertEqual(updates[0]["fields"]["exposure_state"], "guarded")

    async def test_feedback_mapping_ignores_stale_candidate_id(self) -> None:
        state = self._make_state("mem-stale", version=4)
        state.metadata["last_consolidation_candidate_id"] = "cand-current"

        feedback = GovernanceFeedback(
            candidate_id="cand-stale",
            owner_type=OwnerType.MEMORY,
            owner_id="mem-stale",
            kind=GovernanceFeedbackKind.ACCEPTED,
            has_material_new_evidence=False,
        )

        updates = self.gate.map_governance_feedback([feedback], states=[state])
        self.assertEqual(updates, [])

    async def test_feedback_mapping_contested_rejects_and_marks_exposure_contested(self) -> None:
        state = self._make_state("mem-contested", version=5)
        state.consolidation_state = ConsolidationState.SUBMITTED
        state.metadata["last_consolidation_candidate_id"] = "cand-contested"

        feedback = GovernanceFeedback(
            candidate_id="cand-contested",
            owner_type=OwnerType.MEMORY,
            owner_id="mem-contested",
            kind=GovernanceFeedbackKind.CONTESTED,
            has_material_new_evidence=False,
        )

        updates = self.gate.map_governance_feedback([feedback], states=[state])
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["fields"]["consolidation_state"], ConsolidationState.REJECTED.value)
        self.assertEqual(updates[0]["fields"]["exposure_state"], "contested")

    async def test_feedback_mapping_deprecated_reopens_exposure_only(self) -> None:
        state = self._make_state("mem-deprecated", version=6)
        state.consolidation_state = ConsolidationState.ACCEPTED
        state.metadata["last_consolidation_candidate_id"] = "cand-dep"

        feedback = GovernanceFeedback(
            candidate_id="cand-dep",
            owner_type=OwnerType.MEMORY,
            owner_id="mem-deprecated",
            kind=GovernanceFeedbackKind.DEPRECATED,
            has_material_new_evidence=False,
        )

        updates = self.gate.map_governance_feedback([feedback], states=[state])
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["fields"]["exposure_state"], "open")
        self.assertNotIn("consolidation_state", updates[0]["fields"])
