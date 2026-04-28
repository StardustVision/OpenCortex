# SPDX-License-Identifier: Apache-2.0
"""Parent directory record persistence for memory writes."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, List

from opencortex.core.context import Context
from opencortex.core.user_id import UserIdentifier
from opencortex.http.request_context import get_effective_identity
from opencortex.utils.uri import CortexURI

if TYPE_CHECKING:
    from opencortex.services.memory_write_service import MemoryWriteService

logger = logging.getLogger(__name__)


class MemoryDirectoryRecordService:
    """Owns vector-store parent directory records for memory writes."""

    def __init__(self, write_service: "MemoryWriteService") -> None:
        """Bind the directory service to a write service facade."""
        self._write_service = write_service

    @property
    def _orch(self) -> Any:
        return self._write_service._orch

    async def ensure_parent_records(self, parent_uri: str) -> None:
        """Ensure all ancestor directory records exist in the vector store."""
        to_create = await self._collect_missing_ancestors(parent_uri)
        if not to_create:
            return

        tenant_id, user_id = get_effective_identity()
        effective_user = UserIdentifier(tenant_id, user_id)

        for dir_uri in reversed(to_create):
            await self._create_directory_record(
                dir_uri=dir_uri,
                tenant_id=tenant_id,
                user_id=user_id,
                effective_user=effective_user,
            )

    async def _collect_missing_ancestors(self, parent_uri: str) -> List[str]:
        """Walk upward and return directory URIs missing from storage."""
        orch = self._orch
        uri = parent_uri
        to_create: List[str] = []

        while uri:
            try:
                parsed = CortexURI(uri)
            except ValueError:
                break

            existing = await orch._storage.filter(
                orch._get_collection(),
                {"op": "must", "field": "uri", "conds": [uri]},
                limit=1,
            )
            if existing:
                break

            to_create.append(uri)
            parent = parsed.parent
            if parent is None:
                break
            uri = str(parent)

        return to_create

    async def _create_directory_record(
        self,
        *,
        dir_uri: str,
        tenant_id: str,
        user_id: str,
        effective_user: UserIdentifier,
    ) -> None:
        """Build and upsert one directory record."""
        orch = self._orch
        dir_ctx = Context(
            uri=dir_uri,
            parent_uri=orch._derive_parent_uri(dir_uri),
            is_leaf=False,
            abstract="",
            user=effective_user,
        )
        sparse_vector = await self._embed_directory_name(dir_ctx=dir_ctx, uri=dir_uri)

        record = dir_ctx.to_dict()
        if dir_ctx.vector:
            record["vector"] = dir_ctx.vector
        if sparse_vector:
            record["sparse_vector"] = sparse_vector
        record["scope"] = "private" if CortexURI(dir_uri).is_private else "shared"
        record["source_user_id"] = user_id
        record["source_tenant_id"] = tenant_id
        record["category"] = ""
        record["mergeable"] = False
        record["session_id"] = ""
        record["ttl_expires_at"] = ""
        await orch._storage.upsert(orch._get_collection(), record)
        logger.debug("[MemoryService] Created directory record: %s", dir_uri)

    async def _embed_directory_name(self, *, dir_ctx: Context, uri: str) -> Any:
        """Embed the directory basename and attach its dense vector."""
        embedder = self._orch._embedder
        dir_name = uri.rstrip("/").rsplit("/", 1)[-1]
        if not embedder or not dir_name:
            return None

        loop = asyncio.get_running_loop()
        embed_result = await loop.run_in_executor(None, embedder.embed, dir_name)
        dir_ctx.vector = embed_result.dense_vector
        return embed_result.sparse_vector if embed_result.sparse_vector else None
