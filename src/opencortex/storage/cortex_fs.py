# SPDX-License-Identifier: Apache-2.0
"""
CortexFS: OpenCortex file system abstraction layer.

Encapsulates LocalAGFS, providing file operation interface based on OpenCortex URI.
Responsibilities:
- URI conversion (opencortex:// <-> /local/)
- L0/L1 reading (.abstract.md, .overview.md)
- Relation management (.relations.json)
- Semantic search (vector retrieval + rerank)
- Vector sync (sync vector store on rm/mv)
"""

import atexit
import asyncio
import concurrent.futures
import hashlib
import orjson
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePath
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from opencortex.storage.local_agfs import LocalAGFS
from opencortex.storage.storage_interface import StorageInterface
from opencortex.utils.time_utils import format_simplified, get_current_timestamp, parse_iso_datetime
from opencortex.utils.uri import CortexURI

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Bounded executor for file I/O — limits queue depth to prevent memory leaks
_fs_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="cortexfs",
)
atexit.register(_fs_executor.shutdown, wait=False)


# ========== Dataclass ==========


@dataclass
class RelationEntry:
    """Relation table entry."""

    id: str
    uris: List[str]
    reason: str = ""
    created_at: str = field(default_factory=get_current_timestamp)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "uris": self.uris,
            "reason": self.reason,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "RelationEntry":
        return RelationEntry(**data)


# ========== Singleton Pattern ==========

_instance: Optional["CortexFS"] = None


def init_cortex_fs(
    data_root: str = "./data",
    query_embedder: Optional[Any] = None,
    rerank_config: Optional[Any] = None,
    vector_store: Optional["StorageInterface"] = None,
) -> "CortexFS":
    """Initialize CortexFS singleton.

    Args:
        data_root: Root directory for local filesystem storage
        query_embedder: Embedder instance
        rerank_config: Rerank configuration
        vector_store: Vector store instance
    """
    global _instance
    _instance = CortexFS(
        data_root=data_root,
        query_embedder=query_embedder,
        rerank_config=rerank_config,
        vector_store=vector_store,
    )
    return _instance


def get_cortex_fs() -> "CortexFS":
    """Get CortexFS singleton."""
    if _instance is None:
        raise RuntimeError("CortexFS not initialized. Call init_cortex_fs() first.")
    return _instance


# ========== CortexFS Main Class ==========


class CortexFS:
    """LocalAGFS-based OpenCortex file system.

    APIs are divided into two categories:
    - LocalAGFS basic commands (direct forwarding): read, ls, write, mkdir, rm, mv, grep, stat
    - CortexFS specific capabilities: abstract, overview, find, search, relations, link, unlink
    """

    def __init__(
        self,
        data_root: str = "./data",
        query_embedder: Optional[Any] = None,
        rerank_config: Optional[Any] = None,
        vector_store: Optional["StorageInterface"] = None,
    ):
        self.agfs = LocalAGFS(data_root=data_root)
        self.query_embedder = query_embedder
        self.rerank_config = rerank_config
        self.vector_store = vector_store
        logger.info(f"[CortexFS] Initialized with data_root={data_root}")

    # ========== AGFS Basic Commands ==========

    async def read(self, uri: str, offset: int = 0, size: int = -1) -> bytes:
        """Read file."""
        path = self._uri_to_path(uri)
        result = self.agfs.read(path, offset, size)
        if isinstance(result, bytes):
            return result
        elif result is not None and hasattr(result, "content"):
            return result.content
        else:
            return b""

    async def write(self, uri: str, data: Union[bytes, str]) -> str:
        """Write file."""
        path = self._uri_to_path(uri)
        if isinstance(data, str):
            data = data.encode("utf-8")
        return self.agfs.write(path, data)

    async def mkdir(self, uri: str, mode: str = "755", exist_ok: bool = False) -> None:
        """Create directory."""
        path = self._uri_to_path(uri)
        # Always ensure parent directories exist before creating this directory
        await self._ensure_parent_dirs(path)

        if exist_ok:
            try:
                await self.stat(uri)
                return None
            except Exception:
                pass

        self.agfs.mkdir(path)

    async def rm(self, uri: str, recursive: bool = False) -> Dict[str, Any]:
        """Delete file/directory + recursively update vector index."""
        path = self._uri_to_path(uri)
        uris_to_delete = await self._collect_uris(path, recursive)
        result = self.agfs.rm(path, recursive)
        if uris_to_delete:
            await self._delete_from_vector_store(uris_to_delete)
        return result

    async def mv(self, old_uri: str, new_uri: str) -> Dict[str, Any]:
        """Move file/directory + recursively update vector index."""
        old_path = self._uri_to_path(old_uri)
        new_path = self._uri_to_path(new_uri)
        uris_to_move = await self._collect_uris(old_path, recursive=True)
        result = self.agfs.mv(old_path, new_path)
        if uris_to_move:
            await self._update_vector_store_uris(uris_to_move, old_uri, new_uri)
        return result

    async def grep(self, uri: str, pattern: str, case_insensitive: bool = False) -> Dict:
        """Content search by pattern or keywords."""
        path = self._uri_to_path(uri)
        result = self.agfs.grep(path, pattern, True, case_insensitive)
        if result.get("matches", None) is None:
            result["matches"] = []
        new_matches = []
        for match in result.get("matches", []):
            new_match = {
                "line": match.get("line"),
                "uri": self._path_to_uri(match.get("file")),
                "content": match.get("content"),
            }
            new_matches.append(new_match)
        result["matches"] = new_matches
        return result

    async def stat(self, uri: str) -> Dict[str, Any]:
        """
        File/directory information.

        example: {'name': 'resources', 'size': 128, 'mode': 2147484141, 'modTime': '2026-02-10T21:26:02.934376+00:00', 'isDir': True, 'meta': {}}
        """
        path = self._uri_to_path(uri)
        return self.agfs.stat(path)

    async def glob(self, pattern: str, uri: str = "opencortex://", node_limit: int = 1000) -> Dict:
        """File pattern matching, supports **/*.md recursive."""
        entries = await self.tree(uri, node_limit=node_limit)
        base_uri = uri.rstrip("/")
        matches = []
        for entry in entries:
            rel_path = entry.get("rel_path", "")
            if PurePath(rel_path).match(pattern):
                matches.append(f"{base_uri}/{rel_path}")
        return {"matches": matches, "count": len(matches)}

    async def _batch_fetch_abstracts(
        self,
        entries: List[Dict[str, Any]],
        abs_limit: int,
    ) -> None:
        """Batch fetch abstracts for entries.

        Args:
            entries: List of entries to fetch abstracts for
            abs_limit: Maximum length for abstract truncation
        """
        semaphore = asyncio.Semaphore(6)

        async def fetch_abstract(index: int, entry: Dict[str, Any]) -> tuple:
            async with semaphore:
                if not entry.get("isDir", False):
                    return index, ""
                try:
                    abstract = await self.abstract(entry["uri"])
                    return index, abstract
                except Exception:
                    return index, "[.abstract.md is not ready]"

        tasks = [fetch_abstract(i, entry) for i, entry in enumerate(entries)]
        abstract_results = await asyncio.gather(*tasks)
        for index, abstract in abstract_results:
            if len(abstract) > abs_limit:
                abstract = abstract[: abs_limit - 3] + "..."
            entries[index]["abstract"] = abstract

    async def tree(
        self,
        uri: str = "opencortex://",
        output: str = "original",
        abs_limit: int = 256,
        show_all_hidden: bool = False,
        node_limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """
        Recursively list all contents (includes rel_path).

        Args:
            uri: OpenCortex URI
            output: str = "original" or "agent"
            abs_limit: int = 256 (for agent output abstract truncation)
            show_all_hidden: bool = False (list all hidden files, like -a)
        """
        if output == "original":
            return await self._tree_original(uri, show_all_hidden, node_limit)
        elif output == "agent":
            return await self._tree_agent(uri, abs_limit, show_all_hidden, node_limit)
        else:
            raise ValueError(f"Invalid output format: {output}")

    async def _tree_original(
        self, uri: str, show_all_hidden: bool = False, node_limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """Recursively list all contents (original format)."""
        path = self._uri_to_path(uri)
        all_entries = []

        async def _walk(current_path: str, current_rel: str):
            if len(all_entries) >= node_limit:
                return
            for entry in self.agfs.ls(current_path):
                if len(all_entries) >= node_limit:
                    break
                name = entry.get("name", "")
                if name in [".", ".."]:
                    continue
                rel_path = f"{current_rel}/{name}" if current_rel else name
                new_entry = dict(entry)
                new_entry["rel_path"] = rel_path
                new_entry["uri"] = self._path_to_uri(f"{current_path}/{name}")
                if entry.get("isDir"):
                    all_entries.append(new_entry)
                    await _walk(f"{current_path}/{name}", rel_path)
                elif not name.startswith("."):
                    all_entries.append(new_entry)
                elif show_all_hidden:
                    all_entries.append(new_entry)

        await _walk(path, "")
        return all_entries

    async def _tree_agent(
        self, uri: str, abs_limit: int, show_all_hidden: bool = False, node_limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """Recursively list all contents (agent format with abstracts)."""
        path = self._uri_to_path(uri)
        all_entries = []
        now = datetime.now()

        async def _walk(current_path: str, current_rel: str):
            if len(all_entries) >= node_limit:
                return
            for entry in self.agfs.ls(current_path):
                if len(all_entries) >= node_limit:
                    break
                name = entry.get("name", "")
                if name in [".", ".."]:
                    continue
                rel_path = f"{current_rel}/{name}" if current_rel else name
                new_entry = {
                    "uri": str(CortexURI(uri).join(rel_path)),
                    "size": entry.get("size", 0),
                    "isDir": entry.get("isDir", False),
                    "modTime": format_simplified(
                        parse_iso_datetime(entry.get("modTime", "")), now
                    ),
                }
                if entry.get("isDir"):
                    all_entries.append(new_entry)
                    await _walk(f"{current_path}/{name}", rel_path)
                elif not name.startswith("."):
                    all_entries.append(new_entry)
                elif show_all_hidden:
                    all_entries.append(new_entry)

        await _walk(path, "")

        await self._batch_fetch_abstracts(all_entries, abs_limit)

        return all_entries

    # ========== Vector Store Integration ==========

    async def abstract(
        self,
        uri: str,
    ) -> str:
        """Read directory's L0 summary (.abstract.md)."""
        path = self._uri_to_path(uri)
        info = self.agfs.stat(path)
        if not info.get("isDir"):
            raise ValueError(f"{uri} is not a directory")
        file_path = f"{path}/.abstract.md"
        content = self.agfs.read(file_path)
        return self._handle_agfs_content(content)

    async def overview(
        self,
        uri: str,
    ) -> str:
        """Read directory's L1 overview (.overview.md)."""
        path = self._uri_to_path(uri)
        info = self.agfs.stat(path)
        if not info.get("isDir"):
            raise ValueError(f"{uri} is not a directory")
        file_path = f"{path}/.overview.md"
        content = self.agfs.read(file_path)
        return self._handle_agfs_content(content)

    async def abstract_json(
        self,
        uri: str,
    ) -> Dict[str, Any]:
        """Read directory's machine-readable L0 payload (`.abstract.json`)."""
        path = self._uri_to_path(uri)
        info = self.agfs.stat(path)
        if not info.get("isDir"):
            raise ValueError(f"{uri} is not a directory")
        file_path = f"{path}/.abstract.json"
        content = self.agfs.read(file_path)
        raw_bytes = content if isinstance(content, bytes) else bytes(content or b"")
        return orjson.loads(raw_bytes) if raw_bytes else {}

    async def relations(
        self,
        uri: str,
    ) -> List[Dict[str, Any]]:
        """Get relation list.

        Returns: [{"uri": "...", "reason": "..."}, ...]
        """
        entries = await self.get_relation_table(uri)
        result = []
        for entry in entries:
            for u in entry.uris:
                result.append({"uri": u, "reason": entry.reason})
        return result

    async def find(
        self,
        query: str,
        target_uri: str = "",
        limit: int = 10,
        score_threshold: Optional[float] = None,
        filter: Optional[Dict] = None,
    ):
        """Semantic search.

        Args:
            query: Search query
            target_uri: Target directory URI
            limit: Return count
            score_threshold: Score threshold
            filter: Metadata filter

        Returns:
            FindResult
        """
        from opencortex.retrieve.types import (
            ContextType,
            FindResult,
            MatchedContext,
        )

        if not self.rerank_config:
            raise RuntimeError("rerank_config is required for find")

        storage = self._get_vector_store()
        if not storage:
            raise RuntimeError("Vector store not initialized. Call init_cortex_fs() first.")

        embedder = self._get_embedder()
        if not embedder:
            raise RuntimeError("Embedder not configured.")

        # Infer context_type
        context_type = self._infer_context_type(target_uri) if target_uri else ContextType.RESOURCE
        query_vector = embedder.embed_query(query).dense_vector
        conds = [{"op": "must", "field": "is_leaf", "conds": [True]}]
        if context_type != ContextType.ANY:
            conds.append(
                {"op": "must", "field": "context_type", "conds": [context_type.value]}
            )
        if target_uri:
            conds.append({"op": "prefix", "field": "uri", "prefix": target_uri})
        if filter:
            conds.append(filter)
        results = await storage.search(
            "context",
            query_vector=query_vector,
            filter={"op": "and", "conds": conds},
            limit=limit,
            text_query=query,
        )

        # Convert QueryResult to FindResult
        memories, resources, skills = [], [], []
        for record in results:
            score = float(record.get("_score", record.get("score", 0.0)) or 0.0)
            if score_threshold is not None and score < score_threshold:
                continue
            ctx = MatchedContext(
                uri=record.get("uri", ""),
                context_type=ContextType(record.get("context_type", context_type.value)),
                is_leaf=bool(record.get("is_leaf", False)),
                abstract=record.get("abstract", ""),
                overview=record.get("overview"),
                category=record.get("category", ""),
                score=score,
            )
            if ctx.context_type == ContextType.MEMORY:
                memories.append(ctx)
            elif ctx.context_type == ContextType.RESOURCE:
                resources.append(ctx)
            elif ctx.context_type == ContextType.SKILL:
                skills.append(ctx)

        return FindResult(
            memories=memories,
            resources=resources,
            skills=skills,
        )

    async def search(
        self,
        query: str,
        target_uri: str = "",
        session_info: Optional[Dict] = None,
        limit: int = 10,
        score_threshold: Optional[float] = None,
        filter: Optional[Dict] = None,
    ):
        """Complex search with session context.

        Args:
            query: Search query
            target_uri: Target directory URI
            session_info: Session information
            limit: Return count
            filter: Metadata filter

        Returns:
            FindResult
        """
        from opencortex.retrieve.intent_analyzer import IntentAnalyzer
        from opencortex.retrieve.types import (
            ContextType,
            FindResult,
            MatchedContext,
            QueryPlan,
            TypedQuery,
        )

        session_summary = session_info.get("summary") if session_info else None
        recent_messages = session_info.get("recent_messages") if session_info else None

        query_plan: Optional[QueryPlan] = None

        # When target_uri exists: read abstract, infer context_type
        target_context_type: Optional[ContextType] = None
        target_abstract = ""
        if target_uri:
            target_context_type = self._infer_context_type(target_uri)
            try:
                target_abstract = await self.abstract(target_uri)
            except Exception:
                target_abstract = ""

        # With session context: intent analysis
        if session_summary or recent_messages:
            analyzer = IntentAnalyzer(max_recent_messages=5)
            query_plan = await analyzer.analyze(
                compression_summary=session_summary or "",
                messages=recent_messages or [],
                current_message=query,
                context_type=target_context_type,
                target_abstract=target_abstract,
            )
            typed_queries = query_plan.queries
            # Set target_directories
            if target_uri:
                for tq in typed_queries:
                    tq.target_directories = [target_uri]
        else:
            # No session context: create query directly
            if target_context_type:
                # Has target_uri: only query that type
                typed_queries = [
                    TypedQuery(
                        query=query,
                        context_type=target_context_type,
                        intent="",
                        priority=1,
                        target_directories=[target_uri] if target_uri else [],
                    )
                ]
            else:
                # No target_uri: query all types
                typed_queries = [
                    TypedQuery(query=query, context_type=ctx_type, intent="", priority=1)
                    for ctx_type in [ContextType.MEMORY, ContextType.RESOURCE]
                ]

        storage = self._get_vector_store()
        embedder = self._get_embedder()
        if not storage:
            raise RuntimeError("Vector store not initialized. Call init_cortex_fs() first.")
        if not embedder:
            raise RuntimeError("Embedder not configured.")

        async def _execute(tq: TypedQuery):
            query_vector = embedder.embed_query(tq.query).dense_vector
            conds = [{"op": "must", "field": "is_leaf", "conds": [True]}]
            if tq.context_type != ContextType.ANY:
                conds.append(
                    {"op": "must", "field": "context_type", "conds": [tq.context_type.value]}
                )
            if tq.target_directories:
                conds.append(
                    {
                        "op": "or",
                        "conds": [
                            {"op": "prefix", "field": "uri", "prefix": prefix}
                            for prefix in tq.target_directories
                        ],
                    }
                )
            if filter:
                conds.append(filter)
            return await storage.search(
                "context",
                query_vector=query_vector,
                filter={"op": "and", "conds": conds},
                limit=limit,
                text_query=tq.query,
            )

        query_results = await asyncio.gather(*[_execute(tq) for tq in typed_queries])

        # Aggregate results to FindResult
        memories, resources, skills = [], [], []
        for records in query_results:
            for record in records:
                score = float(record.get("_score", record.get("score", 0.0)) or 0.0)
                if score_threshold is not None and score < score_threshold:
                    continue
                ctx = MatchedContext(
                    uri=record.get("uri", ""),
                    context_type=ContextType(record.get("context_type", ContextType.RESOURCE.value)),
                    is_leaf=bool(record.get("is_leaf", False)),
                    abstract=record.get("abstract", ""),
                    overview=record.get("overview"),
                    category=record.get("category", ""),
                    score=score,
                )
                if ctx.context_type == ContextType.MEMORY:
                    memories.append(ctx)
                elif ctx.context_type == ContextType.RESOURCE:
                    resources.append(ctx)
                elif ctx.context_type == ContextType.SKILL:
                    skills.append(ctx)

        return FindResult(
            memories=memories,
            resources=resources,
            skills=skills,
            query_plan=query_plan,
            query_results=query_results,
        )

    # ========== Relation Management ==========

    async def link(
        self,
        from_uri: str,
        uris: Union[str, List[str]],
        reason: str = "",
    ) -> None:
        """Create relation (maintained in .relations.json)."""
        if isinstance(uris, str):
            uris = [uris]

        from_path = self._uri_to_path(from_uri)

        entries = await self._read_relation_table(from_path)
        existing_ids = {e.id for e in entries}

        link_id = next(f"link_{i}" for i in range(1, 10000) if f"link_{i}" not in existing_ids)

        entries.append(RelationEntry(id=link_id, uris=uris, reason=reason))

        await self._write_relation_table(from_path, entries)
        logger.info(f"[CortexFS] Created link: {from_uri} -> {uris}")

    async def unlink(
        self,
        from_uri: str,
        uri: str,
    ) -> None:
        """Delete relation."""
        from_path = self._uri_to_path(from_uri)

        try:
            entries = await self._read_relation_table(from_path)

            entry_to_modify = None
            for entry in entries:
                if uri in entry.uris:
                    entry_to_modify = entry
                    break

            if not entry_to_modify:
                logger.warning(f"[CortexFS] URI not found in relations: {uri}")
                return

            entry_to_modify.uris.remove(uri)

            if not entry_to_modify.uris:
                entries.remove(entry_to_modify)
                logger.info(f"[CortexFS] Removed empty entry: {entry_to_modify.id}")

            await self._write_relation_table(from_path, entries)
            logger.info(f"[CortexFS] Removed link: {from_uri} -> {uri}")

        except Exception as e:
            logger.error(f"[CortexFS] Failed to unlink {from_uri} -> {uri}: {e}")
            raise IOError(f"Failed to unlink: {e}")

    async def get_relation_table(self, uri: str) -> List[RelationEntry]:
        """Get relation table."""
        path = self._uri_to_path(uri)
        return await self._read_relation_table(path)

    # ========== URI Conversion ==========

    # Maximum bytes for a single filename component (filesystem limit is typically 255)
    _MAX_FILENAME_BYTES = 255

    @staticmethod
    def _shorten_component(component: str, max_bytes: int = 255) -> str:
        """Shorten a path component if its UTF-8 encoding exceeds max_bytes."""
        if len(component.encode("utf-8")) <= max_bytes:
            return component
        hash_suffix = hashlib.sha256(component.encode("utf-8")).hexdigest()[:8]
        # Trim to fit within max_bytes after adding hash suffix
        prefix = component
        target = max_bytes - len(f"_{hash_suffix}".encode("utf-8"))
        while len(prefix.encode("utf-8")) > target and prefix:
            prefix = prefix[:-1]
        return f"{prefix}_{hash_suffix}"

    def _uri_to_path(self, uri: str) -> str:
        """Convert opencortex:// URI to local AGFS path.

        opencortex://myteam/resources/proj -> /local/myteam/resources/proj
        """
        remainder = uri[len("opencortex://"):].strip("/")
        if not remainder:
            return "/local"
        # Ensure each path component does not exceed filesystem filename limit
        parts = remainder.split("/")
        safe_parts = [self._shorten_component(p, self._MAX_FILENAME_BYTES) for p in parts]
        return f"/local/{'/'.join(safe_parts)}"

    def _path_to_uri(self, path: str) -> str:
        """Convert local AGFS path to opencortex:// URI.

        /local/myteam/resources/proj -> opencortex://myteam/resources/proj
        """
        if path.startswith("opencortex://"):
            return path
        elif path.startswith("/local/"):
            return f"opencortex://{path[7:]}"  # Remove /local/ prefix
        elif path.startswith("/local"):
            return "opencortex://"
        elif path.startswith("/"):
            return f"opencortex:/{path}"
        else:
            return f"opencortex://{path}"

    def _handle_agfs_read(self, result: Union[bytes, Any, None]) -> bytes:
        """Handle LocalAGFS read return types consistently."""
        if isinstance(result, bytes):
            return result
        elif result is None:
            return b""
        elif hasattr(result, "content") and result.content is not None:
            return result.content
        else:
            try:
                return str(result).encode("utf-8")
            except Exception:
                return b""

    def _handle_agfs_content(self, result: Union[bytes, Any, None]) -> str:
        """Handle LocalAGFS content return types consistently."""
        if isinstance(result, bytes):
            return result.decode("utf-8")
        elif hasattr(result, "content"):
            return result.content.decode("utf-8")
        elif result is None:
            return ""
        else:
            try:
                return str(result)
            except Exception:
                return ""

    def _infer_context_type(self, uri: str):
        """Infer context_type from URI."""
        from opencortex.retrieve.types import ContextType

        if "/memories" in uri:
            return ContextType.MEMORY
        return ContextType.RESOURCE

    # ========== Vector Sync Helper Methods ==========

    async def _collect_uris(self, path: str, recursive: bool) -> List[str]:
        """Recursively collect all URIs (for rm/mv)."""
        uris = []

        async def _collect(p: str):
            try:
                for entry in self.agfs.ls(p):
                    name = entry.get("name", "")
                    if name in [".", ".."]:
                        continue
                    full_path = f"{p}/{name}".replace("//", "/")
                    if entry.get("isDir"):
                        if recursive:
                            await _collect(full_path)
                    else:
                        uris.append(self._path_to_uri(full_path))
            except Exception:
                pass

        await _collect(path)
        return uris

    async def _delete_from_vector_store(self, uris: List[str]) -> None:
        """Delete records with specified URIs from vector store."""
        storage = self._get_vector_store()
        if not storage:
            return

        for uri in uris:
            try:
                await storage.remove_by_uri("context", uri)
                logger.info(f"[CortexFS] Deleted from vector store: {uri}")
            except Exception as e:
                logger.warning(f"[CortexFS] Failed to delete {uri} from vector store: {e}")

    async def _update_vector_store_uris(
        self, uris: List[str], old_base: str, new_base: str
    ) -> None:
        """Update URIs in vector store (when moving files)."""
        storage = self._get_vector_store()
        if not storage:
            return

        old_base_uri = self._path_to_uri(old_base)
        new_base_uri = self._path_to_uri(new_base)

        for uri in uris:
            try:
                records = await storage.filter(
                    collection="context",
                    filter={"op": "must", "field": "uri", "conds": [uri]},
                    limit=1,
                )

                if not records or "id" not in records[0]:
                    continue

                record = records[0]
                record_id = record["id"]

                new_uri = uri.replace(old_base_uri, new_base_uri, 1)

                old_parent_uri = record.get("parent_uri", "")
                new_parent_uri = (
                    old_parent_uri.replace(old_base_uri, new_base_uri, 1) if old_parent_uri else ""
                )

                await storage.update(
                    "context",
                    record_id,
                    {
                        "uri": new_uri,
                        "parent_uri": new_parent_uri,
                    },
                )
                logger.info(f"[CortexFS] Updated URI: {uri} -> {new_uri}")
            except Exception as e:
                logger.warning(f"[CortexFS] Failed to update {uri} in vector store: {e}")

    def _get_vector_store(self) -> Optional["StorageInterface"]:
        """Get vector store instance."""
        return self.vector_store

    def _get_embedder(self) -> Any:
        """Get embedder instance."""
        return self.query_embedder

    # ========== Parent Directory Creation ==========

    async def _ensure_parent_dirs(self, path: str) -> None:
        """Recursively create all parent directories."""
        # Remove leading slash if present, then split
        parts = path.lstrip("/").split("/")
        # Create directories up to the last component (which might be a file)
        for i in range(1, len(parts)):
            parent = "/" + "/".join(parts[:i])
            try:
                self.agfs.mkdir(parent)
            except Exception as e:
                # Log the error but continue, as parent might already exist
                if "exist" not in str(e).lower() and "already" not in str(e).lower():
                    logger.debug(f"Failed to create parent directory {parent}: {e}")

    # ========== Relation Table Internal Methods ==========

    async def _read_relation_table(self, dir_path: str) -> List[RelationEntry]:
        """Read .relations.json."""
        table_path = f"{dir_path}/.relations.json"
        try:
            content = self._handle_agfs_read(self.agfs.read(table_path))
            data = orjson.loads(content)
        except FileNotFoundError:
            return []
        except Exception:
            return []

        entries = []
        # Compatible with old format (nested) and new format (flat)
        if isinstance(data, list):
            # New format: flat list
            for entry_data in data:
                entries.append(RelationEntry.from_dict(entry_data))
        elif isinstance(data, dict):
            # Old format: nested {namespace: {user: [entries]}}
            for _namespace, user_dict in data.items():
                for _user, entry_list in user_dict.items():
                    for entry_data in entry_list:
                        entries.append(RelationEntry.from_dict(entry_data))
        return entries

    async def _write_relation_table(self, dir_path: str, entries: List[RelationEntry]) -> None:
        """Write .relations.json."""
        # Use flat list format
        data = [entry.to_dict() for entry in entries]

        content = orjson.dumps(data, option=orjson.OPT_INDENT_2)
        table_path = f"{dir_path}/.relations.json"
        self.agfs.write(table_path, content)

    # ========== Batch Read (backward compatible) ==========

    async def read_batch(self, uris: List[str], level: str = "l0") -> Dict[str, str]:
        """Batch read content from multiple URIs (concurrent)."""

        async def _read_one(uri: str) -> tuple:
            try:
                if level == "l0":
                    content = await self.abstract(uri)
                elif level == "l1":
                    content = await self.overview(uri)
                else:
                    content = ""
                return uri, content
            except Exception:
                return uri, ""

        pairs = await asyncio.gather(*[_read_one(u) for u in uris])
        return {uri: content for uri, content in pairs if content}

    # ========== Other Preserved Methods ==========

    async def write_file(
        self,
        uri: str,
        content: Union[str, bytes],
    ) -> None:
        """Write file directly."""
        path = self._uri_to_path(uri)
        await self._ensure_parent_dirs(path)

        if isinstance(content, str):
            content = content.encode("utf-8")
        self.agfs.write(path, content)

    async def read_file(
        self,
        uri: str,
    ) -> str:
        """Read single file."""
        path = self._uri_to_path(uri)
        content = self.agfs.read(path)
        return self._handle_agfs_content(content)

    async def read_file_bytes(
        self,
        uri: str,
    ) -> bytes:
        """Read single binary file."""
        path = self._uri_to_path(uri)
        try:
            return self._handle_agfs_read(self.agfs.read(path))
        except Exception as e:
            raise FileNotFoundError(f"Failed to read {uri}: {e}")

    async def write_file_bytes(
        self,
        uri: str,
        content: bytes,
    ) -> None:
        """Write single binary file."""
        path = self._uri_to_path(uri)
        await self._ensure_parent_dirs(path)
        self.agfs.write(path, content)

    async def append_file(
        self,
        uri: str,
        content: str,
    ) -> None:
        """Append content to file."""
        path = self._uri_to_path(uri)

        try:
            existing = ""
            try:
                existing_bytes = self._handle_agfs_read(self.agfs.read(path))
                existing = existing_bytes.decode("utf-8")
            except Exception:
                pass

            await self._ensure_parent_dirs(path)
            self.agfs.write(path, (existing + content).encode("utf-8"))

        except Exception as e:
            logger.error(f"[CortexFS] Failed to append to file {uri}: {e}")
            raise IOError(f"Failed to append to file {uri}: {e}")

    async def ls(
        self,
        uri: str,
        output: str = "original",
        abs_limit: int = 256,
        show_all_hidden: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        List directory contents (URI version).

        Args:
            uri: OpenCortex URI
            output: str = "original" or "agent"
            abs_limit: int = 256
            show_all_hidden: bool = False (list all hidden files, like -a)
        """
        if output == "original":
            return await self._ls_original(uri, show_all_hidden)
        elif output == "agent":
            return await self._ls_agent(uri, abs_limit, show_all_hidden)
        else:
            raise ValueError(f"Invalid output format: {output}")

    async def _ls_agent(
        self, uri: str, abs_limit: int, show_all_hidden: bool
    ) -> List[Dict[str, Any]]:
        """List directory contents (agent format with abstracts)."""
        path = self._uri_to_path(uri)
        try:
            entries = self.agfs.ls(path)
        except Exception as e:
            raise FileNotFoundError(f"Failed to list {uri}: {e}")
        now = datetime.now()
        all_entries = []
        for entry in entries:
            name = entry.get("name", "")
            raw_time = entry.get("modTime", "")
            if raw_time and len(raw_time) > 26 and "+" in raw_time:
                parts = raw_time.split("+")
                raw_time = parts[0][:26] + "+" + parts[1]
            new_entry = {
                "uri": str(CortexURI(uri).join(name)),
                "size": entry.get("size", 0),
                "isDir": entry.get("isDir", False),
                "modTime": format_simplified(parse_iso_datetime(raw_time), now),
            }
            if entry.get("isDir"):
                all_entries.append(new_entry)
            elif not name.startswith("."):
                all_entries.append(new_entry)
            elif show_all_hidden:
                all_entries.append(new_entry)
        await self._batch_fetch_abstracts(all_entries, abs_limit)
        return all_entries

    async def _ls_original(self, uri: str, show_all_hidden: bool = False) -> List[Dict[str, Any]]:
        """List directory contents (original format)."""
        path = self._uri_to_path(uri)
        try:
            entries = self.agfs.ls(path)
            all_entries = []
            for entry in entries:
                name = entry.get("name", "")
                new_entry = dict(entry)
                new_entry["uri"] = str(CortexURI(uri).join(name))
                if entry.get("isDir"):
                    all_entries.append(new_entry)
                elif not name.startswith("."):
                    all_entries.append(new_entry)
                elif show_all_hidden:
                    all_entries.append(new_entry)
            return all_entries
        except Exception as e:
            raise FileNotFoundError(f"Failed to list {uri}: {e}")

    async def move_file(
        self,
        from_uri: str,
        to_uri: str,
    ) -> None:
        """Move file."""
        from_path = self._uri_to_path(from_uri)
        to_path = self._uri_to_path(to_uri)
        content = self.agfs.read(from_path)
        await self._ensure_parent_dirs(to_path)
        self.agfs.write(to_path, content)
        self.agfs.rm(from_path)

    # ========== Temp File Operations ==========

    def create_temp_uri(self) -> str:
        """Create temp directory URI."""
        return CortexURI.create_temp_uri()

    async def delete_temp(self, temp_uri: str) -> None:
        """Delete temp directory and its contents."""
        path = self._uri_to_path(temp_uri)
        try:
            for entry in self.agfs.ls(path):
                name = entry.get("name", "")
                if name in [".", ".."]:
                    continue
                entry_path = f"{path}/{name}"
                if entry.get("isDir"):
                    await self.delete_temp(f"{temp_uri}/{name}")
                else:
                    self.agfs.rm(entry_path)
            self.agfs.rm(path)
        except Exception as e:
            logger.warning(f"[CortexFS] Failed to delete temp {temp_uri}: {e}")

    async def get_relations(self, uri: str) -> List[str]:
        """Get all related URIs (backward compatible)."""
        entries = await self.get_relation_table(uri)
        all_uris = []
        for entry in entries:
            all_uris.extend(entry.uris)
        return all_uris

    async def get_relations_with_content(
        self,
        uri: str,
        include_l0: bool = True,
        include_l1: bool = False,
    ) -> List[Dict[str, Any]]:
        """Get related URIs and their content (backward compatible)."""
        relation_uris = await self.get_relations(uri)
        if not relation_uris:
            return []

        results = []
        abstracts = {}
        overviews = {}
        if include_l0:
            abstracts = await self.read_batch(relation_uris, level="l0")
        if include_l1:
            overviews = await self.read_batch(relation_uris, level="l1")

        for rel_uri in relation_uris:
            info = {"uri": rel_uri}
            if include_l0:
                info["abstract"] = abstracts.get(rel_uri, "")
            if include_l1:
                info["overview"] = overviews.get(rel_uri, "")
            results.append(info)

        return results

    async def write_context(
        self,
        uri: str,
        content: Union[str, bytes] = "",
        abstract: str = "",
        abstract_json: Optional[Union[Dict[str, Any], Any]] = None,
        overview: str = "",
        content_filename: str = "content.md",
        is_leaf: bool = False,
    ) -> None:
        """Write context to local storage (L0/L1/L2) via thread executor."""
        path = self._uri_to_path(uri)
        loop = asyncio.get_running_loop()

        def _sync_write():
            try:
                self.agfs.mkdir(path)
            except Exception as e:
                if "exist" not in str(e).lower():
                    raise
            if content:
                data = content.encode("utf-8") if isinstance(content, str) else content
                self.agfs.write(f"{path}/{content_filename}", data)
            if abstract:
                self.agfs.write(f"{path}/.abstract.md", abstract.encode("utf-8"))
            if abstract_json is not None:
                if hasattr(abstract_json, "model_dump"):
                    json_payload = abstract_json.model_dump(mode="json")
                else:
                    json_payload = abstract_json
                self.agfs.write(
                    f"{path}/.abstract.json",
                    orjson.dumps(json_payload),
                )
            if overview:
                self.agfs.write(f"{path}/.overview.md", overview.encode("utf-8"))

        try:
            await loop.run_in_executor(_fs_executor, _sync_write)
        except Exception as e:
            logger.error(f"[CortexFS] Failed to write {uri}: {e}")
            raise IOError(f"Failed to write {uri}: {e}")
