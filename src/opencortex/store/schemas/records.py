# SPDX-License-Identifier: Apache-2.0
"""Pydantic records used while assembling store writes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from opencortex.core.identity import IdentityProfile
from opencortex.store.types import ContextType


class Vectorize(str):
    """Text selected for vectorization."""


class Context(BaseModel):
    """Store record before projection to a storage payload."""

    uri: str
    parent_uri: str = ""
    is_leaf: bool = True
    abstract: str = ""
    overview: str = ""
    context_type: ContextType = ContextType.MEMORY
    category: str = ""
    related_uri: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    session_id: str = ""
    profile: IdentityProfile = Field(default_factory=IdentityProfile)
    vector: list[float] | None = None
    vectorize: Vectorize | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def get_vectorization_text(self) -> str:
        """Return text used for embedding."""
        if self.vectorize:
            return str(self.vectorize)
        return self.overview or self.abstract
