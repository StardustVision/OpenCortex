# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# Ported from OpenViking (https://github.com/volcengine/openviking)
# SPDX-License-Identifier: Apache-2.0
"""
Rerank configuration for OpenCortex retrieval.
"""

from dataclasses import dataclass


@dataclass
class RerankConfig:
    """Rerank configuration for retrieval.

    Supports three modes (in priority order):
    1. API mode — dedicated Rerank API (Volcengine/Jina/Cohere)
    2. LLM mode — use LLM completion as listwise reranker (fallback)
    3. Disabled — no rerank, pure embedding + retrieval scores
    """

    model: str = ""
    api_key: str = ""
    api_base: str = ""
    threshold: float = 0.0
    provider: str = ""  # "volcengine" | "jina" | "cohere" | "llm"
    fusion_beta: float = 0.7  # rerank vs retrieval score weight (0-1)
    max_candidates: int = 5   # max docs to send for rerank (cost control)
    use_llm_fallback: bool = True  # fallback to LLM when no API
    score_gap_threshold: float = 0.15  # skip rerank if top1-top2 gap > this

    def is_available(self) -> bool:
        """Return True if reranking can be performed (API or LLM fallback)."""
        return bool(self.model and self.api_key) or self.use_llm_fallback
