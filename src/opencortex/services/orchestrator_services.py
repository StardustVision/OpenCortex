# SPDX-License-Identifier: Apache-2.0
"""Backward-compatible service registry exports."""

from opencortex.services.cortex_memory_services import (
    CortexMemoryServices,
    MemoryOrchestratorServices,
)

__all__ = ["CortexMemoryServices", "MemoryOrchestratorServices"]
