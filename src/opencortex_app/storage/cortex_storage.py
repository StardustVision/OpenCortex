# SPDX-License-Identifier: Apache-2.0
"""OpenCortex URI storage backed by CFS."""

from __future__ import annotations

import asyncio
import fnmatch
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, Field

from opencortex_app.storage.cfs import CFS

storage_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="opencortex-app-storage",
)


class RelationEntry(BaseModel):
    """One relation table entry."""

    id: str
    uris: list[str]
    reason: str = ""
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable relation entry."""
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RelationEntry":
        """Build a relation entry from stored JSON."""
        return cls.model_validate(data)


class CortexStorage:
    """Store OpenCortex URI trees and context layers."""

    def __init__(self, *, data_root: str = "./data", cfs: CFS | None = None) -> None:
        self.cfs = cfs or CFS(root=data_root)

    async def write_context(
        self,
        *,
        uri: str,
        content: str | bytes = "",
        abstract: str = "",
        abstract_json: dict[str, Any] | Any | None = None,
        overview: str = "",
        content_filename: str = "content.md",
        is_leaf: bool = False,
    ) -> None:
        """Write L0, L1, and L2 files for one context URI."""
        _ = is_leaf
        node_path = uri_relative_path(uri)
        loop = asyncio.get_running_loop()

        def write_files() -> None:
            self.cfs.mkdir(node_path)
            if content:
                content_bytes = (
                    content.encode("utf-8") if isinstance(content, str) else content
                )
                self.cfs.write_bytes(Path(node_path) / content_filename, content_bytes)
            if abstract:
                self.cfs.write_text(Path(node_path) / ".abstract.md", abstract)
            if abstract_json is not None:
                payload = (
                    abstract_json.model_dump(mode="json")
                    if hasattr(abstract_json, "model_dump")
                    else abstract_json
                )
                self.cfs.write_text(
                    Path(node_path) / ".abstract.json",
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                )
            if overview:
                self.cfs.write_text(Path(node_path) / ".overview.md", overview)

        await loop.run_in_executor(storage_executor, write_files)

    async def abstract(self, uri: str) -> str:
        """Read a context L0 abstract."""
        return await self.read_file(f"{uri.rstrip('/')}/.abstract.md")

    async def overview(self, uri: str) -> str:
        """Read a context L1 overview."""
        return await self.read_file(f"{uri.rstrip('/')}/.overview.md")

    async def abstract_json(self, uri: str) -> dict[str, Any]:
        """Read a context machine-readable L0 payload."""
        content = await self.read_file(f"{uri.rstrip('/')}/.abstract.json")
        return json.loads(content) if content else {}

    async def read(self, uri: str, offset: int = 0, size: int = -1) -> bytes:
        """Read bytes from a file URI."""
        data = self.cfs.read_bytes(uri_relative_path(uri))
        if offset:
            data = data[offset:]
        if size >= 0:
            data = data[:size]
        return data

    async def write(self, uri: str, data: str | bytes) -> str:
        """Write bytes or text to a file URI."""
        path = uri_relative_path(uri)
        if isinstance(data, str):
            return self.cfs.write_text(path, data)
        return self.cfs.write_bytes(path, data)

    async def read_file(self, uri: str) -> str:
        """Read UTF-8 text from a file URI."""
        return self.cfs.read_text(uri_relative_path(uri))

    async def read_file_bytes(self, uri: str) -> bytes:
        """Read bytes from a file URI."""
        return self.cfs.read_bytes(uri_relative_path(uri))

    async def write_file(self, uri: str, content: str | bytes) -> None:
        """Write one file."""
        await self.write(uri, content)

    async def write_file_bytes(self, uri: str, content: bytes) -> None:
        """Write one binary file."""
        await self.write(uri, content)

    async def append_file(self, uri: str, content: str) -> None:
        """Append text to one file."""
        self.cfs.append_text(uri_relative_path(uri), content)

    async def mkdir(self, uri: str, *, exist_ok: bool = True) -> None:
        """Create a directory URI."""
        self.cfs.mkdir(uri_relative_path(uri), exist_ok=exist_ok)

    async def rm(self, uri: str, recursive: bool = False) -> dict[str, Any]:
        """Remove a file or directory URI."""
        self.cfs.remove(uri_relative_path(uri), recursive=recursive)
        return {"uri": uri, "removed": True}

    async def mv(self, old_uri: str, new_uri: str) -> dict[str, Any]:
        """Move a file or directory URI."""
        self.cfs.move(uri_relative_path(old_uri), uri_relative_path(new_uri))
        return {"from": old_uri, "to": new_uri, "moved": True}

    async def move_file(self, from_uri: str, to_uri: str) -> None:
        """Move one file URI."""
        self.cfs.move(uri_relative_path(from_uri), uri_relative_path(to_uri))

    async def stat(self, uri: str) -> dict[str, Any]:
        """Return metadata for one file or directory URI."""
        return self.cfs.stat(uri_relative_path(uri))

    async def ls(
        self,
        uri: str,
        *,
        show_all_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        """List one directory URI."""
        entries = []
        for entry in self.cfs.list(uri_relative_path(uri)):
            if str(entry["name"]).startswith(".") and not show_all_hidden:
                continue
            entry["uri"] = join_uri(uri, str(entry["name"]))
            entries.append(entry)
        return entries

    async def tree(
        self,
        uri: str = "opencortex://",
        *,
        show_all_hidden: bool = False,
        node_limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Recursively list a URI subtree."""
        root_path = uri_relative_path(uri)
        base_uri = uri.rstrip("/")
        root = self.cfs.resolve(root_path)
        entries: list[dict[str, Any]] = []

        for path in self.cfs.walk(root_path):
            if len(entries) >= node_limit:
                break
            rel_from_root = path.relative_to(root).as_posix()
            hidden_path = any(part.startswith(".") for part in rel_from_root.split("/"))
            if not show_all_hidden and hidden_path:
                continue
            entry = self.cfs.stat(self.cfs.relative(path))
            entry["rel_path"] = rel_from_root
            entry["uri"] = f"{base_uri}/{rel_from_root}"
            entries.append(entry)
        return entries

    async def grep(
        self,
        uri: str,
        pattern: str,
        case_insensitive: bool = False,
    ) -> dict[str, Any]:
        """Search text files under one URI."""
        result = self.cfs.grep(
            uri_relative_path(uri),
            pattern,
            case_insensitive=case_insensitive,
        ).to_dict()
        for match in result.get("matches", []):
            match["uri"] = self.relative_path_to_uri(str(match.pop("file")))
        return result

    async def glob(
        self,
        pattern: str,
        uri: str = "opencortex://",
        node_limit: int = 1000,
    ) -> dict[str, Any]:
        """Return URIs matching a glob pattern under one URI."""
        entries = await self.tree(uri, show_all_hidden=True, node_limit=node_limit)
        matches = [
            entry["uri"]
            for entry in entries
            if fnmatch.fnmatch(str(entry.get("rel_path", "")), pattern)
        ]
        return {"matches": matches, "count": len(matches)}

    async def read_batch(self, uris: list[str], level: str = "l0") -> dict[str, str]:
        """Read L0 or L1 content from multiple URI directories."""

        async def read_one(uri: str) -> tuple[str, str]:
            try:
                if level == "l0":
                    return uri, await self.abstract(uri)
                if level == "l1":
                    return uri, await self.overview(uri)
            except OSError:
                return uri, ""
            return uri, ""

        pairs = await asyncio.gather(*(read_one(uri) for uri in uris))
        return {uri: content for uri, content in pairs if content}

    def create_temp_uri(self) -> str:
        """Create a temp directory URI."""
        temp_path = self.cfs.create_temp_path()
        self.cfs.mkdir(temp_path)
        return self.relative_path_to_uri(temp_path)

    async def delete_temp(self, temp_uri: str) -> None:
        """Delete a temp directory recursively."""
        self.cfs.remove(uri_relative_path(temp_uri), recursive=True)

    async def link(
        self,
        from_uri: str,
        uris: str | list[str],
        reason: str = "",
    ) -> None:
        """Create a relation entry in the source URI's relation table."""
        target_uris = [uris] if isinstance(uris, str) else list(uris)
        entries = await self.get_relation_table(from_uri)
        existing_ids = {entry.id for entry in entries}
        link_id = next(
            f"link_{index}"
            for index in range(1, 10000)
            if f"link_{index}" not in existing_ids
        )
        entries.append(RelationEntry(id=link_id, uris=target_uris, reason=reason))
        self.write_relation_table(from_uri, entries)

    async def unlink(self, from_uri: str, uri: str) -> None:
        """Remove one target URI from the source URI's relation table."""
        entries = await self.get_relation_table(from_uri)
        for entry in list(entries):
            if uri not in entry.uris:
                continue
            entry.uris.remove(uri)
            if not entry.uris:
                entries.remove(entry)
            self.write_relation_table(from_uri, entries)
            return

    async def relations(self, uri: str) -> list[dict[str, str]]:
        """Return flattened relation targets for one URI."""
        result = []
        for entry in await self.get_relation_table(uri):
            for target_uri in entry.uris:
                result.append({"uri": target_uri, "reason": entry.reason})
        return result

    async def get_relation_table(self, uri: str) -> list[RelationEntry]:
        """Return stored relation table entries for one URI."""
        try:
            content = self.cfs.read_text(
                Path(uri_relative_path(uri)) / ".relations.json"
            )
        except OSError:
            return []
        if not content:
            return []
        data = json.loads(content)
        if not isinstance(data, list):
            return []
        return [
            RelationEntry.from_dict(item) for item in data if isinstance(item, dict)
        ]

    async def get_relations(self, uri: str) -> list[str]:
        """Return all related target URIs for one URI."""
        target_uris: list[str] = []
        for entry in await self.get_relation_table(uri):
            target_uris.extend(entry.uris)
        return target_uris

    async def get_relations_with_content(
        self,
        uri: str,
        *,
        include_l0: bool = True,
        include_l1: bool = False,
    ) -> list[dict[str, Any]]:
        """Return related target URIs with optional L0/L1 content."""
        target_uris = await self.get_relations(uri)
        l0_content = (
            await self.read_batch(target_uris, level="l0") if include_l0 else {}
        )
        l1_content = (
            await self.read_batch(target_uris, level="l1") if include_l1 else {}
        )
        result = []
        for target_uri in target_uris:
            item: dict[str, Any] = {"uri": target_uri}
            if include_l0:
                item["abstract"] = l0_content.get(target_uri, "")
            if include_l1:
                item["overview"] = l1_content.get(target_uri, "")
            result.append(item)
        return result

    def uri_to_path(self, uri: str) -> Path:
        """Resolve an OpenCortex URI to a safe local path."""
        return self.cfs.resolve(uri_relative_path(uri))

    def path_to_uri(self, path: str | Path) -> str:
        """Convert a local path under the backing CFS root to an OpenCortex URI."""
        rel = self.cfs.relative(Path(path))
        return self.relative_path_to_uri(rel)

    @staticmethod
    def relative_path_to_uri(relative_path: str) -> str:
        """Convert a CFS-relative path to an OpenCortex URI."""
        return f"opencortex://{relative_path}" if relative_path else "opencortex://"

    def write_relation_table(
        self,
        uri: str,
        entries: list[RelationEntry],
    ) -> None:
        """Persist relation table entries for one URI."""
        node_path = uri_relative_path(uri)
        self.cfs.mkdir(node_path)
        self.cfs.write_text(
            Path(node_path) / ".relations.json",
            json.dumps(
                [entry.to_dict() for entry in entries],
                ensure_ascii=False,
                indent=2,
            ),
        )


def uri_relative_path(uri: str) -> str:
    """Return safe CFS-relative path for an OpenCortex URI."""
    text = str(uri or "").strip()
    if text.startswith("opencortex://"):
        text = text[len("opencortex://") :]
    elif text.startswith("opencortex:/"):
        text = text[len("opencortex:/") :]
    path = PurePosixPath(text.strip("/"))
    parts = [part for part in path.parts if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ValueError(f"URI contains parent traversal: {uri}")
    return PurePosixPath(*parts).as_posix() if parts else ""


def join_uri(base_uri: str, name: str) -> str:
    """Join one path segment to an OpenCortex URI."""
    return f"{base_uri.rstrip('/')}/{name}"
