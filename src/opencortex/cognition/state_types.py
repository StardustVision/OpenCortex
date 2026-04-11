# SPDX-License-Identifier: Apache-2.0
"""Cognitive state domain types."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
from typing import Any, Dict, List, Optional
from uuid import uuid4


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


class GovernanceFeedbackKind(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CONTESTED = "contested"
    DEPRECATED = "deprecated"


@dataclass
class ConsolidationCandidate:
    candidate_id: str
    source_owner_type: str
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

    @property
    def id(self) -> str:
        return self.candidate_id

    @staticmethod
    def new_id() -> str:
        return str(uuid4())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "candidate_id": self.candidate_id,
            "source_owner_type": self.source_owner_type,
            "source_owner_id": self.source_owner_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "candidate_kind": self.candidate_kind,
            "statement": self.statement,
            "abstract": self.abstract,
            "overview": self.overview,
            "supporting_memory_ids": json.dumps(self.supporting_memory_ids, separators=(",", ":")),
            "supporting_trace_ids": json.dumps(self.supporting_trace_ids, separators=(",", ":")),
            "confidence_estimate": float(self.confidence_estimate),
            "stability_score": float(self.stability_score),
            "risk_score": float(self.risk_score),
            "conflict_summary": self.conflict_summary,
            "submission_reason": self.submission_reason,
            "dedupe_fingerprint": self.dedupe_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConsolidationCandidate":
        mem_ids_raw = data.get("supporting_memory_ids", [])
        if isinstance(mem_ids_raw, str):
            try:
                mem_ids = json.loads(mem_ids_raw) if mem_ids_raw else []
            except json.JSONDecodeError:
                mem_ids = []
        elif isinstance(mem_ids_raw, list):
            mem_ids = mem_ids_raw
        else:
            mem_ids = []

        trace_ids_raw = data.get("supporting_trace_ids", [])
        if isinstance(trace_ids_raw, str):
            try:
                trace_ids = json.loads(trace_ids_raw) if trace_ids_raw else []
            except json.JSONDecodeError:
                trace_ids = []
        elif isinstance(trace_ids_raw, list):
            trace_ids = trace_ids_raw
        else:
            trace_ids = []

        return cls(
            candidate_id=data.get("candidate_id") or data.get("id") or "",
            source_owner_type=str(data.get("source_owner_type") or ""),
            source_owner_id=str(data.get("source_owner_id") or ""),
            tenant_id=str(data.get("tenant_id") or ""),
            user_id=str(data.get("user_id") or ""),
            project_id=str(data.get("project_id") or ""),
            candidate_kind=str(data.get("candidate_kind") or ""),
            statement=str(data.get("statement") or ""),
            abstract=str(data.get("abstract") or ""),
            overview=str(data.get("overview") or ""),
            supporting_memory_ids=list(mem_ids),
            supporting_trace_ids=list(trace_ids),
            confidence_estimate=float(data.get("confidence_estimate", 0.0)),
            stability_score=float(data.get("stability_score", 0.0)),
            risk_score=float(data.get("risk_score", 0.0)),
            conflict_summary=str(data.get("conflict_summary") or ""),
            submission_reason=str(data.get("submission_reason") or ""),
            dedupe_fingerprint=str(data.get("dedupe_fingerprint") or ""),
        )


@dataclass
class GovernanceFeedback:
    candidate_id: str
    owner_type: "OwnerType"
    owner_id: str
    kind: GovernanceFeedbackKind
    has_material_new_evidence: bool = False


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
    explanations: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class MetabolismResult:
    """Pure/store-free result envelope for metabolism ticks."""

    state_updates: List[Dict[str, Any]] = field(default_factory=list)
    review_events: List[Dict[str, Any]] = field(default_factory=list)
