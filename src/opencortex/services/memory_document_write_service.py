# SPDX-License-Identifier: Apache-2.0
"""Document and batch memory write service for OpenCortex."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from uuid import uuid4

from opencortex.core.context import Context
from opencortex.http.request_context import get_effective_identity
from opencortex.prompts import build_doc_summarization_prompt
from opencortex.utils.json_parse import parse_json_from_response
from opencortex.utils.text import chunked_llm_derive, smart_truncate

if TYPE_CHECKING:
    from opencortex.services.memory_service import MemoryService

logger = logging.getLogger(__name__)


class MemoryDocumentWriteService:
    """Own document ingest and batch write logic behind MemoryService."""

    def __init__(self, memory_service: "MemoryService") -> None:
        self._service = memory_service

    @property
    def _orch(self) -> Any:
        return self._service._orch

    async def _generate_abstract_overview(
        self,
        content: str,
        file_path: str,
    ) -> tuple[str, str]:
        """Use LLM to generate abstract (L0) and overview (L1) from content."""
        orch = self._orch
        fallback_overview = smart_truncate(content, 500)

        if not orch._llm_completion:
            return file_path, fallback_overview

        if len(content) > 3000:
            try:
                result = await chunked_llm_derive(
                    content=content,
                    prompt_builder=lambda chunk: build_doc_summarization_prompt(
                        file_path, chunk
                    ),
                    llm_fn=orch._llm_completion,
                    parse_fn=parse_json_from_response,
                    merge_policy="abstract_overview",
                    max_chars_per_chunk=3000,
                )
                return result.get("abstract", file_path), result.get(
                    "overview", fallback_overview
                )
            except Exception:
                pass
            return file_path, fallback_overview

        prompt = build_doc_summarization_prompt(file_path, content)
        try:
            response = await orch._llm_completion(prompt)
            data = parse_json_from_response(response)
            if isinstance(data, dict):
                return data.get("abstract", file_path), data.get(
                    "overview", fallback_overview
                )
        except Exception:
            pass

        return file_path, fallback_overview

    async def _add_document(
        self,
        content: str,
        abstract: str,
        overview: str,
        category: str,
        parent_uri: Optional[str],
        context_type: str,
        meta: Optional[Dict[str, Any]],
        session_id: Optional[str],
        source_path: str,
    ) -> Context:
        """Parse document content and enqueue or write derived chunks."""
        orch = self._orch
        if orch._parser_registry is None:
            from opencortex.parse.registry import ParserRegistry

            orch._parser_registry = ParserRegistry()
        registry = orch._parser_registry
        parser = registry.get_parser_for_file(source_path) if source_path else None

        if parser:
            chunks = await parser.parse_content(content, source_path=source_path)
        else:
            chunks = await registry.parse_content(content, source_format="markdown")

        # --- v0.6: Generate source_doc_id for document scoped search ---
        _effective_source_path = (
            source_path
            or (meta or {}).get("source_path", "")
            or (meta or {}).get("file_path", "")
        )
        if _effective_source_path:
            source_doc_id = hashlib.sha256(_effective_source_path.encode()).hexdigest()[
                :16
            ]
        else:
            source_doc_id = uuid4().hex[:16]
        source_doc_title = (meta or {}).get("title", "")
        if not source_doc_title and _effective_source_path:
            source_doc_title = os.path.basename(_effective_source_path)

        # Single chunk or no chunks -> fall through to memory mode
        if len(chunks) <= 1:
            single_content = chunks[0].content if chunks else content
            embed_text = ""
            if orch._config.context_flattening_enabled:
                parts = []
                if source_doc_title:
                    parts.append(f"[{source_doc_title}]")
                sp = chunks[0].meta.get("section_path", "") if chunks else ""
                if sp:
                    parts.append(f"[{sp}]")
                parts.append(abstract)
                embed_text = " ".join(parts)
            return await self._service.add(
                abstract=abstract,
                content=single_content,
                category=category,
                parent_uri=parent_uri,
                context_type=context_type,
                meta={
                    **(meta or {}),
                    "ingest_mode": "memory",
                    "source_doc_id": source_doc_id,
                    "source_doc_title": source_doc_title,
                    "source_section_path": chunks[0].meta.get("section_path", "")
                    if chunks
                    else "",
                    "chunk_role": "document",
                },
                session_id=session_id,
                embed_text=embed_text,
            )

        # Multi-chunk: async derive -- return immediately, process in background
        doc_title = (
            Path(source_path).stem
            if source_path
            else abstract
            if abstract
            else "Document"
        )

        # Phase A: generate URI, write CortexFS, enqueue, return
        import json as _json

        parent_uri_candidate = orch._auto_uri(
            context_type or "resource", category, abstract=doc_title
        )
        parent_uri_candidate = await orch._resolve_unique_uri(parent_uri_candidate)
        while parent_uri_candidate in orch._inflight_derive_uris:
            parent_uri_candidate = await orch._resolve_unique_uri(
                parent_uri_candidate + "_"
            )
        orch._inflight_derive_uris.add(parent_uri_candidate)

        tid, uid = get_effective_identity()

        # Write .derive_pending marker first (recovery signal)
        marker_data = _json.dumps(
            {
                "parent_uri": parent_uri_candidate,
                "category": category,
                "context_type": context_type or "resource",
                "source_path": source_path or "",
                "source_doc_id": source_doc_id,
                "source_doc_title": source_doc_title,
                "meta": meta or {},
                "tenant_id": tid,
                "user_id": uid,
            }
        ).encode("utf-8")
        fs_path = orch._fs._uri_to_path(parent_uri_candidate)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: (
                orch._fs.agfs.mkdir(fs_path),
                orch._fs.agfs.write(f"{fs_path}/.derive_pending", marker_data),
            ),
        )

        # Write L2 content to CortexFS
        await orch._fs.write_context(uri=parent_uri_candidate, content=content)

        # Enqueue derive task
        from opencortex.services.derivation_service import DeriveTask

        task = DeriveTask(
            parent_uri=parent_uri_candidate,
            content=content,
            abstract=doc_title,
            chunks=chunks,
            category=category,
            context_type=context_type or "resource",
            meta=meta or {},
            session_id=session_id,
            source_path=source_path or "",
            source_doc_id=source_doc_id,
            source_doc_title=source_doc_title,
            tenant_id=tid,
            user_id=uid,
        )
        await orch._derive_queue.put(task)

        logger.info(
            "[MemoryService] Document enqueued for async derive: %s (%d chunks)",
            parent_uri_candidate,
            len(chunks),
        )

        return Context(
            uri=parent_uri_candidate,
            abstract=doc_title,
            context_type=context_type or "resource",
            category=category,
            is_leaf=False,
            meta={**(meta or {}), "dedup_action": "created", "derive_pending": True},
            session_id=session_id,
        )

    # =========================================================================
    # Batch (U2 of plan 011)
    # =========================================================================

    async def batch_add(
        self,
        items: List[Dict[str, Any]],
        source_path: str = "",
        scan_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Batch-add documents with LLM-generated abstracts and overviews.

        When ``scan_meta`` is present, builds a directory hierarchy from
        ``meta.file_path`` values before writing leaf records.

        Args:
            items: List of dicts with ``content``, ``meta``,
                ``category``, etc.
            source_path: Source path hint for the batch.
            scan_meta: Scan metadata for directory tree building.

        Returns:
            Dict with keys ``status``, ``total``, ``imported``,
            ``errors``, ``uris``, ``has_git_project``, ``project_id``.
        """
        orch = self._orch
        orch._ensure_init()

        imported = 0
        errors: List[Dict[str, Any]] = []
        uris: List[str] = []

        # Hierarchical tree building when scan_meta present
        dir_uris: Dict[str, str] = {}
        if scan_meta:
            from pathlib import PurePosixPath

            # Collect unique directories
            all_dirs: set = set()
            for item in items:
                fp = (item.get("meta") or {}).get("file_path", "")
                if fp:
                    parts = PurePosixPath(fp).parts
                    for j in range(1, len(parts)):
                        all_dirs.add("/".join(parts[:j]))

            # Create directory nodes bottom-up (sorted by depth)
            for d in sorted(all_dirs, key=lambda x: x.count("/")):
                parent_dir = str(PurePosixPath(d).parent)
                parent_uri = dir_uris.get(parent_dir) if parent_dir != "." else None
                try:
                    dir_ctx = await orch.add(
                        abstract=PurePosixPath(d).name,
                        content="",
                        category="documents",
                        parent_uri=parent_uri,
                        is_leaf=False,
                        context_type="resource",
                        meta={
                            "source": "batch:scan",
                            "dir_path": d,
                            "ingest_mode": "memory",
                        },
                        dedup=False,
                    )
                    dir_uris[d] = dir_ctx.uri
                    uris.append(dir_ctx.uri)
                except Exception as exc:
                    logger.warning("[batch_add] Dir node failed for %s: %s", d, exc)

        from opencortex.services import memory_service as memory_service_module

        sem = asyncio.Semaphore(memory_service_module._BATCH_ADD_CONCURRENCY)

        async def _process_one(i: int, item: dict) -> dict:
            """Process a single batch item: derive metadata and persist via add.

            Args:
                i: Zero-based index of the item within the batch.
                item: Raw item dict with content, meta, category, etc.

            Returns:
                A dict with ``uri`` and ``index`` on success, or ``error`` and
                ``index`` on failure.
            """
            async with sem:
                content = item.get("content", "")
                file_path = (item.get("meta") or {}).get("file_path", f"item_{i}")
                abstract, overview = await orch._generate_abstract_overview(
                    content, file_path
                )

                item_meta = dict(item.get("meta") or {})
                item_meta.setdefault("source", "batch:scan")
                item_meta["ingest_mode"] = "memory"

                parent_uri = None
                if scan_meta and file_path:
                    from pathlib import PurePosixPath

                    parent_dir = str(PurePosixPath(file_path).parent)
                    parent_uri = dir_uris.get(parent_dir)

                embed_text = ""
                if orch._config.context_flattening_enabled:
                    fp = item_meta.get("file_path", "")
                    if fp:
                        embed_text = f"[{fp}] {abstract}"

                try:
                    result = await orch.add(
                        abstract=abstract,
                        content=content,
                        overview=overview,
                        category=item.get("category", "documents"),
                        parent_uri=parent_uri,
                        context_type=item.get("context_type", "resource"),
                        meta=item_meta,
                        dedup=False,
                        embed_text=embed_text,
                    )
                    return {"uri": result.uri, "index": i}
                except Exception as exc:
                    return {"error": str(exc), "index": i}

        outcomes: List[Any] = []
        task_chunk_size = memory_service_module._BATCH_ADD_TASK_CHUNK_SIZE
        for chunk_start in range(0, len(items), task_chunk_size):
            chunk = items[chunk_start : chunk_start + task_chunk_size]
            chunk_outcomes = await asyncio.gather(
                *[_process_one(chunk_start + i, item) for i, item in enumerate(chunk)],
                return_exceptions=True,
            )
            outcomes.extend(chunk_outcomes)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                errors.append({"error": str(outcome)})
            elif isinstance(outcome, dict) and "error" in outcome:
                errors.append({"index": outcome["index"], "error": outcome["error"]})
            else:
                uris.append(outcome["uri"])
                imported += 1

        has_git = (scan_meta or {}).get("has_git", False)
        project_id = (scan_meta or {}).get("project_id", "public")

        return {
            "status": "ok" if not errors else "partial",
            "total": len(items),
            "imported": imported,
            "errors": errors,
            "has_git_project": has_git and project_id != "public",
            "project_id": project_id,
            "uris": uris,
        }
