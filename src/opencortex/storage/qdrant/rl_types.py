# SPDX-License-Identifier: Apache-2.0
"""
Reinforcement learning data types for the Qdrant adapter.

These dataclasses match the attribute names expected by MemoryOrchestrator
(e.g. profile.reward_score, profile.positive_feedback_count).
"""

from dataclasses import dataclass


@dataclass
class Profile:
    id: str = ""
    reward_score: float = 0.0
    retrieval_count: int = 0
    positive_feedback_count: int = 0
    negative_feedback_count: int = 0
    effective_score: float = 0.0
    is_protected: bool = False
    accessed_at: str = ""


@dataclass
class DecayResult:
    records_processed: int = 0
    records_decayed: int = 0
    records_below_threshold: int = 0
    records_archived: int = 0
