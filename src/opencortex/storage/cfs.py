# SPDX-License-Identifier: Apache-2.0
"""Local file store used by CortexStorage."""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class GrepMatch(BaseModel):
    """One text search match."""

    file: str
    line: int
    content: str


class GrepResult(BaseModel):
    """Text search result."""

    matches: list[GrepMatch] = Field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable grep result."""
        data = self.model_dump()
        data["count"] = len(self.matches)
        if self.error:
            data["error"] = self.error
        else:
            data.pop("error", None)
        return data


class CFS:
    """Small local filesystem wrapper rooted at one data directory."""

    def __init__(self, *, root: str = "./data") -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str | Path = "") -> Path:
        """Resolve a relative path under the configured root."""
        path = (self.root / Path(relative_path)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"Path resolves outside storage root: {relative_path}")
        return path

    def read_bytes(self, relative_path: str | Path) -> bytes:
        """Read one file as bytes."""
        return self.resolve(relative_path).read_bytes()

    def read_text(self, relative_path: str | Path) -> str:
        """Read one UTF-8 text file."""
        return self.resolve(relative_path).read_text(encoding="utf-8")

    def write_bytes(self, relative_path: str | Path, content: bytes) -> str:
        """Write bytes to one file."""
        path = self.resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return str(path)

    def write_text(self, relative_path: str | Path, content: str) -> str:
        """Write UTF-8 text to one file."""
        path = self.resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return str(path)

    def append_text(self, relative_path: str | Path, content: str) -> None:
        """Append UTF-8 text to one file."""
        path = self.resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(content)

    def mkdir(self, relative_path: str | Path, *, exist_ok: bool = True) -> None:
        """Create one directory."""
        self.resolve(relative_path).mkdir(parents=True, exist_ok=exist_ok)

    def remove(self, relative_path: str | Path, *, recursive: bool = False) -> None:
        """Remove one file or directory."""
        path = self.resolve(relative_path)
        if path.is_dir():
            if recursive:
                shutil.rmtree(path)
            else:
                path.rmdir()
            return
        path.unlink()

    def move(self, old_path: str | Path, new_path: str | Path) -> None:
        """Move one file or directory."""
        source = self.resolve(old_path)
        target = self.resolve(new_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))

    def create_temp_path(self) -> str:
        """Return a new temp directory path relative to storage root."""
        return f".tmp/{uuid4().hex}"

    def stat(self, relative_path: str | Path) -> dict[str, Any]:
        """Return file metadata."""
        return file_stat(self.resolve(relative_path))

    def list(self, relative_path: str | Path = "") -> list[dict[str, Any]]:
        """List one directory."""
        path = self.resolve(relative_path)
        return [
            file_stat(child)
            for child in sorted(path.iterdir(), key=lambda item: item.name)
        ]

    def walk(self, relative_path: str | Path = "") -> list[Path]:
        """Return all paths under a directory."""
        return sorted(self.resolve(relative_path).rglob("*"))

    def grep(
        self,
        relative_path: str | Path,
        pattern: str,
        *,
        recursive: bool = True,
        case_insensitive: bool = False,
    ) -> GrepResult:
        """Search text files under one path."""
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return GrepResult(error=str(exc))

        root = self.resolve(relative_path)
        candidates = self._grep_candidates(root, recursive=recursive)
        result = GrepResult()
        for path in candidates:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as file:
                    for line_number, line in enumerate(file, start=1):
                        if compiled.search(line):
                            result.matches.append(
                                GrepMatch(
                                    file=self.relative(path),
                                    line=line_number,
                                    content=line.rstrip("\n"),
                                )
                            )
            except OSError:
                continue
        return result

    def relative(self, path: Path) -> str:
        """Return a path relative to the storage root."""
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError(f"Path is outside storage root: {path}")
        return resolved.relative_to(self.root).as_posix()

    @staticmethod
    def _grep_candidates(root: Path, *, recursive: bool) -> list[Path]:
        """Return files searched by grep."""
        if root.is_file():
            return [root]
        if recursive:
            return [path for path in root.rglob("*") if path.is_file()]
        return [path for path in root.iterdir() if path.is_file()]


def file_stat(path: Path) -> dict[str, Any]:
    """Build file metadata for list/stat results."""
    stat = path.stat()
    return {
        "name": path.name,
        "size": 0 if path.is_dir() else stat.st_size,
        "mode": stat.st_mode,
        "modTime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "isDir": path.is_dir(),
        "meta": {},
    }
