# SPDX-License-Identifier: Apache-2.0
"""Standalone dependency container for opencortex_app."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel

from opencortex_app.llm.client import (
    LLMCompletion,
    LLMConfig,
    llm_api_key_from_env,
)
from opencortex_app.storage.cfs_queue import CFSQueue
from opencortex_app.storage.cortex_storage import CortexStorage
from opencortex_app.store.event.events import MemoryEventManager
from opencortex_app.vector.embedder import (
    EmbeddingConfig,
    OpenAIEmbeddingClient,
    embedding_api_key_from_env,
)
from opencortex_app.vector.qdrant_store import QdrantVectorStore


class AppRuntimeConfig(BaseModel):
    """Runtime config consumed by store flows."""

    data_root: str = "./data"
    vector_dimension: int = 1024
    embedding_api_key: str = ""
    embedding_api_base: str = "https://api.openai.com/v1"
    embedding_model: str = "text-embedding-3-small"
    llm_api_key: str = ""
    llm_api_base: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_api_style: str = "openai"
    conversation_merge_token_budget: int = 6144
    session_idle_ttl: float = 1800.0
    immediate_event_ttl_hours: int = 24
    merged_event_ttl_hours: int = 168


class AppRuntime:
    """Own standalone app dependencies."""

    def __init__(self, config: AppRuntimeConfig | None = None) -> None:
        self.config = config or AppRuntimeConfig()
        self.vector_store = QdrantVectorStore(
            path=str(Path(self.config.data_root) / "qdrant"),
            vector_size=self.config.vector_dimension,
        )
        self.embedder = OpenAIEmbeddingClient(
            EmbeddingConfig(
                api_key=(
                    self.config.embedding_api_key
                    or embedding_api_key_from_env()
                    or self.config.llm_api_key
                    or llm_api_key_from_env()
                ),
                api_base=self.config.embedding_api_base,
                model=self.config.embedding_model,
                dimension=self.config.vector_dimension,
            )
        )
        self.llm_completion = LLMCompletion(
            LLMConfig(
                api_key=self.config.llm_api_key or llm_api_key_from_env(),
                api_base=self.config.llm_api_base,
                model=self.config.llm_model,
                api_style=self.config.llm_api_style,
            )
        )
        self.memory_events = MemoryEventManager()
        self.cortex_storage = CortexStorage(data_root=self.config.data_root)
        self.store_event_queue = CFSQueue(cfs=self.cortex_storage.cfs)

    async def init(self) -> "AppRuntime":
        """Initialize runtime resources."""
        return self

    async def close(self) -> None:
        """Close runtime resources."""
        await self.vector_store.close()
        await self.llm_completion.close()
        self.embedder.close()

    def get_collection(self) -> str:
        """Return the default collection."""
        return "context"

    def ttl_from_hours(self, hours: int) -> str:
        """Return an ISO timestamp hours from now."""
        if not hours:
            return ""
        return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
