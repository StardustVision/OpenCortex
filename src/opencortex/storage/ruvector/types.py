# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""
Adapter-specific types for the RuVector backend.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RuVectorConfig:
    """Configuration for RuVector backend."""

    data_dir: str = "./data/ruvector"
    dimension: int = 1024
    distance_metric: str = "cosine"  # cosine, euclidean, dotproduct
    cli_path: str = "rvf"
    cli_timeout: int = 30
    enable_sona: bool = True
    sona_decay_rate: float = 0.95
    sona_protected_decay_rate: float = 0.99
    sona_min_score: float = 0.01
    sona_learning_rate: float = 0.1
    sona_exploration_rate: float = 0.1
    # HTTP server mode (ruvector-server.js)
    server_host: str = "127.0.0.1"
    server_port: int = 6921
    use_http: bool = True  # True = HTTP client, False = CLI subprocess


@dataclass
class SonaProfile:
    """SONA behavior profile for a vector record."""

    id: str
    reward_score: float = 0.0
    retrieval_count: int = 0
    positive_feedback_count: int = 0
    negative_feedback_count: int = 0
    last_retrieved_at: float = 0.0
    last_feedback_at: float = 0.0
    effective_score: float = 0.0
    is_protected: bool = False


@dataclass
class DecayResult:
    """Result of decay operation."""

    records_processed: int = 0
    records_decayed: int = 0
    records_below_threshold: int = 0
    records_archived: int = 0
