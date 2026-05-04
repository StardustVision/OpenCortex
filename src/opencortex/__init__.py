# SPDX-License-Identifier: Apache-2.0
"""
OpenCortex: Memory and context management for AI agents.

Tenant-based multi-user memory and context management system.
"""

__version__ = "0.8.0"

from opencortex.config import CortexConfig, get_config, init_config

__all__ = [
    "__version__",
    "CortexConfig",
    "get_config",
    "init_config",
    "CortexMemory",
    "MemoryOrchestrator",
]


def __getattr__(name: str):
    """Lazy-load heavy exports to avoid optional dependency import at package import time."""
    if name == "CortexMemory":
        from opencortex.cortex_memory import CortexMemory

        return CortexMemory
    if name == "MemoryOrchestrator":
        from opencortex.orchestrator import MemoryOrchestrator

        return MemoryOrchestrator
    raise AttributeError(f"module 'opencortex' has no attribute {name!r}")
