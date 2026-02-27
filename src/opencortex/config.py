# SPDX-License-Identifier: Apache-2.0
"""
OpenCortex configuration.

Provides global configuration for tenant, user, data paths, etc.
Configuration is loaded from a YAML/JSON file or set programmatically.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default config file search order
_CONFIG_FILE_NAMES = ["opencortex.json", ".opencortex.json"]

# Global default config directory and file
DEFAULT_CONFIG_DIR = Path.home() / ".opencortex"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "opencortex.json"


@dataclass
class CortexConfig:
    """Global configuration for OpenCortex.

    Attributes:
        tenant_id: Team/organization identifier. Default "default" for single-user.
        user_id: User identifier within the tenant. Default "default".
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

    tenant_id: str = "default"
    user_id: str = "default"
    data_root: str = "./data"
    embedding_dimension: int = 1024
    embedding_provider: str = ""
    embedding_model: str = ""
    embedding_api_key: str = ""
    embedding_api_base: str = ""
    # LLM completion (for IntentAnalyzer)
    llm_model: str = ""           # LLM model name for intent analysis
    llm_api_key: str = ""         # LLM API key (defaults to embedding_api_key if empty)
    llm_api_base: str = ""        # LLM API base URL
    # Rerank
    rerank_provider: str = ""       # "volcengine" | "jina" | "cohere" | "llm"
    rerank_model: str = ""          # Rerank model name
    rerank_api_key: str = ""        # API key (defaults to embedding_api_key)
    rerank_api_base: str = ""       # API endpoint
    rerank_threshold: float = 0.0   # Score threshold
    rerank_fusion_beta: float = 0.7 # Rerank vs retrieval score weight (0-1)
    # MCP server
    mcp_transport: str = "stdio"  # "stdio" | "sse" | "streamable-http"
    mcp_port: int = 8920
    mcp_mode: str = "remote"  # "remote" (thin client) | "local" (in-process orchestrator)
    # OpenCortex HTTP Server (FastAPI)
    http_server_host: str = "127.0.0.1"
    http_server_port: int = 8921

    def tenant_prefix(self) -> str:
        """Return the tenant URI prefix: opencortex://tenant/{tenant_id}"""
        return f"opencortex://tenant/{self.tenant_id}"

    def user_prefix(self) -> str:
        """Return the user URI prefix: opencortex://tenant/{tenant_id}/user/{user_id}"""
        return f"opencortex://tenant/{self.tenant_id}/user/{self.user_id}"

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: Optional[str] = None) -> None:
        """Save config to JSON file.

        If no path is given, saves to $HOME/.opencortex/opencortex.json
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

        Args:
            path: Explicit config file path. If None, searches for default files.

        Returns:
            CortexConfig instance (defaults if no file found).
        """
        if path:
            return cls._load_from_file(path)

        # Search for config file in CWD
        for name in _CONFIG_FILE_NAMES:
            if os.path.exists(name):
                return cls._load_from_file(name)

        # Fallback to global default: $HOME/.opencortex/opencortex.json
        if DEFAULT_CONFIG_PATH.exists():
            return cls._load_from_file(str(DEFAULT_CONFIG_PATH))

        # No config file found, return defaults
        return cls()

    @classmethod
    def _load_from_file(cls, path: str) -> "CortexConfig":
        """Load config from a specific file."""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Only use known fields
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        config = cls(**filtered)
        logger.info(f"[CortexConfig] Loaded from {path} (tenant={config.tenant_id}, user={config.user_id})")
        return config

    @classmethod
    def ensure_default_config(cls) -> Path:
        """Create default config at $HOME/.opencortex/opencortex.json if it doesn't exist.

        Returns:
            Path to the default config file.
        """
        if not DEFAULT_CONFIG_PATH.exists():
            DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            default = cls()
            default.save(str(DEFAULT_CONFIG_PATH))
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
