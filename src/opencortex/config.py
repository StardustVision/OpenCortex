# SPDX-License-Identifier: Apache-2.0
"""
OpenCortex configuration.

Provides global configuration for tenant, user, data paths, etc.
Configuration is loaded from a JSON file or set programmatically.

Search order:
  CWD/server.json → CWD/opencortex.json → CWD/.opencortex.json
  → $HOME/.opencortex/server.json → $HOME/.opencortex/opencortex.json

Environment variables (OPENCORTEX_*) override file values.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Default config file search order (server.json first)
_CONFIG_FILE_NAMES = ["server.json", "opencortex.json", ".opencortex.json"]

# Global default config directory and file
DEFAULT_CONFIG_DIR = Path.home() / ".opencortex"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "server.json"

# Legacy config path (for migration)
_LEGACY_CONFIG_PATH = DEFAULT_CONFIG_DIR / "opencortex.json"

# Fields that belong to MCP config (excluded from server.json)
_MCP_ONLY_FIELDS = {"mcp_transport", "mcp_port", "mcp_mode"}


@dataclass
class CortexAlphaConfig:
    """Cortex Alpha sub-configuration (Design doc §11)."""
    # Observer
    observer_enabled: bool = True
    # Trace Splitter
    trace_splitter_enabled: bool = False    # was True; Phase 1 shrinkage
    trace_splitter_max_context_tokens: int = 128000
    # Archivist
    archivist_enabled: bool = False         # was True; Phase 1 shrinkage
    archivist_trigger_mode: str = "auto"        # "auto" | "manual"
    archivist_trigger_threshold: int = 20       # traces per trigger
    archivist_max_delay_hours: int = 24
    archivist_llm_model: str = ""               # defaults to main llm_model
    # Sandbox
    sandbox_min_traces: int = 3
    sandbox_min_success_rate: float = 0.7
    sandbox_min_source_users: int = 2
    sandbox_min_source_users_private: int = 1
    sandbox_llm_sample_size: int = 5
    sandbox_llm_min_pass_rate: float = 0.6
    sandbox_require_human_approval: bool = True
    # Knowledge Store
    knowledge_collection_name: str = "knowledge"
    trace_collection_name: str = "traces"
    # User scope auto-approval
    user_auto_approve_confidence: float = 0.95
    # Knowledge recall in prepare()
    knowledge_recall_enabled: bool = False  # Server-side default for include_knowledge


@dataclass
class CortexConfig:
    """Global configuration for OpenCortex.

    Tenant and user identity are determined per-request from the JWT
    Bearer token claims (tid/uid), not from server-side configuration.

    Attributes:
        data_root: Root directory for local data storage.
        embedding_dimension: Default vector dimension for embeddings.
        embedding_provider: Embedding provider name (e.g., "openai", "jina").
        embedding_model: Embedding model name.
        embedding_api_key: API key for embedding provider.
        embedding_api_base: Custom API base URL for embedding provider.
        llm_model: LLM model name for intent analysis.
        llm_api_key: LLM API key (defaults to embedding_api_key if empty).
        llm_api_base: LLM API base URL.
    """

    data_root: str = "./data"
    embedding_dimension: int = 1024
    embedding_provider: str = "local"
    embedding_model: str = ""
    embedding_api_key: str = ""
    embedding_api_base: str = ""
    # LLM completion (for IntentAnalyzer)
    llm_model: str = ""           # LLM model name for intent analysis
    llm_api_key: str = ""         # LLM API key (defaults to embedding_api_key if empty)
    llm_api_base: str = ""        # LLM API base URL
    llm_api_format: str = "openai"  # "openai" | "anthropic"
    # Rerank
    rerank_provider: str = "local"  # "jina" | "cohere" | "local" | "llm"
    rerank_model: str = ""          # Rerank model name
    rerank_api_key: str = ""        # API key (defaults to embedding_api_key)
    rerank_api_base: str = ""       # API endpoint
    rerank_threshold: float = 0.0   # Score threshold
    rerank_fusion_beta: float = 0.7 # Rerank vs retrieval score weight (0-1)
    rerank_max_candidates: int = 20 # Max docs sent to reranker per query (cost control)
    rerank_flat_pool_multiplier: int = 3  # Flat-search rerank candidate multiplier (pool = limit * N)
    # Search behavior
    force_flat_search: bool = False  # Skip frontier/recursive, always use flat vector search
    # HyDE (Hypothetical Document Embedding)
    hyde_enabled: bool = False  # Generate hypothetical answer for dense embedding
    # OpenCortex HTTP Server (FastAPI)
    http_server_host: str = "127.0.0.1"
    http_server_port: int = 8921
    # Event retention
    immediate_event_ttl_hours: int = 24
    merged_event_ttl_hours: int = 168
    # Cortex Alpha
    cortex_alpha: CortexAlphaConfig = field(default_factory=CortexAlphaConfig)

    # --- v0.6 Feature Flags ---
    query_classifier_enabled: bool = True
    query_classifier_classes: Dict[str, str] = field(default_factory=lambda: {
        "document_scoped": "查找特定文档、论文、文件中的内容",
        "temporal_lookup": "查找最近、上次、昨天等时间相关的记忆",
        "fact_lookup": "查找特定人名、数字、术语、文件名等精确事实",
        "simple_recall": "简单的记忆召回，回忆之前存储的信息",
    })
    query_classifier_threshold: float = 0.3
    query_classifier_hybrid_weights: Dict[str, Dict[str, float]] = field(default_factory=lambda: {
        "document_scoped": {"dense": 0.5, "lexical": 0.5},
        "fact_lookup": {"dense": 0.3, "lexical": 0.7},
        "temporal_lookup": {"dense": 0.6, "lexical": 0.4},
        "simple_recall": {"dense": 0.7, "lexical": 0.3},
        "complex": {"dense": 0.7, "lexical": 0.3},
    })
    doc_scope_search_enabled: bool = True
    small_to_big_enabled: bool = True
    small_to_big_sibling_count: int = 2
    context_flattening_enabled: bool = True
    time_filter_enabled: bool = True
    time_filter_fallback_threshold: int = 3
    rerank_gate_score_gap_threshold: float = 0.15
    rerank_gate_doc_scope_skip_threshold: int = 5
    max_compensation_queries: int = 3
    max_total_search_calls: int = 12
    explain_enabled: bool = True
    onnx_intra_op_threads: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: Optional[str] = None) -> None:
        """Save config to JSON file.

        If no path is given, saves to $HOME/.opencortex/server.json
        (creates the directory if needed).
        """
        if path:
            save_path = Path(path)
        else:
            save_path = DEFAULT_CONFIG_PATH
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        logger.info(f"[CortexConfig] Saved to {save_path}")

    @classmethod
    def load(cls, path: Optional[str] = None) -> "CortexConfig":
        """Load config from JSON file.

        Search order:
          1. Explicit path (if given)
          2. CWD: server.json → opencortex.json → .opencortex.json
          3. $HOME/.opencortex/server.json
          4. $HOME/.opencortex/opencortex.json (legacy)
          5. Defaults

        Environment variables (OPENCORTEX_*) override file values.

        Returns:
            CortexConfig instance (defaults if no file found).
        """
        if path:
            return cls._load_from_file(path)

        # Search for config file in CWD (project-local overrides global)
        for name in _CONFIG_FILE_NAMES:
            if Path(name).exists():
                return cls._load_from_file(name)

        # $HOME/.opencortex/server.json
        if DEFAULT_CONFIG_PATH.exists():
            return cls._load_from_file(str(DEFAULT_CONFIG_PATH))

        # Legacy fallback: $HOME/.opencortex/opencortex.json
        if _LEGACY_CONFIG_PATH.exists():
            return cls._load_from_file(str(_LEGACY_CONFIG_PATH))

        # No config file found, apply env overrides to defaults
        config = cls()
        config._apply_env_overrides()
        return config

    @classmethod
    def _load_from_file(cls, path: str) -> "CortexConfig":
        """Load config from a specific file."""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Only use known fields
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        # Handle nested dataclass fields
        if "cortex_alpha" in filtered and isinstance(filtered["cortex_alpha"], dict):
            filtered["cortex_alpha"] = CortexAlphaConfig(**filtered["cortex_alpha"])
        config = cls(**filtered)
        config._apply_env_overrides()
        logger.info(f"[CortexConfig] Loaded from {path}")
        return config

    def _apply_env_overrides(self) -> None:
        """Override config fields from OPENCORTEX_* environment variables.

        Supports nested dataclass fields via JSON strings, e.g.:
          OPENCORTEX_CORTEX_ALPHA='{"trace_splitter_enabled":true,"archivist_enabled":true}'
        """
        for f in fields(self):
            env_key = f"OPENCORTEX_{f.name.upper()}"
            env_val = os.environ.get(env_key)
            if env_val is not None:
                # Type conversion based on field type
                if f.type is int:
                    setattr(self, f.name, int(env_val))
                elif f.type is float:
                    setattr(self, f.name, float(env_val))
                elif f.type is bool:
                    setattr(self, f.name, env_val.lower() in ("1", "true", "yes"))
                elif f.name == "cortex_alpha":
                    import json
                    try:
                        data = json.loads(env_val)
                        if isinstance(data, dict):
                            # Merge with existing defaults
                            current = getattr(self, f.name)
                            for k, v in data.items():
                                if hasattr(current, k):
                                    setattr(current, k, v)
                    except (json.JSONDecodeError, TypeError) as exc:
                        logger.warning("[CortexConfig] Failed to parse %s: %s", env_key, exc)
                else:
                    setattr(self, f.name, env_val)

    @classmethod
    def ensure_default_config(cls) -> Path:
        """Create default config at $HOME/.opencortex/server.json if it doesn't exist.

        If a legacy opencortex.json exists, extracts server fields into server.json.

        Returns:
            Path to the default config file.
        """
        if DEFAULT_CONFIG_PATH.exists():
            return DEFAULT_CONFIG_PATH

        DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        if _LEGACY_CONFIG_PATH.exists():
            # Migrate: extract server fields from legacy config
            try:
                with open(_LEGACY_CONFIG_PATH, "r", encoding="utf-8") as f:
                    legacy_data = json.load(f)
                known_fields = {f.name for f in cls.__dataclass_fields__.values()}
                filtered = {k: v for k, v in legacy_data.items()
                            if k in known_fields and k not in _MCP_ONLY_FIELDS}
                config = cls(**filtered)
            except Exception:
                config = cls()
        else:
            config = cls()

        config.save(str(DEFAULT_CONFIG_PATH))
        logger.info(f"[CortexConfig] Created default config at {DEFAULT_CONFIG_PATH}")
        return DEFAULT_CONFIG_PATH


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------

_instance: Optional[CortexConfig] = None


def init_config(
    config: Optional[CortexConfig] = None,
    path: Optional[str] = None,
) -> CortexConfig:
    """Initialize the global CortexConfig singleton.

    Args:
        config: Provide a config directly, or
        path: Load from a specific file path.
        If neither is given, auto-discovers config file or uses defaults.
    """
    global _instance
    if config:
        _instance = config
    else:
        _instance = CortexConfig.load(path)
    return _instance


def get_config() -> CortexConfig:
    """Get the global CortexConfig singleton. Auto-initializes if needed."""
    global _instance
    if _instance is None:
        _instance = CortexConfig.load()
    return _instance
