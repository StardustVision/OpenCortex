# SPDX-License-Identifier: Apache-2.0
"""Settings helpers for the opencortex FastAPI application."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the opencortex FastAPI application.

    Environment variables use the ``OPENCORTEX_APP_`` prefix. For example:
    ``OPENCORTEX_APP_IDENTITY_CONTEXT_ENABLED=false`` disables the identity
    context middleware.
    """

    app_name: str = "OpenCortex App"
    app_description: str = "OpenCortex write-path APIs"
    app_version: str = "0.8.0"
    data_root: str = "./data"
    vector_dimension: int = Field(
        default=1024,
        description="Dense vector dimension used by the Qdrant vector store.",
    )
    qdrant_url: str = Field(
        default="",
        description="Qdrant service URL. Empty uses embedded local Qdrant.",
    )
    qdrant_api_key: str = Field(
        default="",
        description="Optional Qdrant service API key.",
    )
    qdrant_timeout: float = Field(
        default=30.0,
        description="Qdrant client request timeout in seconds.",
    )
    embedding_api_key: str = Field(
        default="",
        description="Required API key for write-path embedding.",
    )
    embedding_api_base: str = Field(
        default="https://api.openai.com/v1",
        description="OpenAI-compatible embedding API base URL.",
    )
    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="Model used for write-path embedding.",
    )
    llm_api_key: str = Field(
        default="",
        description="Required API key for write-path layer derivation.",
    )
    llm_api_base: str = Field(
        default="https://api.openai.com/v1",
        description="OpenAI-compatible or Anthropic-compatible API base URL.",
    )
    llm_model: str = Field(
        default="gpt-4o-mini",
        description="Model used for write-path layer derivation.",
    )
    llm_api_style: str = Field(
        default="openai",
        description="LLM API style: openai or anthropic.",
    )
    conversation_merge_token_budget: int = Field(
        default=6144,
        description="Approximate token budget that triggers session merge.",
    )
    session_idle_ttl: float = Field(
        default=1800.0,
        description="Seconds before idle session buffer state is pruned.",
    )
    immediate_event_ttl_hours: int = Field(
        default=24,
        description="TTL in hours for immediate session primary records.",
    )
    merged_event_ttl_hours: int = Field(
        default=168,
        description="TTL in hours for merged session primary records.",
    )
    store_event_worker_concurrency: int = Field(
        default=4,
        description="Number of persistent store-event worker consumers.",
    )
    identity_context_enabled: bool = Field(
        default=True,
        description="Enable header-derived identity context middleware.",
    )
    admin_api_token: str = Field(
        default="",
        description=(
            "Optional pre-generated admin JWT to register at startup. "
            "Use OPENCORTEX_APP_ADMIN_API_TOKEN or .env for local bootstrap."
        ),
    )
    retrieval_rerank_enabled: bool = Field(
        default=True,
        description="Enable rerank for medium and large recall queries.",
    )
    retrieval_rerank_provider: str = Field(
        default="llm",
        description="Recall rerank provider: llm, litellm, hosted_vllm, or openai.",
    )
    retrieval_rerank_model: str = Field(
        default="",
        description="Rerank model for provider=litellm. Empty uses llm_model.",
    )
    retrieval_rerank_api_base: str = Field(
        default="",
        description="Optional rerank API base for provider=litellm.",
    )
    retrieval_rerank_api_key: str = Field(
        default="",
        description="Optional rerank API key for provider=litellm.",
    )
    retrieval_rerank_seed_limit: int = Field(
        default=30,
        ge=1,
        le=100,
        description="Maximum fused candidates reranked before cone expansion.",
    )
    retrieval_rerank_final_limit: int = Field(
        default=30,
        ge=1,
        le=100,
        description="Maximum fused candidates reranked before returning results.",
    )

    model_config = SettingsConfigDict(
        env_prefix="OPENCORTEX_APP_",
        env_file=".env",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
