# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# Ported from OpenViking (https://github.com/volcengine/openviking)
# SPDX-License-Identifier: Apache-2.0
"""
OpenCortex: Memory and context management for AI agents.

Ported and adapted from OpenViking with tenant-based multi-user isolation.
"""

__version__ = "0.1.0"

from opencortex.config import CortexConfig, get_config, init_config
from opencortex.orchestrator import MemoryOrchestrator
