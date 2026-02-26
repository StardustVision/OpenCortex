# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""
RuVector storage backend for OpenCortex.

Provides :class:`RuVectorAdapter` (dual-faced VikingDBInterface implementation
with SONA reinforcement capabilities), the subprocess CLI client, and the
adapter-specific type definitions.
"""

from opencortex.storage.ruvector.adapter import RuVectorAdapter
from opencortex.storage.ruvector.cli_client import RuVectorCLI
from opencortex.storage.ruvector.hooks_client import HooksStats, LearningResult, RuVectorHooks
from opencortex.storage.ruvector.http_client import RuVectorHTTPClient
from opencortex.storage.ruvector.types import DecayResult, RuVectorConfig, SonaProfile

__all__ = [
    "RuVectorAdapter",
    "RuVectorCLI",
    "RuVectorHTTPClient",
    "RuVectorHooks",
    "RuVectorConfig",
    "SonaProfile",
    "DecayResult",
    "LearningResult",
    "HooksStats",
]
