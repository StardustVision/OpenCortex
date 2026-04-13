# Autophagy Kernel Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the Phase 2 Autophagy kernel so OpenCortex has a real cognition state plane, recall mutation path, consolidation gate, and metabolism hooks without collapsing knowledge or skill back into the orchestrator.

**Architecture:** Add a new `opencortex.cognition` subpackage for Autophagy kernel code and keep it separate from retrieval infrastructure. Store `cognitive_state`, `consolidation_candidate`, and mutation-batch ledgers in dedicated collections behind `StorageInterface`, then integrate the kernel into existing `MemoryOrchestrator`, `TraceStore`, and `ContextManager` hooks incrementally.

**Tech Stack:** Python 3.10+, dataclasses, existing `StorageInterface` backends (`InMemoryStorage`, Qdrant), unittest-based tests, FastAPI lifecycle hooks already present in `MemoryOrchestrator` / `ContextManager`

---

## Scope Note

This plan implements only `Autophagy Kernel Phase 2`.

It includes:

- `cognitive_state` types, schemas, and stores
- recall mutation scoring and logical batch persistence
- consolidation candidate generation and governance feedback mapping
- metabolism controller and sweep hooks
- kernel integration into current memory / trace / session lifecycle

It does **not** implement:

- full `Knowledge Governance Layer`
- new HTTP endpoints for cognitive inspection
- Rust/PyO3 acceleration
- cross-process distributed locking
- final performance campaign for the whole north-star roadmap

## File Structure

### New files

- `src/opencortex/cognition/state_types.py`
  - enums and dataclasses for `CognitiveState`, `ConsolidationCandidate`, `GovernanceFeedback`, `MutationBatch`, and event payloads
- `src/opencortex/cognition/state_store.py`
  - `CognitiveStateStore` with collection init, per-owner lock registry, version-aware read/update helpers, and batch ledger persistence
- `src/opencortex/cognition/candidate_store.py`
  - `ConsolidationCandidateStore` for candidate CRUD, dedupe lookup, cooldown lookup, and committed-candidate listing
- `src/opencortex/cognition/mutation_engine.py`
  - `RecallMutationEngine` and result dataclasses
- `src/opencortex/cognition/consolidation_gate.py`
  - `ConsolidationGate` for candidate eligibility, fingerprinting, cooldowns, and governance feedback mapping
- `src/opencortex/cognition/metabolism.py`
  - `CognitiveMetabolismController` for `metabolize`, `compress`, `archive`, `forget`, and `review`
- `src/opencortex/cognition/kernel.py`
  - `AutophagyKernel` facade that wires state store, candidate store, mutation engine, consolidation gate, and metabolism controller
- `tests/test_cognitive_state_store.py`
  - unit tests for schemas, version-aware updates, and mutation batch persistence
- `tests/test_recall_mutation_engine.py`
  - unit tests for reinforce / penalize / quarantine / contest logic
- `tests/test_consolidation_gate.py`
  - unit tests for candidate creation, dedupe cooldown, and governance feedback transitions
- `tests/test_cognitive_metabolism.py`
  - unit tests for cooling, compression, archive, forget, and dominance penalty rules
- `tests/test_autophagy_kernel.py`
  - integration tests for orchestrator + trace + session-end hooks

### Modified files

- `src/opencortex/cognition/__init__.py`
  - export the new cognition-layer types and kernel entry points
- `src/opencortex/storage/collection_schemas.py`
  - add collection schemas and init helpers for `cognitive_state`, `consolidation_candidate`, and `cognitive_mutation_batch`
- `src/opencortex/orchestrator.py`
  - initialize the Autophagy kernel, call state initialization on memory writes, route recall outcomes into mutation, and schedule startup / periodic metabolism sweeps
- `src/opencortex/alpha/trace_store.py`
  - add an optional callback so saving a trace also initializes `cognitive_state`
- `src/opencortex/context/manager.py`
  - trigger `session-end tick` after commit/end flows and preserve current non-blocking task behavior
- `tests/test_e2e_phase1.py`
  - extend `InMemoryStorage` only if tests need helper support for new collection filtering or ordered batch assertions

## Task 1: Add Cognitive State Types, Schemas, And Stores

**Files:**
- Create: `tests/test_cognitive_state_store.py`
- Create: `src/opencortex/cognition/state_types.py`
- Create: `src/opencortex/cognition/state_store.py`
- Modify: `src/opencortex/storage/collection_schemas.py`
- Modify: `src/opencortex/cognition/__init__.py`

- [ ] **Step 1: Write the failing state-store tests**

```python
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from opencortex.cognition.state_store import CognitiveStateStore
from opencortex.cognition.state_types import (
    CognitiveState,
    ConsolidationState,
    ExposureState,
    LifecycleState,
    MutationBatch,
    MutationBatchStatus,
    OwnerType,
)
from opencortex.storage.collection_schemas import (
    init_cognitive_mutation_batch_collection,
    init_cognitive_state_collection,
)
from test_e2e_phase1 import InMemoryStorage


class TestCognitiveStateStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = InMemoryStorage()
        await init_cognitive_state_collection(self.storage, "cognitive_state")
        await init_cognitive_mutation_batch_collection(self.storage, "cognitive_batches")
        self.store = CognitiveStateStore(
            storage=self.storage,
            state_collection="cognitive_state",
            batch_collection="cognitive_batches",
        )

    async def test_save_and_get_round_trip(self):
        state = CognitiveState(
            state_id="state-1",
            owner_type=OwnerType.MEMORY,
            owner_id="mem-1",
            tenant_id="team",
            user_id="alice",
            project_id="public",
            lifecycle_state=LifecycleState.ACTIVE,
            exposure_state=ExposureState.OPEN,
            consolidation_state=ConsolidationState.NONE,
        )

        await self.store.save_state(state)
        loaded = await self.store.get_by_owner(OwnerType.MEMORY, "mem-1")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.owner_id, "mem-1")
        self.assertEqual(loaded.version, 0)
        self.assertEqual(loaded.lifecycle_state, LifecycleState.ACTIVE)

    async def test_update_state_rejects_stale_version(self):
        state = CognitiveState(
            state_id="state-2",
            owner_type=OwnerType.MEMORY,
            owner_id="mem-2",
            tenant_id="team",
            user_id="alice",
            project_id="public",
            lifecycle_state=LifecycleState.ACTIVE,
            exposure_state=ExposureState.OPEN,
            consolidation_state=ConsolidationState.NONE,
        )
        await self.store.save_state(state)

        updated = await self.store.update_state(
            owner_type=OwnerType.MEMORY,
            owner_id="mem-2",
            expected_version=0,
            fields={"activation_score": 0.8, "version": 1},
        )
        stale = await self.store.update_state(
            owner_type=OwnerType.MEMORY,
            owner_id="mem-2",
            expected_version=0,
            fields={"activation_score": 0.2, "version": 1},
        )

        self.assertTrue(updated)
        self.assertFalse(stale)

    async def test_persist_batch_commits_state_and_ledger(self):
        state = CognitiveState(
            state_id="state-3",
            owner_type=OwnerType.MEMORY,
            owner_id="mem-3",
            tenant_id="team",
            user_id="alice",
            project_id="public",
            lifecycle_state=LifecycleState.ACTIVE,
            exposure_state=ExposureState.OPEN,
            consolidation_state=ConsolidationState.NONE,
        )
        await self.store.save_state(state)

        batch = MutationBatch(
            batch_id="batch-1",
            owner_ids=["mem-3"],
            status=MutationBatchStatus.PENDING,
        )

        await self.store.persist_batch(
            batch=batch,
            state_updates=[
                {
                    "owner_type": OwnerType.MEMORY,
                    "owner_id": "mem-3",
                    "expected_version": 0,
                    "fields": {"activation_score": 0.9, "version": 1},
                }
            ],
        )

        ledger = await self.storage.get("cognitive_batches", ["batch-1"])
        loaded = await self.store.get_by_owner(OwnerType.MEMORY, "mem-3")

        self.assertEqual(ledger[0]["status"], "committed")
        self.assertEqual(loaded.activation_score, 0.9)
        self.assertEqual(loaded.version, 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_cognitive_state_store -v`

Expected: FAIL with import errors for `opencortex.cognition.state_store` / missing collection init helpers

- [ ] **Step 3: Add the state and batch dataclasses**

```python
# src/opencortex/cognition/state_types.py
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class OwnerType(str, Enum):
    MEMORY = "memory"
    TRACE = "trace"


class LifecycleState(str, Enum):
    ACTIVE = "active"
    COMPRESSED = "compressed"
    ARCHIVED = "archived"
    FORGOTTEN = "forgotten"


class ExposureState(str, Enum):
    OPEN = "open"
    GUARDED = "guarded"
    QUARANTINED = "quarantined"
    CONTESTED = "contested"


class ConsolidationState(str, Enum):
    NONE = "none"
    CANDIDATE = "candidate"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class MutationBatchStatus(str, Enum):
    PENDING = "pending"
    COMMITTED = "committed"
    FAILED = "failed"


@dataclass
class CognitiveState:
    state_id: str
    owner_type: OwnerType
    owner_id: str
    tenant_id: str
    user_id: str
    project_id: str = "public"
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE
    exposure_state: ExposureState = ExposureState.OPEN
    consolidation_state: ConsolidationState = ConsolidationState.NONE
    activation_score: float = 0.0
    stability_score: float = 0.0
    risk_score: float = 0.0
    novelty_score: float = 1.0
    evidence_residual_score: float = 0.0
    access_count: int = 0
    retrieval_success_count: int = 0
    retrieval_failure_count: int = 0
    last_accessed_at: str = ""
    last_reinforced_at: str = ""
    last_penalized_at: str = ""
    last_mutation_at: str = ""
    last_mutation_reason: str = ""
    last_mutation_source: str = ""
    version: int = 0

    def to_record(self) -> Dict[str, Any]:
        return {
            "id": self.state_id,
            "state_id": self.state_id,
            "owner_type": self.owner_type.value,
            "owner_id": self.owner_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "lifecycle_state": self.lifecycle_state.value,
            "exposure_state": self.exposure_state.value,
            "consolidation_state": self.consolidation_state.value,
            "activation_score": self.activation_score,
            "stability_score": self.stability_score,
            "risk_score": self.risk_score,
            "novelty_score": self.novelty_score,
            "evidence_residual_score": self.evidence_residual_score,
            "access_count": self.access_count,
            "retrieval_success_count": self.retrieval_success_count,
            "retrieval_failure_count": self.retrieval_failure_count,
            "last_accessed_at": self.last_accessed_at,
            "last_reinforced_at": self.last_reinforced_at,
            "last_penalized_at": self.last_penalized_at,
            "last_mutation_at": self.last_mutation_at,
            "last_mutation_reason": self.last_mutation_reason,
            "last_mutation_source": self.last_mutation_source,
            "version": self.version,
        }

    @classmethod
    def from_record(cls, record: Dict[str, Any]) -> "CognitiveState":
        return cls(
            state_id=record["state_id"],
            owner_type=OwnerType(record["owner_type"]),
            owner_id=record["owner_id"],
            tenant_id=record["tenant_id"],
            user_id=record["user_id"],
            project_id=record.get("project_id", "public"),
            lifecycle_state=LifecycleState(record["lifecycle_state"]),
            exposure_state=ExposureState(record["exposure_state"]),
            consolidation_state=ConsolidationState(record["consolidation_state"]),
            activation_score=record.get("activation_score", 0.0),
            stability_score=record.get("stability_score", 0.0),
            risk_score=record.get("risk_score", 0.0),
            novelty_score=record.get("novelty_score", 1.0),
            evidence_residual_score=record.get("evidence_residual_score", 0.0),
            access_count=record.get("access_count", 0),
            retrieval_success_count=record.get("retrieval_success_count", 0),
            retrieval_failure_count=record.get("retrieval_failure_count", 0),
            last_accessed_at=record.get("last_accessed_at", ""),
            last_reinforced_at=record.get("last_reinforced_at", ""),
            last_penalized_at=record.get("last_penalized_at", ""),
            last_mutation_at=record.get("last_mutation_at", ""),
            last_mutation_reason=record.get("last_mutation_reason", ""),
            last_mutation_source=record.get("last_mutation_source", ""),
            version=record.get("version", 0),
        )


@dataclass
class MutationBatch:
    batch_id: str
    owner_ids: List[str]
    status: MutationBatchStatus = MutationBatchStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_record(self) -> Dict[str, Any]:
        return {
            "id": self.batch_id,
            "batch_id": self.batch_id,
            "owner_ids": self.owner_ids,
            "status": self.status.value,
            "created_at": self.created_at,
        }
```

- [ ] **Step 4: Add schemas and store implementations**

```python
# src/opencortex/storage/collection_schemas.py
    @staticmethod
    def cognitive_state_collection(name: str) -> Dict[str, Any]:
        return {
            "CollectionName": name,
            "Description": "Autophagy cognitive state collection",
            "Fields": [
                {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
                {"FieldName": "state_id", "FieldType": "string"},
                {"FieldName": "owner_type", "FieldType": "string"},
                {"FieldName": "owner_id", "FieldType": "string"},
                {"FieldName": "tenant_id", "FieldType": "string"},
                {"FieldName": "user_id", "FieldType": "string"},
                {"FieldName": "project_id", "FieldType": "string"},
                {"FieldName": "lifecycle_state", "FieldType": "string"},
                {"FieldName": "exposure_state", "FieldType": "string"},
                {"FieldName": "consolidation_state", "FieldType": "string"},
                {"FieldName": "activation_score", "FieldType": "float"},
                {"FieldName": "stability_score", "FieldType": "float"},
                {"FieldName": "risk_score", "FieldType": "float"},
                {"FieldName": "novelty_score", "FieldType": "float"},
                {"FieldName": "evidence_residual_score", "FieldType": "float"},
                {"FieldName": "access_count", "FieldType": "int64"},
                {"FieldName": "retrieval_success_count", "FieldType": "int64"},
                {"FieldName": "retrieval_failure_count", "FieldType": "int64"},
                {"FieldName": "last_accessed_at", "FieldType": "string"},
                {"FieldName": "last_reinforced_at", "FieldType": "string"},
                {"FieldName": "last_penalized_at", "FieldType": "string"},
                {"FieldName": "last_mutation_at", "FieldType": "string"},
                {"FieldName": "last_mutation_reason", "FieldType": "string"},
                {"FieldName": "last_mutation_source", "FieldType": "string"},
                {"FieldName": "version", "FieldType": "int64"},
            ],
            "ScalarIndex": [
                "state_id", "owner_type", "owner_id", "tenant_id", "user_id",
                "project_id", "lifecycle_state", "exposure_state",
                "consolidation_state", "version",
            ],
        }
```

```python
# src/opencortex/storage/collection_schemas.py
async def init_cognitive_state_collection(storage: StorageInterface, name: str) -> bool:
    schema = CollectionSchemas.cognitive_state_collection(name)
    return await storage.create_collection(name, schema)


async def init_cognitive_mutation_batch_collection(storage: StorageInterface, name: str) -> bool:
    schema = {
        "CollectionName": name,
        "Description": "Autophagy mutation batch ledger",
        "Fields": [
            {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
            {"FieldName": "batch_id", "FieldType": "string"},
            {"FieldName": "owner_ids", "FieldType": "string"},
            {"FieldName": "status", "FieldType": "string"},
            {"FieldName": "created_at", "FieldType": "date_time"},
        ],
        "ScalarIndex": ["batch_id", "status", "created_at"],
    }
    return await storage.create_collection(name, schema)
```

```python
# src/opencortex/cognition/state_store.py
import asyncio
from collections import defaultdict
from typing import Any, Dict, Optional

from opencortex.cognition.state_types import CognitiveState, MutationBatch, OwnerType


class CognitiveStateStore:
    def __init__(
        self,
        storage,
        state_collection: str = "cognitive_state",
        batch_collection: str = "cognitive_mutation_batch",
    ):
        self._storage = storage
        self._state_collection = state_collection
        self._batch_collection = batch_collection
        self._locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def init(self) -> None:
        from opencortex.storage.collection_schemas import (
            init_cognitive_mutation_batch_collection,
            init_cognitive_state_collection,
        )
        await init_cognitive_state_collection(self._storage, self._state_collection)
        await init_cognitive_mutation_batch_collection(self._storage, self._batch_collection)

    async def save_state(self, state: CognitiveState) -> str:
        return await self._storage.upsert(self._state_collection, state.to_record())

    async def get_by_owner(self, owner_type: OwnerType, owner_id: str) -> Optional[CognitiveState]:
        rows = await self._storage.filter(
            self._state_collection,
            {"op": "and", "conds": [
                {"op": "must", "field": "owner_type", "conds": [owner_type.value]},
                {"op": "must", "field": "owner_id", "conds": [owner_id]},
            ]},
            limit=1,
        )
        return CognitiveState.from_record(rows[0]) if rows else None

    async def get_states_for_owners(self, owner_ids: list[str]) -> dict[str, CognitiveState]:
        out: dict[str, CognitiveState] = {}
        for owner_id in owner_ids:
            rows = await self._storage.filter(
                self._state_collection,
                {"op": "must", "field": "owner_id", "conds": [owner_id]},
                limit=1,
            )
            if rows:
                state = CognitiveState.from_record(rows[0])
                out[owner_id] = state
        return out

    async def update_state(
        self,
        owner_type: OwnerType,
        owner_id: str,
        expected_version: int,
        fields: Dict[str, Any],
    ) -> bool:
        async with self._locks[f"{owner_type.value}:{owner_id}"]:
            current = await self.get_by_owner(owner_type, owner_id)
            if not current or current.version != expected_version:
                return False
            return await self._storage.update(self._state_collection, current.state_id, fields)

    async def persist_batch(self, batch: MutationBatch, state_updates: list[Dict[str, Any]]) -> None:
        await self._storage.upsert(self._batch_collection, batch.to_record())
        for update in state_updates:
            ok = await self.update_state(**update)
            if not ok:
                await self._storage.update(
                    self._batch_collection,
                    batch.batch_id,
                    {"status": "failed"},
                )
                raise RuntimeError(f"stale cognitive_state update for {update['owner_id']}")
        await self._storage.update(
            self._batch_collection,
            batch.batch_id,
            {"status": "committed"},
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m unittest tests.test_cognitive_state_store -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/test_cognitive_state_store.py src/opencortex/cognition/state_types.py src/opencortex/cognition/state_store.py src/opencortex/storage/collection_schemas.py src/opencortex/cognition/__init__.py
git commit -m "feat: add cognitive state stores"
```

## Task 2: Implement Recall Mutation Engine

**Files:**
- Create: `tests/test_recall_mutation_engine.py`
- Create: `src/opencortex/cognition/mutation_engine.py`
- Modify: `src/opencortex/cognition/state_types.py`

- [ ] **Step 1: Write the failing mutation tests**

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.cognition.mutation_engine import RecallMutationEngine
from opencortex.cognition.state_types import (
    CognitiveState,
    ConsolidationState,
    ExposureState,
    LifecycleState,
    OwnerType,
)


class TestRecallMutationEngine(unittest.TestCase):
    def setUp(self):
        self.engine = RecallMutationEngine()
        self.base_state = CognitiveState(
            state_id="state-1",
            owner_type=OwnerType.MEMORY,
            owner_id="mem-1",
            tenant_id="team",
            user_id="alice",
            project_id="public",
            lifecycle_state=LifecycleState.ACTIVE,
            exposure_state=ExposureState.OPEN,
            consolidation_state=ConsolidationState.NONE,
        )

    def test_final_answer_usage_reinforces_state(self):
        result = self.engine.apply(
            query="how do I fix auth timeout",
            states={"mem-1": self.base_state},
            recall_outcome={
                "selected_results": ["mem-1"],
                "final_answer_used_memories": ["mem-1"],
                "rejected_results": [],
                "conflict_signals": [],
                "user_feedback": None,
            },
        )

        update = result.state_updates[0]
        self.assertEqual(update["owner_id"], "mem-1")
        self.assertGreater(update["fields"]["activation_score"], 0.0)
        self.assertEqual(update["fields"]["last_mutation_reason"], "reinforce")

    def test_recalled_but_unused_penalizes_hot_candidate(self):
        hot = self.base_state
        hot.activation_score = 0.9
        hot.access_count = 12
        result = self.engine.apply(
            query="auth timeout",
            states={"mem-1": hot},
            recall_outcome={
                "selected_results": ["mem-1"],
                "final_answer_used_memories": [],
                "rejected_results": ["mem-1"],
                "conflict_signals": [],
                "user_feedback": None,
            },
        )

        self.assertEqual(result.state_updates[0]["fields"]["last_mutation_reason"], "penalize")
        self.assertLess(result.state_updates[0]["fields"]["activation_score"], 0.9)

    def test_conflict_signal_marks_state_contested(self):
        result = self.engine.apply(
            query="what is the root cause",
            states={"mem-1": self.base_state},
            recall_outcome={
                "selected_results": ["mem-1"],
                "final_answer_used_memories": ["mem-1"],
                "rejected_results": [],
                "conflict_signals": [{"owner_id": "mem-1", "kind": "knowledge_conflict"}],
                "user_feedback": None,
            },
        )

        self.assertEqual(
            result.state_updates[0]["fields"]["exposure_state"],
            ExposureState.CONTESTED.value,
        )
        self.assertEqual(result.contestation_events[0]["owner_id"], "mem-1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_recall_mutation_engine -v`

Expected: FAIL with import errors for `RecallMutationEngine`

- [ ] **Step 3: Add mutation result types and engine implementation**

```python
# src/opencortex/cognition/state_types.py
@dataclass
class RecallMutationResult:
    state_updates: List[Dict[str, Any]] = field(default_factory=list)
    generated_candidates: List[Dict[str, Any]] = field(default_factory=list)
    quarantine_events: List[Dict[str, Any]] = field(default_factory=list)
    contestation_events: List[Dict[str, Any]] = field(default_factory=list)
    explanations: List[Dict[str, Any]] = field(default_factory=list)
```

```python
# src/opencortex/cognition/mutation_engine.py
from datetime import datetime, timezone

from opencortex.cognition.state_types import ExposureState, RecallMutationResult


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecallMutationEngine:
    def __init__(self, reinforce_gain: float = 0.25, penalize_loss: float = 0.15):
        self._reinforce_gain = reinforce_gain
        self._penalize_loss = penalize_loss

    def apply(self, query: str, states: dict, recall_outcome: dict) -> RecallMutationResult:
        result = RecallMutationResult()
        used = set(recall_outcome.get("final_answer_used_memories", []))
        rejected = set(recall_outcome.get("rejected_results", []))
        conflicts = {
            item["owner_id"]: item for item in recall_outcome.get("conflict_signals", [])
        }

        for owner_id, state in states.items():
            fields = {
                "access_count": state.access_count + 1,
                "last_accessed_at": _utc_now(),
                "version": state.version + 1,
            }
            reason = "observe"
            activation = state.activation_score

            if owner_id in used:
                gain = self._reinforce_gain * max(0.2, 1.0 - state.activation_score)
                activation = min(1.0, state.activation_score + gain)
                fields["retrieval_success_count"] = state.retrieval_success_count + 1
                fields["last_reinforced_at"] = _utc_now()
                reason = "reinforce"
            elif owner_id in rejected:
                loss = self._penalize_loss * max(0.5, state.activation_score)
                activation = max(0.0, state.activation_score - loss)
                fields["retrieval_failure_count"] = state.retrieval_failure_count + 1
                fields["last_penalized_at"] = _utc_now()
                reason = "penalize"

            fields["activation_score"] = activation
            fields["last_mutation_at"] = _utc_now()
            fields["last_mutation_reason"] = reason
            fields["last_mutation_source"] = "recall"

            if owner_id in conflicts:
                fields["exposure_state"] = ExposureState.CONTESTED.value
                result.contestation_events.append(
                    {"owner_id": owner_id, "reason": conflicts[owner_id]["kind"]}
                )

            result.state_updates.append(
                {
                    "owner_type": state.owner_type,
                    "owner_id": owner_id,
                    "expected_version": state.version,
                    "fields": fields,
                }
            )
            result.explanations.append(
                {"owner_id": owner_id, "reason": reason, "query": query}
            )

        return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_recall_mutation_engine -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_recall_mutation_engine.py src/opencortex/cognition/mutation_engine.py src/opencortex/cognition/state_types.py
git commit -m "feat: add recall mutation engine"
```

## Task 3: Implement Consolidation Gate And Candidate Dedupe

**Files:**
- Create: `tests/test_consolidation_gate.py`
- Modify: `src/opencortex/cognition/state_types.py`
- Create: `src/opencortex/cognition/candidate_store.py`
- Create: `src/opencortex/cognition/consolidation_gate.py`
- Modify: `src/opencortex/storage/collection_schemas.py`

- [ ] **Step 1: Write the failing consolidation tests**

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from opencortex.cognition.consolidation_gate import ConsolidationGate
from opencortex.cognition.state_types import (
    CognitiveState,
    ConsolidationState,
    ExposureState,
    GovernanceFeedback,
    GovernanceFeedbackKind,
    LifecycleState,
    OwnerType,
)
from test_e2e_phase1 import InMemoryStorage


class TestConsolidationGate(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = InMemoryStorage()
        self.gate = ConsolidationGate(storage=self.storage)
        await self.gate.init()
        self.state = CognitiveState(
            state_id="state-1",
            owner_type=OwnerType.MEMORY,
            owner_id="mem-1",
            tenant_id="team",
            user_id="alice",
            project_id="public",
            lifecycle_state=LifecycleState.ACTIVE,
            exposure_state=ExposureState.OPEN,
            consolidation_state=ConsolidationState.NONE,
            stability_score=0.92,
            activation_score=0.71,
        )

    async def test_evaluate_creates_candidate_for_stable_state(self):
        candidates = await self.gate.evaluate(
            states=[self.state],
            mutation_result={"generated_candidates": []},
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_owner_id, "mem-1")
        self.assertEqual(candidates[0].candidate_kind, "belief")

    async def test_duplicate_fingerprint_is_suppressed_inside_cooldown(self):
        first = await self.gate.evaluate(states=[self.state], mutation_result={})
        second = await self.gate.evaluate(states=[self.state], mutation_result={})

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    async def test_rejected_feedback_returns_state_to_none_only_with_new_evidence(self):
        feedback = GovernanceFeedback(
            candidate_id="cand-1",
            owner_type=OwnerType.MEMORY,
            owner_id="mem-1",
            kind=GovernanceFeedbackKind.REJECTED,
            has_material_new_evidence=True,
        )

        fields = self.gate.feedback_to_state_fields(
            current=self.state,
            feedback=feedback,
        )

        self.assertEqual(fields["consolidation_state"], ConsolidationState.NONE.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_consolidation_gate -v`

Expected: FAIL with import errors for `ConsolidationGate` / `GovernanceFeedback`

- [ ] **Step 3: Add candidate and feedback types**

```python
# src/opencortex/cognition/state_types.py
class GovernanceFeedbackKind(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CONTESTED = "contested"
    DEPRECATED = "deprecated"


@dataclass
class ConsolidationCandidate:
    candidate_id: str
    source_owner_type: OwnerType
    source_owner_id: str
    tenant_id: str
    user_id: str
    project_id: str
    candidate_kind: str
    statement: str
    abstract: str
    overview: str
    supporting_memory_ids: List[str] = field(default_factory=list)
    supporting_trace_ids: List[str] = field(default_factory=list)
    confidence_estimate: float = 0.0
    stability_score: float = 0.0
    risk_score: float = 0.0
    conflict_summary: str = ""
    submission_reason: str = ""
    dedupe_fingerprint: str = ""


@dataclass
class GovernanceFeedback:
    candidate_id: str
    owner_type: OwnerType
    owner_id: str
    kind: GovernanceFeedbackKind
    has_material_new_evidence: bool = False
```

- [ ] **Step 4: Implement candidate store and consolidation gate**

```python
# src/opencortex/storage/collection_schemas.py
async def init_consolidation_candidate_collection(storage: StorageInterface, name: str) -> bool:
    schema = {
        "CollectionName": name,
        "Description": "Autophagy consolidation candidates",
        "Fields": [
            {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
            {"FieldName": "candidate_id", "FieldType": "string"},
            {"FieldName": "tenant_id", "FieldType": "string"},
            {"FieldName": "user_id", "FieldType": "string"},
            {"FieldName": "project_id", "FieldType": "string"},
            {"FieldName": "source_owner_id", "FieldType": "string"},
            {"FieldName": "candidate_kind", "FieldType": "string"},
            {"FieldName": "statement", "FieldType": "string"},
            {"FieldName": "dedupe_fingerprint", "FieldType": "string"},
            {"FieldName": "status", "FieldType": "string"},
            {"FieldName": "created_at", "FieldType": "date_time"},
        ],
        "ScalarIndex": [
            "candidate_id", "tenant_id", "user_id", "project_id",
            "source_owner_id", "candidate_kind", "dedupe_fingerprint", "status", "created_at",
        ],
    }
    return await storage.create_collection(name, schema)
```

```python
# src/opencortex/cognition/candidate_store.py
import hashlib
import json
from datetime import datetime, timedelta, timezone


class ConsolidationCandidateStore:
    def __init__(self, storage, collection_name: str = "consolidation_candidate"):
        self._storage = storage
        self._collection = collection_name

    async def init(self) -> None:
        from opencortex.storage.collection_schemas import init_consolidation_candidate_collection
        await init_consolidation_candidate_collection(self._storage, self._collection)

    def build_fingerprint(self, payload: dict) -> str:
        normalized = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    async def find_recent_by_fingerprint(self, fingerprint: str, tenant_id: str) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        rows = await self._storage.filter(
            self._collection,
            {"op": "and", "conds": [
                {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
                {"op": "must", "field": "dedupe_fingerprint", "conds": [fingerprint]},
                {"op": "range", "field": "created_at", "gte": cutoff},
            ]},
            limit=5,
        )
        return rows

    async def save_many(self, candidates: list) -> None:
        if not candidates:
            return
        rows = []
        for candidate in candidates:
            rows.append(
                {
                    "id": candidate.candidate_id,
                    "candidate_id": candidate.candidate_id,
                    "tenant_id": candidate.tenant_id,
                    "user_id": candidate.user_id,
                    "project_id": candidate.project_id,
                    "source_owner_id": candidate.source_owner_id,
                    "candidate_kind": candidate.candidate_kind,
                    "statement": candidate.statement,
                    "dedupe_fingerprint": candidate.dedupe_fingerprint,
                    "status": "committed",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        await self._storage.batch_upsert(self._collection, rows)
```

```python
# src/opencortex/cognition/consolidation_gate.py
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from opencortex.cognition.candidate_store import ConsolidationCandidateStore
from opencortex.cognition.state_types import (
    ConsolidationCandidate,
    ConsolidationState,
    ExposureState,
    GovernanceFeedbackKind,
    OwnerType,
)
from opencortex.storage.collection_schemas import init_consolidation_candidate_collection


class ConsolidationGate:
    def __init__(self, storage, cooldown_hours: int = 24):
        self._storage = storage
        self._cooldown_hours = cooldown_hours
        self._candidate_store = ConsolidationCandidateStore(storage)

    async def init(self) -> None:
        await init_consolidation_candidate_collection(self._storage, "consolidation_candidate")

    async def evaluate(self, states: list, mutation_result: dict) -> list[ConsolidationCandidate]:
        out = []
        for state in states:
            if state.exposure_state == ExposureState.QUARANTINED:
                continue
            if state.stability_score < 0.8 or state.activation_score < 0.6:
                continue
            payload = {
                "candidate_kind": "belief",
                "statement": state.owner_id,
                "supporting_memory_ids": [state.owner_id] if state.owner_type == OwnerType.MEMORY else [],
                "supporting_trace_ids": [state.owner_id] if state.owner_type == OwnerType.TRACE else [],
            }
            fingerprint = self._candidate_store.build_fingerprint(payload)
            recent = await self._candidate_store.find_recent_by_fingerprint(fingerprint, state.tenant_id)
            if recent:
                continue
            out.append(
                ConsolidationCandidate(
                    candidate_id=str(uuid4()),
                    source_owner_type=state.owner_type,
                    source_owner_id=state.owner_id,
                    tenant_id=state.tenant_id,
                    user_id=state.user_id,
                    project_id=state.project_id,
                    candidate_kind="belief",
                    statement=state.owner_id,
                    abstract=state.owner_id,
                    overview=state.owner_id,
                    supporting_memory_ids=payload["supporting_memory_ids"],
                    supporting_trace_ids=payload["supporting_trace_ids"],
                    confidence_estimate=state.stability_score,
                    stability_score=state.stability_score,
                    risk_score=state.risk_score,
                    submission_reason="stable_state",
                    dedupe_fingerprint=fingerprint,
                )
            )
        return out

    def feedback_to_state_fields(self, current, feedback):
        if feedback.kind == GovernanceFeedbackKind.ACCEPTED:
            return {
                "consolidation_state": ConsolidationState.ACCEPTED.value,
                "exposure_state": ExposureState.GUARDED.value,
            }
        if feedback.kind == GovernanceFeedbackKind.REJECTED:
            target = ConsolidationState.NONE if feedback.has_material_new_evidence else ConsolidationState.REJECTED
            return {"consolidation_state": target.value}
        if feedback.kind == GovernanceFeedbackKind.CONTESTED:
            return {
                "consolidation_state": ConsolidationState.REJECTED.value,
                "exposure_state": ExposureState.CONTESTED.value,
            }
        return {"exposure_state": ExposureState.OPEN.value}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m unittest tests.test_consolidation_gate -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/test_consolidation_gate.py src/opencortex/cognition/state_types.py src/opencortex/cognition/candidate_store.py src/opencortex/cognition/consolidation_gate.py src/opencortex/storage/collection_schemas.py
git commit -m "feat: add consolidation gate"
```

## Task 4: Implement Cognitive Metabolism Controller

**Files:**
- Create: `tests/test_cognitive_metabolism.py`
- Create: `src/opencortex/cognition/metabolism.py`
- Modify: `src/opencortex/cognition/state_types.py`

- [ ] **Step 1: Write the failing metabolism tests**

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.cognition.metabolism import CognitiveMetabolismController
from opencortex.cognition.state_types import (
    CognitiveState,
    ConsolidationState,
    ExposureState,
    LifecycleState,
    OwnerType,
)


class TestCognitiveMetabolismController(unittest.TestCase):
    def setUp(self):
        self.controller = CognitiveMetabolismController()

    def test_cold_low_value_state_is_compressed(self):
        state = CognitiveState(
            state_id="state-1",
            owner_type=OwnerType.MEMORY,
            owner_id="mem-1",
            tenant_id="team",
            user_id="alice",
            lifecycle_state=LifecycleState.ACTIVE,
            exposure_state=ExposureState.OPEN,
            consolidation_state=ConsolidationState.NONE,
            activation_score=0.05,
            stability_score=0.2,
            risk_score=0.1,
            access_count=0,
        )

        result = self.controller.tick([state])

        self.assertEqual(result.state_updates[0]["fields"]["lifecycle_state"], "compressed")

    def test_dominant_hot_state_is_cooled(self):
        state = CognitiveState(
            state_id="state-2",
            owner_type=OwnerType.MEMORY,
            owner_id="mem-2",
            tenant_id="team",
            user_id="alice",
            lifecycle_state=LifecycleState.ACTIVE,
            exposure_state=ExposureState.OPEN,
            consolidation_state=ConsolidationState.NONE,
            activation_score=0.98,
            stability_score=0.9,
            risk_score=0.2,
            access_count=20,
        )

        result = self.controller.tick(
            [state],
            dominance_window={"mem-2": {"wins": 15, "success_rate": 0.55, "cluster": "auth"}},
        )

        self.assertLess(result.state_updates[0]["fields"]["activation_score"], 0.98)
        self.assertEqual(result.state_updates[0]["fields"]["last_mutation_reason"], "metabolize")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_cognitive_metabolism -v`

Expected: FAIL with import errors for `CognitiveMetabolismController`

- [ ] **Step 3: Implement metabolism result types and controller**

```python
# src/opencortex/cognition/state_types.py
@dataclass
class MetabolismResult:
    state_updates: List[Dict[str, Any]] = field(default_factory=list)
    review_events: List[Dict[str, Any]] = field(default_factory=list)
```

```python
# src/opencortex/cognition/metabolism.py
from datetime import datetime, timezone

from opencortex.cognition.state_types import LifecycleState, MetabolismResult


class CognitiveMetabolismController:
    def __init__(
        self,
        cooling_factor: float = 0.1,
        compress_threshold: float = 0.08,
        archive_threshold: float = 0.02,
    ):
        self._cooling_factor = cooling_factor
        self._compress_threshold = compress_threshold
        self._archive_threshold = archive_threshold

    def tick(self, states: list, dominance_window: dict | None = None) -> MetabolismResult:
        dominance_window = dominance_window or {}
        result = MetabolismResult()
        for state in states:
            fields = {
                "version": state.version + 1,
                "last_mutation_at": datetime.now(timezone.utc).isoformat(),
                "last_mutation_source": "metabolism",
            }
            reason = "metabolize"

            if state.activation_score <= self._archive_threshold and state.lifecycle_state == LifecycleState.COMPRESSED:
                fields["lifecycle_state"] = LifecycleState.ARCHIVED.value
            elif state.activation_score <= self._compress_threshold and state.lifecycle_state == LifecycleState.ACTIVE:
                fields["lifecycle_state"] = LifecycleState.COMPRESSED.value

            dominance = dominance_window.get(state.owner_id, {})
            if dominance.get("wins", 0) >= 10 and dominance.get("success_rate", 0.0) < 0.7:
                fields["activation_score"] = max(0.0, state.activation_score - self._cooling_factor)
            elif state.activation_score > 0:
                fields["activation_score"] = max(0.0, state.activation_score - (self._cooling_factor / 2))

            fields["last_mutation_reason"] = reason
            result.state_updates.append(
                {
                    "owner_type": state.owner_type,
                    "owner_id": state.owner_id,
                    "expected_version": state.version,
                    "fields": fields,
                }
            )
        return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_cognitive_metabolism -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_cognitive_metabolism.py src/opencortex/cognition/metabolism.py src/opencortex/cognition/state_types.py
git commit -m "feat: add cognitive metabolism controller"
```

## Task 5: Wire The Autophagy Kernel Facade

**Files:**
- Create: `src/opencortex/cognition/kernel.py`
- Modify: `src/opencortex/cognition/__init__.py`
- Modify: `src/opencortex/cognition/state_store.py`
- Modify: `src/opencortex/cognition/candidate_store.py`

- [ ] **Step 1: Write the failing kernel unit tests**

```python
import os
import sys
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.cognition.kernel import AutophagyKernel
from opencortex.cognition.state_types import OwnerType


class TestAutophagyKernelFacade(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_owner_creates_missing_state(self):
        store = AsyncMock()
        store.get_by_owner.return_value = None
        store.save_state.return_value = "state-1"
        kernel = AutophagyKernel(
            state_store=store,
            candidate_store=AsyncMock(),
            mutation_engine=Mock(),
            consolidation_gate=Mock(),
            metabolism_controller=Mock(),
        )

        await kernel.initialize_owner(
            owner_type=OwnerType.MEMORY,
            owner_id="mem-1",
            tenant_id="team",
            user_id="alice",
            project_id="public",
        )

        store.save_state.assert_awaited()

    async def test_apply_recall_outcome_persists_mutation_batch(self):
        store = AsyncMock()
        state = Mock(owner_type=OwnerType.MEMORY, owner_id="mem-1", version=0)
        store.get_states_for_owners.return_value = {"mem-1": state}
        mutation = Mock(
            state_updates=[{"owner_type": OwnerType.MEMORY, "owner_id": "mem-1", "expected_version": 0, "fields": {"version": 1}}],
            generated_candidates=[],
            quarantine_events=[],
            contestation_events=[],
            explanations=[],
        )
        mutation_engine = Mock()
        mutation_engine.apply.return_value = mutation
        gate = AsyncMock()
        gate.evaluate.return_value = []
        candidate_store = AsyncMock()
        kernel = AutophagyKernel(
            state_store=store,
            candidate_store=candidate_store,
            mutation_engine=mutation_engine,
            consolidation_gate=gate,
            metabolism_controller=Mock(),
        )

        await kernel.apply_recall_outcome(
            owner_ids=["mem-1"],
            query="auth timeout",
            recall_outcome={"selected_results": ["mem-1"]},
        )

        store.persist_batch.assert_awaited()
        candidate_store.save_many.assert_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_autophagy_kernel.TestAutophagyKernelFacade -v`

Expected: FAIL with import errors for `AutophagyKernel`

- [ ] **Step 3: Implement the kernel facade**

```python
# src/opencortex/cognition/kernel.py
from uuid import uuid4

from opencortex.cognition.state_types import (
    CognitiveState,
    ConsolidationState,
    ExposureState,
    LifecycleState,
    MutationBatch,
    OwnerType,
)


class AutophagyKernel:
    def __init__(
        self,
        state_store,
        candidate_store,
        mutation_engine,
        consolidation_gate,
        metabolism_controller,
    ):
        self._state_store = state_store
        self._candidate_store = candidate_store
        self._mutation_engine = mutation_engine
        self._consolidation_gate = consolidation_gate
        self._metabolism_controller = metabolism_controller

    async def initialize_owner(
        self,
        owner_type: OwnerType,
        owner_id: str,
        tenant_id: str,
        user_id: str,
        project_id: str = "public",
    ) -> None:
        existing = await self._state_store.get_by_owner(owner_type, owner_id)
        if existing:
            return
        await self._state_store.save_state(
            CognitiveState(
                state_id=f"{owner_type.value}:{owner_id}",
                owner_type=owner_type,
                owner_id=owner_id,
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=project_id,
                lifecycle_state=LifecycleState.ACTIVE,
                exposure_state=ExposureState.OPEN,
                consolidation_state=ConsolidationState.NONE,
            )
        )

    async def apply_recall_outcome(self, owner_ids: list[str], query: str, recall_outcome: dict):
        states = await self._state_store.get_states_for_owners(owner_ids)
        mutation = self._mutation_engine.apply(query=query, states=states, recall_outcome=recall_outcome)
        batch = MutationBatch(batch_id=str(uuid4()), owner_ids=owner_ids)
        await self._state_store.persist_batch(batch=batch, state_updates=mutation.state_updates)
        candidates = await self._consolidation_gate.evaluate(list(states.values()), {"generated_candidates": mutation.generated_candidates})
        await self._candidate_store.save_many(candidates)
        return mutation, candidates

    async def metabolize_states(self, owner_ids: list[str], dominance_window: dict | None = None):
        states = await self._state_store.get_states_for_owners(owner_ids)
        result = self._metabolism_controller.tick(list(states.values()), dominance_window=dominance_window)
        batch = MutationBatch(batch_id=str(uuid4()), owner_ids=owner_ids)
        await self._state_store.persist_batch(batch=batch, state_updates=result.state_updates)
        return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_autophagy_kernel.TestAutophagyKernelFacade -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/cognition/kernel.py src/opencortex/cognition/__init__.py src/opencortex/cognition/state_store.py src/opencortex/cognition/candidate_store.py tests/test_autophagy_kernel.py
git commit -m "feat: add autophagy kernel facade"
```

## Task 6: Integrate Autophagy Into Orchestrator, Trace, And Session Lifecycle

**Files:**
- Modify: `src/opencortex/orchestrator.py`
- Modify: `src/opencortex/alpha/trace_store.py`
- Modify: `src/opencortex/context/manager.py`
- Modify: `tests/test_context_manager.py`
- Modify: `tests/test_recall_planner.py`
- Create: `tests/test_autophagy_kernel.py`

- [ ] **Step 1: Write the failing integration tests**

```python
import asyncio
import os
import sys
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from opencortex.config import CortexConfig, init_config
from opencortex.cognition.state_types import OwnerType
from opencortex.context.manager import ContextManager
from opencortex.http.request_context import reset_request_identity, set_request_identity
from opencortex.orchestrator import MemoryOrchestrator
from test_e2e_phase1 import InMemoryStorage, MockEmbedder


class TestAutophagyIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="opencortex_autophagy_")
        self.config = CortexConfig(
            data_root=self.temp_dir,
            embedding_dimension=MockEmbedder.DIMENSION,
            rerank_provider="disabled",
        )
        init_config(self.config)
        self.identity = set_request_identity("team", "alice")

    def tearDown(self):
        reset_request_identity(self.identity)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.run(coro)

    def test_add_initializes_memory_state(self):
        orch = MemoryOrchestrator(
            config=self.config,
            storage=InMemoryStorage(),
            embedder=MockEmbedder(),
        )
        self._run(orch.init())

        ctx = self._run(orch.add(abstract="auth timeout fix", content="use retry budget"))
        state = self._run(
            orch._autophagy_kernel._state_store.get_by_owner(OwnerType.MEMORY, ctx.id)
        )

        self.assertIsNotNone(state)
        self.assertEqual(state.owner_id, ctx.id)
        self._run(orch.close())

    def test_session_end_triggers_metabolism_tick(self):
        orch = MemoryOrchestrator(
            config=self.config,
            storage=InMemoryStorage(),
            embedder=MockEmbedder(),
        )
        self._run(orch.init())
        orch._autophagy_kernel.metabolize_states = AsyncMock(return_value=None)
        cm = orch._context_manager

        result = self._run(cm.handle(
            session_id="sess-1",
            phase="end",
            tenant_id="team",
            user_id="alice",
        ))

        self.assertEqual(result["status"], "closed")
        orch._autophagy_kernel.metabolize_states.assert_awaited()
        self._run(orch.close())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_autophagy_kernel.TestAutophagyIntegration -v`

Expected: FAIL because `MemoryOrchestrator` has no `_autophagy_kernel` and `ContextManager` does not call metabolism hooks

- [ ] **Step 3: Wire kernel initialization and lifecycle hooks**

```python
# src/opencortex/orchestrator.py inside __init__
self._autophagy_kernel = None
self._cognitive_state_store = None
```

```python
# src/opencortex/orchestrator.py inside init() after storage/embedder are ready
from opencortex.cognition import (
    AutophagyKernel,
    CognitiveMetabolismController,
    CognitiveStateStore,
    ConsolidationCandidateStore,
    ConsolidationGate,
    RecallMutationEngine,
)

self._cognitive_state_store = CognitiveStateStore(self._storage)
await self._cognitive_state_store.init()
candidate_store = ConsolidationCandidateStore(self._storage)
await candidate_store.init()
mutation_engine = RecallMutationEngine()
gate = ConsolidationGate(self._storage)
await gate.init()
self._autophagy_kernel = AutophagyKernel(
    state_store=self._cognitive_state_store,
    candidate_store=candidate_store,
    mutation_engine=mutation_engine,
    consolidation_gate=gate,
    metabolism_controller=CognitiveMetabolismController(),
)
```

```python
# src/opencortex/orchestrator.py after successful add()
if self._autophagy_kernel:
    tid, uid = get_effective_identity()
    await self._autophagy_kernel.initialize_owner(
        owner_type=OwnerType.MEMORY,
        owner_id=record_id,
        tenant_id=tid,
        user_id=uid,
        project_id=get_effective_project_id(),
    )
```

```python
# src/opencortex/alpha/trace_store.py
class TraceStore:
    def __init__(self, storage, embedder, cortex_fs, collection_name="traces", embedding_dim=1024, on_trace_saved=None):
        self._on_trace_saved = on_trace_saved

    async def save(self, trace: Trace) -> str:
        ...
        if self._on_trace_saved:
            await self._on_trace_saved(trace)
        return trace.trace_id
```

```python
# src/opencortex/context/manager.py inside __init__ and prepare path
self._prepared_owner_ids: Dict[SessionKey, Set[str]] = {}
...
self._prepared_owner_ids.setdefault(session_key, set()).update(
    item.get("id") for item in memory_results if item.get("id")
)
```

```python
# src/opencortex/context/manager.py inside end/close path
session_owner_ids = list(self._prepared_owner_ids.get(session_key, set()))
if self._orchestrator._autophagy_kernel:
    task = asyncio.create_task(
        self._orchestrator._autophagy_kernel.metabolize_states(session_owner_ids)
    )
    self._pending_tasks.add(task)
    task.add_done_callback(self._pending_tasks.discard)
```

- [ ] **Step 4: Add recall-path hook with a narrow first integration**

```python
# src/opencortex/orchestrator.py at the end of search() after results are built
if self._autophagy_kernel and results.memories:
    owner_ids = [item.get("id") for item in results.memories if item.get("id")]
    if owner_ids:
        await self._autophagy_kernel.apply_recall_outcome(
            owner_ids=owner_ids,
            query=query,
            recall_outcome={
                "selected_results": owner_ids,
                "final_answer_used_memories": [],
                "rejected_results": [],
                "conflict_signals": [],
            },
        )
```

- [ ] **Step 5: Run targeted tests to verify integration passes**

Run: `python -m unittest tests.test_autophagy_kernel.TestAutophagyIntegration -v`

Expected: PASS

Run: `python -m unittest tests.test_recall_planner.TestOrchestratorRecallPlanning -v`

Expected: PASS

Run: `python -m pytest tests/test_context_manager.py -q`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/orchestrator.py src/opencortex/alpha/trace_store.py src/opencortex/context/manager.py tests/test_context_manager.py tests/test_recall_planner.py tests/test_autophagy_kernel.py
git commit -m "feat: integrate autophagy kernel lifecycle hooks"
```

## Task 7: Add Reconciliation And Startup Sweep Coverage

**Files:**
- Modify: `src/opencortex/cognition/state_store.py`
- Modify: `src/opencortex/orchestrator.py`
- Modify: `tests/test_autophagy_kernel.py`

- [ ] **Step 1: Write the failing reconciliation tests**

```python
class TestAutophagyReconciliation(unittest.IsolatedAsyncioTestCase):
    async def test_failed_batch_is_returned_by_pending_reconciliation(self):
        storage = InMemoryStorage()
        store = CognitiveStateStore(storage)
        await store.init()
        await storage.upsert(
            "cognitive_mutation_batch",
            {"id": "batch-1", "batch_id": "batch-1", "owner_ids": ["mem-1"], "status": "failed"},
        )

        pending = await store.list_unfinished_batches()

        self.assertEqual([row["batch_id"] for row in pending], ["batch-1"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_autophagy_kernel.TestAutophagyReconciliation -v`

Expected: FAIL because `CognitiveStateStore.list_unfinished_batches()` does not exist

- [ ] **Step 3: Implement unfinished-batch sweep and orchestrator startup hook**

```python
# src/opencortex/cognition/state_store.py
    async def list_unfinished_batches(self) -> list[dict]:
        return await self._storage.filter(
            self._batch_collection,
            {"op": "must", "field": "status", "conds": ["pending", "failed"]},
            limit=1000,
        )
```

```python
# src/opencortex/orchestrator.py inside init()
if self._autophagy_kernel:
    asyncio.create_task(self._run_autophagy_startup_sweep())
    asyncio.create_task(self._run_autophagy_periodic_loop())
```

```python
# src/opencortex/orchestrator.py
async def _run_autophagy_startup_sweep(self) -> None:
    if not self._cognitive_state_store:
        return
    pending = await self._cognitive_state_store.list_unfinished_batches()
    if pending:
        logger.info("[MemoryOrchestrator] Autophagy startup sweep found %d unfinished batches", len(pending))


async def _run_autophagy_periodic_loop(self) -> None:
    while True:
        await asyncio.sleep(900)
        if self._autophagy_kernel:
            await self._autophagy_kernel.metabolize_states([])
```

- [ ] **Step 4: Run targeted tests to verify it passes**

Run: `python -m unittest tests.test_autophagy_kernel.TestAutophagyReconciliation -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/cognition/state_store.py src/opencortex/orchestrator.py tests/test_autophagy_kernel.py
git commit -m "feat: add autophagy startup reconciliation"
```

## Verification Checklist

- [ ] Run: `python -m unittest tests.test_cognitive_state_store -v`
- [ ] Run: `python -m unittest tests.test_recall_mutation_engine -v`
- [ ] Run: `python -m unittest tests.test_consolidation_gate -v`
- [ ] Run: `python -m unittest tests.test_cognitive_metabolism -v`
- [ ] Run: `python -m unittest tests.test_autophagy_kernel -v`
- [ ] Run: `python -m unittest tests.test_recall_planner.TestOrchestratorRecallPlanning -v`
- [ ] Run: `python -m pytest tests/test_context_manager.py -q`
- [ ] Run: `python -m unittest tests.test_http_server.TestHTTPServer.test_03_search -v`

## Self-Review

Spec coverage checked:

- `cognitive_state` 独立数据面: Task 1
- recall mutation + 批量持久化: Task 2, Task 5
- consolidation gate + feedback: Task 3
- metabolism + dominance penalty + sweep: Task 4, Task 7
- orchestrator / trace / session hooks: Task 6

Placeholder scan checked:

- no `TODO`
- no `TBD`
- no “write tests for the above”

Type consistency checked:

- stores use `OwnerType`
- batch ledger uses `MutationBatch`
- consolidation feedback uses `GovernanceFeedbackKind`
- metabolism output uses `MetabolismResult`
