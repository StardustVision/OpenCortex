# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# Ported from OpenViking (https://github.com/volcengine/openviking)
# SPDX-License-Identifier: Apache-2.0
"""
LocalAGFS: Local filesystem adapter replacing pyagfs.AGFSClient.

Provides the same interface as AGFSClient but operates directly on the local
filesystem instead of making network calls to an AGFS service.

Path mapping: /local/<path> -> {data_root}/<path>
"""

import os
import re
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


def _modtime_iso(path: str) -> str:
    """Return mtime as ISO 8601 UTC string."""
    mtime = os.path.getmtime(path)
    dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _file_mode(path: str) -> int:
    """Return file mode as integer."""
    try:
        return os.stat(path).st_mode
    except OSError:
        return 0


def _entry_dict(full_path: str, name: str) -> Dict[str, Any]:
    """Build an AGFS-style entry dict for a single filesystem entry."""
    is_dir = os.path.isdir(full_path)
    try:
        size = 0 if is_dir else os.path.getsize(full_path)
    except OSError:
        size = 0
    return {
        "name": name,
        "size": size,
        "mode": _file_mode(full_path),
        "modTime": _modtime_iso(full_path),
        "isDir": is_dir,
        "meta": {},
    }


class LocalAGFS:
    """
    Local filesystem adapter that replaces pyagfs.AGFSClient.

    All paths use the /local/<path> convention from CortexFS, which is mapped
    to {data_root}/<path> on the real filesystem.

    Args:
        data_root: Root directory for all data. Defaults to "./data".
    """

    def __init__(self, data_root: str = "./data"):
        self.data_root = os.path.abspath(data_root)
        os.makedirs(self.data_root, exist_ok=True)

    # -------------------------------------------------------------------------
    # Internal path helpers
    # -------------------------------------------------------------------------

    def _resolve(self, path: str) -> str:
        """Translate a /local/<...> path to an absolute filesystem path.

        /local/user/memories  ->  {data_root}/user/memories
        /local               ->  {data_root}
        """
        if path.startswith("/local"):
            remainder = path[len("/local"):]
        else:
            # Fallback: treat as relative within data_root
            remainder = path

        # Strip leading slash from remainder
        remainder = remainder.lstrip("/")

        if remainder:
            resolved = os.path.join(self.data_root, remainder)
        else:
            resolved = self.data_root

        return os.path.normpath(resolved)

    def _safe_resolve(self, path: str) -> str:
        """Resolve path and ensure it stays within data_root (path traversal guard)."""
        resolved = self._resolve(path)
        if not resolved.startswith(self.data_root):
            raise ValueError(
                f"Path '{path}' resolves outside data_root '{self.data_root}'"
            )
        return resolved

    # -------------------------------------------------------------------------
    # Core AGFS-compatible methods
    # -------------------------------------------------------------------------

    def read(self, path: str, offset: int = 0, size: int = -1) -> bytes:
        """Read file contents.

        Args:
            path: /local/... path
            offset: Byte offset to start reading from (default 0)
            size: Number of bytes to read; -1 means read to end (default -1)

        Returns:
            File contents as bytes.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        fspath = self._safe_resolve(path)
        if not os.path.isfile(fspath):
            raise FileNotFoundError(f"No such file: {path!r} (resolved: {fspath!r})")
        with open(fspath, "rb") as f:
            if offset:
                f.seek(offset)
            if size == -1:
                return f.read()
            return f.read(size)

    def write(self, path: str, data: bytes) -> str:
        """Write data to a file, creating parent directories as needed.

        Args:
            path: /local/... path
            data: Bytes to write

        Returns:
            The resolved filesystem path of the written file.
        """
        fspath = self._safe_resolve(path)
        os.makedirs(os.path.dirname(fspath), exist_ok=True)
        with open(fspath, "wb") as f:
            f.write(data)
        return fspath

    def mkdir(self, path: str) -> None:
        """Create a directory (and all missing parents).

        Args:
            path: /local/... path

        Raises:
            FileExistsError: If path already exists and is not a directory.
        """
        fspath = self._safe_resolve(path)
        if os.path.isfile(fspath):
            raise FileExistsError(f"Path exists as file: {path!r}")
        os.makedirs(fspath, exist_ok=True)

    def rm(self, path: str, recursive: bool = False) -> Dict[str, Any]:
        """Remove a file or directory.

        Args:
            path: /local/... path
            recursive: If True, remove directory tree recursively.

        Returns:
            Dict with "path" and "removed" keys.

        Raises:
            FileNotFoundError: If path does not exist.
        """
        fspath = self._safe_resolve(path)
        if not os.path.exists(fspath):
            raise FileNotFoundError(f"No such file or directory: {path!r}")

        if os.path.isdir(fspath):
            if recursive:
                shutil.rmtree(fspath)
            else:
                os.rmdir(fspath)  # raises OSError if not empty
        else:
            os.remove(fspath)

        return {"path": path, "removed": True}

    def mv(self, old_path: str, new_path: str) -> Dict[str, Any]:
        """Move/rename a file or directory.

        Args:
            old_path: Source /local/... path
            new_path: Destination /local/... path

        Returns:
            Dict with "from", "to", and "moved" keys.

        Raises:
            FileNotFoundError: If old_path does not exist.
        """
        old_fspath = self._safe_resolve(old_path)
        new_fspath = self._safe_resolve(new_path)

        if not os.path.exists(old_fspath):
            raise FileNotFoundError(f"No such file or directory: {old_path!r}")

        os.makedirs(os.path.dirname(new_fspath), exist_ok=True)
        shutil.move(old_fspath, new_fspath)
        return {"from": old_path, "to": new_path, "moved": True}

    def ls(self, path: str) -> List[Dict[str, Any]]:
        """List directory contents.

        Args:
            path: /local/... path

        Returns:
            List of entry dicts, each with keys: name, size, mode, modTime, isDir, meta.

        Raises:
            FileNotFoundError: If path does not exist or is not a directory.
        """
        fspath = self._safe_resolve(path)
        if not os.path.exists(fspath):
            raise FileNotFoundError(f"No such directory: {path!r}")
        if not os.path.isdir(fspath):
            raise NotADirectoryError(f"Not a directory: {path!r}")

        entries = []
        try:
            names = os.listdir(fspath)
        except PermissionError as e:
            raise PermissionError(f"Permission denied: {path!r}") from e

        for name in sorted(names):
            full = os.path.join(fspath, name)
            entries.append(_entry_dict(full, name))

        return entries

    def stat(self, path: str) -> Dict[str, Any]:
        """Return metadata for a file or directory.

        Args:
            path: /local/... path

        Returns:
            Dict with keys: name, size, mode, modTime, isDir, meta.

        Raises:
            FileNotFoundError: If path does not exist.
        """
        fspath = self._safe_resolve(path)
        if not os.path.exists(fspath):
            raise FileNotFoundError(f"No such file or directory: {path!r}")
        name = os.path.basename(fspath)
        return _entry_dict(fspath, name)

    def grep(
        self,
        path: str,
        pattern: str,
        recursive: bool = False,
        case_insensitive: bool = False,
    ) -> Dict[str, Any]:
        """Search for pattern in file(s).

        Mirrors the structure returned by AGFSClient.grep:
        {
            "matches": [
                {"file": "/local/...", "line": <line_number>, "content": "<matched line>"},
                ...
            ],
            "count": <total_match_count>
        }

        Args:
            path: /local/... path (file or directory)
            pattern: Regular expression pattern to search for
            recursive: If True, search recursively in directories
            case_insensitive: If True, perform case-insensitive matching

        Returns:
            Dict with "matches" list and "count" integer.
        """
        fspath = self._safe_resolve(path)
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error as e:
            return {"matches": [], "count": 0, "error": str(e)}

        matches: List[Dict[str, Any]] = []

        def _search_file(file_fspath: str, local_path: str) -> None:
            try:
                with open(file_fspath, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, start=1):
                        if compiled.search(line):
                            matches.append(
                                {
                                    "file": local_path,
                                    "line": lineno,
                                    "content": line.rstrip("\n"),
                                }
                            )
            except (OSError, UnicodeDecodeError):
                pass

        def _local_path(full: str) -> str:
            """Convert resolved filesystem path back to /local/... form."""
            rel = os.path.relpath(full, self.data_root)
            return "/local/" + rel.replace(os.sep, "/")

        if os.path.isfile(fspath):
            _search_file(fspath, _local_path(fspath))
        elif os.path.isdir(fspath):
            if recursive:
                for dirpath, dirnames, filenames in os.walk(fspath):
                    dirnames.sort()
                    for fname in sorted(filenames):
                        full = os.path.join(dirpath, fname)
                        _search_file(full, _local_path(full))
            else:
                for fname in sorted(os.listdir(fspath)):
                    full = os.path.join(fspath, fname)
                    if os.path.isfile(full):
                        _search_file(full, _local_path(full))

        return {"matches": matches, "count": len(matches)}
