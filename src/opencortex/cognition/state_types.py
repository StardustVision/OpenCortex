# SPDX-License-Identifier: Apache-2.0
"""Cognitive state domain types."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OwnerType(str, Enum):
    USER = "user"
    SESSION = "session"
    TENANT = "tenant"


class LifecycleState(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class ExposureState(str, Enum):
    PRIVATE = "private"
    SHARED = "shared"
    PUBLIC = "public"


class ConsolidationState(str, Enum):
    UNCONSOLIDATED = "unconsolidated"
    CONSOLIDATED = "consolidated"


class MutationBatchStatus(str, Enum):
    PENDING = "pending"
    COMMITTED = "committed"
    FAILED = "failed"


@dataclass
class CognitiveState:
    owner_type: OwnerType
    owner_id: str
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE
    exposure_state: ExposureState = ExposureState.PRIVATE
    consolidation_state: ConsolidationState = ConsolidationState.UNCONSOLIDATED
    version: int = 1
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)

    @property
    def id(self) -> str:
        return f"{self.owner_type.value}:{self.owner_id}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "owner_type": self.owner_type.value,
            "owner_id": self.owner_id,
            "lifecycle_state": self.lifecycle_state.value,
            "exposure_state": self.exposure_state.value,
            "consolidation_state": self.consolidation_state.value,
            "version": self.version,
            "payload": self.payload,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CognitiveState":
        return cls(
            owner_type=OwnerType(data["owner_type"]),
            owner_id=data["owner_id"],
            lifecycle_state=LifecycleState(data.get("lifecycle_state", LifecycleState.ACTIVE.value)),
            exposure_state=ExposureState(data.get("exposure_state", ExposureState.PRIVATE.value)),
            consolidation_state=ConsolidationState(
                data.get("consolidation_state", ConsolidationState.UNCONSOLIDATED.value)
            ),
            version=int(data.get("version", 1)),
            payload=dict(data.get("payload", {})),
            created_at=data.get("created_at") or _utc_now_iso(),
            updated_at=data.get("updated_at") or _utc_now_iso(),
        )


@dataclass
class MutationBatch:
    batch_id: str
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
            "status": self.status.value,
            "error": self.error,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.committed_at:
            record["committed_at"] = self.committed_at
        return record

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MutationBatch":
        committed_at = data.get("committed_at") or None
        return cls(
            batch_id=data["batch_id"],
            status=MutationBatchStatus(data.get("status", MutationBatchStatus.PENDING.value)),
            error=data.get("error", ""),
            metadata=dict(data.get("metadata", {})),
            created_at=data.get("created_at") or _utc_now_iso(),
            updated_at=data.get("updated_at") or _utc_now_iso(),
            committed_at=committed_at,
        )
