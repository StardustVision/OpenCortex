# SPDX-License-Identifier: Apache-2.0
"""Legacy recall events owned by the current recall path."""

from __future__ import annotations

from typing import Any, List

from pydantic import BaseModel
from pydantic import Field


class RecallCompletedEvent(BaseModel):
    """Emitted by the legacy recall path after a search completes."""

    model_config = {"frozen": True}

    query: str
    memories: List[Any] = Field(default_factory=list)
    resources: List[Any] = Field(default_factory=list)
    skills: List[Any] = Field(default_factory=list)
    tenant_id: str
    user_id: str

    @property
    def name(self) -> str:
        """Event name used by the manager."""
        return "recall_completed"
