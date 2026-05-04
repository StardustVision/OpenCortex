# SPDX-License-Identifier: Apache-2.0
"""Record write and persistence helpers for session recomposition."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING, Any, Dict, List

from opencortex.services.memory_filters import FilterExpr

if TYPE_CHECKING:
    from opencortex.context.manager import ContextManager, SessionKey

logger = logging.getLogger(__name__)


class RecompositionWriteService:
    """Own recomposition record persistence and follow-up derive scheduling."""

    def __init__(self, manager: "ContextManager") -> None:
        """Create a write service bound to one context manager."""
        self._manager = manager

    @staticmethod
    def merged_leaf_uri(
        tenant_id: str,
        user_id: str,
        session_id: str,
        msg_range: List[int],
    ) -> str:
        """Return one stable merged-leaf URI for a session message span."""
        from opencortex.utils.uri import CortexURI

        start = int(msg_range[0])
        end = int(msg_range[1])
        session_hash = hashlib.md5(session_id.encode("utf-8")).hexdigest()[:12]
        node_name = f"conversation-{session_hash}-{start:06d}-{end:06d}"
        return CortexURI.build_private(
            tenant_id,
            user_id,
            "memories",
            "events",
            node_name,
        )

    @staticmethod
    def directory_uri(
        tenant_id: str,
        user_id: str,
        session_id: str,
        index: int,
    ) -> str:
        """Return URI for a directory parent record."""
        from opencortex.utils.uri import CortexURI

        session_hash = hashlib.md5(session_id.encode("utf-8")).hexdigest()[:12]
        node_name = f"conversation-{session_hash}/dir-{index:03d}"
        return CortexURI.build_private(
            tenant_id,
            user_id,
            "memories",
            "events",
            node_name,
        )

    async def write_directory_record(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: str,
        directory_index: int,
        segment: Dict[str, Any],
        children_abstracts: List[str],
        derived: Dict[str, Any],
        aggregated_meta: Dict[str, Any],
        all_tool_calls: List[Dict[str, Any]],
    ) -> str:
        """Persist one derived directory record and return its URI."""
        keywords_list = derived.get("keywords", [])
        keywords_str = self._keywords_string(keywords_list)
        dir_uri = self.directory_uri(
            tenant_id,
            user_id,
            session_id,
            directory_index,
        )
        source_records = segment.get("source_records", [])
        content = "\n\n".join(children_abstracts)
        llm_abstract = derived.get("abstract", "")
        llm_overview = derived.get("overview", "")

        await self._manager._orchestrator.add(
            uri=dir_uri,
            abstract=llm_abstract,
            content=content,
            category="events",
            context_type="memory",
            is_leaf=False,
            session_id=session_id,
            meta={
                **aggregated_meta,
                "layer": "directory",
                "ingest_mode": "memory",
                "msg_range": list(segment["msg_range"]),
                "source_uri": source_uri or "",
                "session_id": session_id,
                "child_count": len(source_records),
                "child_uris": [str(r.get("uri", "")) for r in source_records],
                "tool_calls": all_tool_calls if all_tool_calls else [],
            },
            overview=llm_overview,
        )

        await self._patch_keywords(dir_uri, keywords_str)
        await self._write_fs_context(
            uri=dir_uri,
            content=content,
            abstract=llm_abstract,
            abstract_json={
                "keywords": keywords_list,
                "child_count": len(source_records),
            },
            overview=llm_overview,
            is_leaf=False,
        )
        return dir_uri

    async def write_session_summary(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: str,
        abstracts: List[str],
        llm_abstract: str,
        llm_overview: str,
        keywords_list: Any,
    ) -> str:
        """Persist the session summary record and return its URI."""
        summary_uri = self._manager._session_summary_uri(
            tenant_id,
            user_id,
            session_id,
        )
        keywords_str = self._keywords_string(keywords_list)
        content = "\n\n".join(abstracts)

        await self._manager._orchestrator.add(
            uri=summary_uri,
            abstract=llm_abstract,
            content=content,
            category="events",
            context_type="memory",
            is_leaf=False,
            session_id=session_id,
            meta={
                "layer": "session_summary",
                "session_id": session_id,
                "source_uri": source_uri or "",
                "child_count": len(abstracts),
                "topics": keywords_list,
            },
            overview=llm_overview,
        )

        await self._patch_keywords(summary_uri, keywords_str)
        await self._write_fs_context(
            uri=summary_uri,
            content=content,
            abstract=llm_abstract,
            abstract_json={
                "keywords": keywords_list,
                "child_count": len(abstracts),
            },
            overview=llm_overview,
            is_leaf=False,
        )
        return summary_uri

    async def write_merged_leaf(
        self,
        *,
        sk: "SessionKey",
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: str,
        msg_range: List[int],
        content: str,
        aggregated_meta: Dict[str, Any],
        all_tool_calls: List[Dict[str, Any]],
    ) -> str:
        """Persist one merged leaf and schedule its deferred derive."""
        leaf_meta = {
            **aggregated_meta,
            "layer": "merged",
            "ingest_mode": "memory",
            "msg_range": list(msg_range),
            "source_uri": source_uri,
            "session_id": session_id,
            "recomposition_stage": "online_tail",
            "tool_calls": all_tool_calls if all_tool_calls else [],
        }
        merged_context = await self._manager._orchestrator.add(
            uri=self.merged_leaf_uri(
                tenant_id,
                user_id,
                session_id,
                msg_range,
            ),
            abstract="",
            content=content,
            category="events",
            context_type="memory",
            meta=leaf_meta,
            session_id=session_id,
            defer_derive=True,
        )
        self._schedule_deferred_derive(
            sk=sk,
            uri=merged_context.uri,
            content=content,
            session_id=session_id,
            meta=aggregated_meta,
        )
        return str(merged_context.uri)

    @staticmethod
    def _keywords_string(keywords_list: Any) -> str:
        """Serialize derived keywords into the existing record field format."""
        if not isinstance(keywords_list, list):
            return ""
        return ", ".join(str(k) for k in keywords_list if k)

    async def _patch_keywords(self, uri: str, keywords_str: str) -> None:
        """Patch keywords into the Qdrant record after ``add()``."""
        if not keywords_str:
            return
        orchestrator = self._manager._orchestrator
        try:
            records = await orchestrator._storage.filter(
                orchestrator._get_collection(),
                FilterExpr.eq("uri", uri).to_dict(),
                limit=1,
            )
            if records:
                await orchestrator._storage.update(
                    orchestrator._get_collection(),
                    str(records[0].get("id", "")),
                    {"keywords": keywords_str},
                )
        except Exception:
            logger.warning("[ContextManager] Failed to patch keywords for %s", uri)

    async def _write_fs_context(
        self,
        *,
        uri: str,
        content: str,
        abstract: str,
        abstract_json: Dict[str, Any],
        overview: str,
        is_leaf: bool,
    ) -> None:
        """Write the paired CortexFS context when CortexFS is enabled."""
        fs = getattr(self._manager._orchestrator, "_fs", None)
        if fs is None:
            return
        await fs.write_context(
            uri=uri,
            content=content,
            abstract=abstract,
            abstract_json=abstract_json,
            overview=overview,
            is_leaf=is_leaf,
        )

    def _schedule_deferred_derive(
        self,
        *,
        sk: "SessionKey",
        uri: str,
        content: str,
        session_id: str,
        meta: Dict[str, Any],
    ) -> None:
        """Schedule and track deferred derive for one merged leaf."""

        async def _bounded_derive(
            sem: asyncio.Semaphore = self._manager._derive_semaphore,
            **dkw: Any,
        ) -> None:
            async with sem:
                await self._manager._orchestrator._complete_deferred_derive(**dkw)

        defer_task = asyncio.create_task(
            _bounded_derive(
                uri=uri,
                content=content,
                abstract="",
                overview="",
                session_id=session_id,
                meta=meta,
                raise_on_error=True,
            )
        )
        self._manager._recomposition_tasks.track_session_merge_followup_task(
            sk,
            defer_task,
        )
        defer_task.add_done_callback(
            lambda task: (
                None
                if task.cancelled()
                else (
                    logger.warning(
                        "[ContextManager] deferred derive failed: %s",
                        task.exception(),
                    )
                    if task.exception()
                    else None
                )
            )
        )
