# SPDX-License-Identifier: Apache-2.0
"""OpenAI-compatible embedding client for opencortex_app vector writes."""

from __future__ import annotations

import os
from typing import Any

import httpx
from pydantic import BaseModel, Field, model_validator


class EmbeddingConfig(BaseModel):
    """Configuration for the required write-path embedding model."""

    api_key: str = ""
    api_base: str = "https://api.openai.com/v1"
    model: str = "text-embedding-3-small"
    dimension: int = Field(default=1024, gt=0)
    timeout_seconds: float = 60.0

    @model_validator(mode="after")
    def validate_required_embedding(self) -> "EmbeddingConfig":
        """Require complete embedding configuration."""
        if not self.api_key.strip():
            raise ValueError("Embedding api key is required")
        if not self.api_base.strip():
            raise ValueError("Embedding api base is required")
        if not self.model.strip():
            raise ValueError("Embedding model is required")
        return self


class EmbeddingResult(BaseModel):
    """Embedding output accepted by vector writers."""

    dense_vector: list[float]
    sparse_vector: dict[str, float] | None = None


class OpenAIEmbeddingClient:
    """Synchronous OpenAI-compatible embedding client."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self.client = httpx.Client(timeout=config.timeout_seconds)

    def embed(self, text: str) -> EmbeddingResult:
        """Embed one text."""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]:
        """Embed multiple texts in one request."""
        if not texts:
            return []
        response = self.client.post(
            f"{self.config.api_base.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            json={"model": self.config.model, "input": texts},
        )
        response.raise_for_status()
        return [
            self.embedding_result(item)
            for item in sorted(response.json()["data"], key=lambda item: item["index"])
        ]

    def close(self) -> None:
        """Close the HTTP client."""
        self.client.close()

    def embedding_result(self, item: dict[str, Any]) -> EmbeddingResult:
        """Validate one embedding item from the API response."""
        vector = [float(value) for value in item["embedding"]]
        if len(vector) != self.config.dimension:
            raise ValueError(
                "Embedding dimension mismatch: "
                f"expected {self.config.dimension}, got {len(vector)}"
            )
        return EmbeddingResult(dense_vector=vector)


def embedding_api_key_from_env() -> str:
    """Return embedding API key from supported environment variables."""
    return os.environ.get("OPENCORTEX_APP_EMBEDDING_API_KEY", "") or os.environ.get(
        "OPENAI_API_KEY",
        "",
    )
