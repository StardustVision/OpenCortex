# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# Ported from OpenViking (https://github.com/volcengine/openviking)
# SPDX-License-Identifier: Apache-2.0
"""
Rerank configuration for OpenCortex retrieval.
"""

from dataclasses import dataclass


@dataclass
class RerankConfig:
    """Rerank configuration for retrieval."""

    model: str = ""
    api_key: str = ""
    api_base: str = ""
    threshold: float = 0.0

    def is_available(self) -> bool:
        return bool(self.model and self.api_key)
