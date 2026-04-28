# SPDX-License-Identifier: Apache-2.0
"""Write-path derive and fallback derive coordination."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from opencortex.services.memory_write_service import MemoryWriteService


@dataclass(frozen=True)
class MemoryWriteDeriveResult:
    """Result of deriving write-path abstract, overview, and layers."""

    abstract: str
    overview: str
    layers: Dict[str, Any] = field(default_factory=dict)
    derive_layers_ms: int = 0


class MemoryWriteDeriveService:
    """Owns derive/fallback derive decisions for memory writes."""

    def __init__(self, write_service: "MemoryWriteService") -> None:
        """Bind the derive service to a write service facade."""
        self._write_service = write_service

    @property
    def _orch(self) -> Any:
        return self._write_service._orch

    async def derive_for_write(
        self,
        *,
        abstract: str,
        overview: str,
        content: str,
        is_leaf: bool,
        defer_derive: bool,
    ) -> MemoryWriteDeriveResult:
        """Derive or fallback-fill write summary fields for normal add()."""
        orch = self._orch
        if content and is_leaf and not defer_derive:
            derive_started = asyncio.get_running_loop().time()
            layers = await orch._derive_layers(
                user_abstract=abstract,
                content=content,
                user_overview=overview,
            )
            derive_layers_ms = int(
                (asyncio.get_running_loop().time() - derive_started) * 1000
            )
            return MemoryWriteDeriveResult(
                abstract=abstract or layers["abstract"],
                overview=overview or layers["overview"],
                layers=layers,
                derive_layers_ms=derive_layers_ms,
            )

        if content and is_leaf and defer_derive:
            resolved_overview = overview
            if not resolved_overview:
                resolved_overview = orch._fallback_overview_from_content(
                    user_overview=overview,
                    content=content,
                )
            resolved_abstract = abstract
            if not resolved_abstract:
                resolved_abstract = orch._derive_abstract_from_overview(
                    user_abstract=abstract,
                    overview=resolved_overview,
                    content=content,
                )
            return MemoryWriteDeriveResult(
                abstract=resolved_abstract,
                overview=resolved_overview,
            )

        return MemoryWriteDeriveResult(abstract=abstract, overview=overview)
