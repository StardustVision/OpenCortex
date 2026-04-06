# SPDX-License-Identifier: Apache-2.0
"""Cognitive state domain types."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
from typing import Any, Dict, List, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    project_id: str
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE
    exposure_state: ExposureState = ExposureState.OPEN
    consolidation_state: ConsolidationState = ConsolidationState.NONE
    activation_score: float = 0.0
    stability_score: float = 0.0
    risk_score: float = 0.0
    novelty_score: float = 0.0
    evidence_residual_score: float = 0.0
    access_count: int = 0
    retrieval_success_count: int = 0
    retrieval_failure_count: int = 0
    last_accessed_at: Optional[str] = None
    last_reinforced_at: Optional[str] = None
    last_penalized_at: Optional[str] = None
    last_mutation_at: Optional[str] = None
    last_mutation_reason: str = ""
    last_mutation_source: str = ""
    version: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
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
            "last_mutation_reason": self.last_mutation_reason,
            "last_mutation_source": self.last_mutation_source,
            "version": self.version,
            "metadata": json.dumps(self.metadata, separators=(",", ":"), sort_keys=True),
        }
        if self.last_accessed_at:
            record["last_accessed_at"] = self.last_accessed_at
        if self.last_reinforced_at:
            record["last_reinforced_at"] = self.last_reinforced_at
        if self.last_penalized_at:
            record["last_penalized_at"] = self.last_penalized_at
        if self.last_mutation_at:
            record["last_mutation_at"] = self.last_mutation_at
        return record

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CognitiveState":
        metadata_raw = data.get("metadata", {})
        if isinstance(metadata_raw, str):
            try:
                metadata = json.loads(metadata_raw) if metadata_raw else {}
            except json.JSONDecodeError:
                metadata = {}
        elif isinstance(metadata_raw, dict):
            metadata = dict(metadata_raw)
        else:
            metadata = {}
        return cls(
            state_id=data["state_id"],
            owner_type=OwnerType(data["owner_type"]),
            owner_id=data["owner_id"],
            tenant_id=data.get("tenant_id", ""),
            user_id=data.get("user_id", ""),
            project_id=data.get("project_id", ""),
            lifecycle_state=LifecycleState(data.get("lifecycle_state", LifecycleState.ACTIVE.value)),
            exposure_state=ExposureState(data.get("exposure_state", ExposureState.OPEN.value)),
            consolidation_state=ConsolidationState(
                data.get("consolidation_state", ConsolidationState.NONE.value)
            ),
            activation_score=float(data.get("activation_score", 0.0)),
            stability_score=float(data.get("stability_score", 0.0)),
            risk_score=float(data.get("risk_score", 0.0)),
            novelty_score=float(data.get("novelty_score", 0.0)),
            evidence_residual_score=float(data.get("evidence_residual_score", 0.0)),
            access_count=int(data.get("access_count", 0)),
            retrieval_success_count=int(data.get("retrieval_success_count", 0)),
            retrieval_failure_count=int(data.get("retrieval_failure_count", 0)),
            last_accessed_at=data.get("last_accessed_at") or None,
            last_reinforced_at=data.get("last_reinforced_at") or None,
            last_penalized_at=data.get("last_penalized_at") or None,
            last_mutation_at=data.get("last_mutation_at") or None,
            last_mutation_reason=data.get("last_mutation_reason", ""),
            last_mutation_source=data.get("last_mutation_source", ""),
            version=int(data.get("version", 1)),
            metadata=metadata,
        )


@dataclass
class MutationBatch:
    batch_id: str
    owner_ids: List[str] = field(default_factory=list)
    status: MutationBatchStatus = MutationBatchStatus.PENDING
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    committed_at: Optional[str] = None

    @property
    def id(self) -> str:
        return self.batch_id

    def to_dict(self) -> Dict[str, Any]:
        record = {
            "id": self.id,
            "batch_id": self.batch_id,
            "owner_ids": json.dumps(self.owner_ids, separators=(",", ":")),
            "status": self.status.value,
            "error": self.error,
            "metadata": json.dumps(self.metadata, separators=(",", ":"), sort_keys=True),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.committed_at:
            record["committed_at"] = self.committed_at
        return record

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MutationBatch":
        committed_at = data.get("committed_at") or None
        owner_ids_raw = data.get("owner_ids", [])
        if isinstance(owner_ids_raw, str):
            try:
                owner_ids_value = json.loads(owner_ids_raw) if owner_ids_raw else []
            except json.JSONDecodeError:
                owner_ids_value = []
        elif isinstance(owner_ids_raw, list):
            owner_ids_value = owner_ids_raw
        else:
            owner_ids_value = []

        metadata_raw = data.get("metadata", {})
        if isinstance(metadata_raw, str):
            try:
                metadata_value = json.loads(metadata_raw) if metadata_raw else {}
            except json.JSONDecodeError:
                metadata_value = {}
        elif isinstance(metadata_raw, dict):
            metadata_value = metadata_raw
        else:
            metadata_value = {}

        return cls(
            batch_id=data["batch_id"],
            owner_ids=list(owner_ids_value),
            status=MutationBatchStatus(data.get("status", MutationBatchStatus.PENDING.value)),
            error=data.get("error", ""),
            metadata=dict(metadata_value),
            created_at=data.get("created_at") or _utc_now_iso(),
            updated_at=data.get("updated_at") or _utc_now_iso(),
            committed_at=committed_at,
        )


@dataclass
class RecallMutationResult:
    state_updates: List[Dict[str, Any]] = field(default_factory=list)
    generated_candidates: List[Dict[str, Any]] = field(default_factory=list)
    quarantine_events: List[Dict[str, Any]] = field(default_factory=list)
    contestation_events: List[Dict[str, Any]] = field(default_factory=list)
    explanations: List[str] = field(default_factory=list)
