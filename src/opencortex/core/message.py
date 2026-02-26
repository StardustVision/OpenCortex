# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# Ported from OpenViking (https://github.com/volcengine/openviking)
# SPDX-License-Identifier: Apache-2.0
"""
Simple message representation for OpenCortex.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Message:
    """Simple message representation."""

    role: str  # "user", "assistant", "system"
    content: str
    name: Optional[str] = None
