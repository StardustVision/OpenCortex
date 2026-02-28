# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# Ported from OpenViking (https://github.com/volcengine/openviking)
# SPDX-License-Identifier: Apache-2.0
"""
OpenCortex: Memory and context management for AI agents.

Ported and adapted from OpenViking with tenant-based multi-user isolation.
"""

__version__ = "0.1.0"

from opencortex.config import CortexConfig, get_config, init_config

__all__ = [
    "__version__",
    "CortexConfig",
    "get_config",
    "init_config",
    "MemoryOrchestrator",
]


def __getattr__(name: str):
    """Lazy-load heavy exports to avoid optional dependency import at package import time."""
    if name == "MemoryOrchestrator":
        from opencortex.orchestrator import MemoryOrchestrator

        return MemoryOrchestrator
    raise AttributeError(f"module 'opencortex' has no attribute {name!r}")
