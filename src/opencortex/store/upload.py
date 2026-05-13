# SPDX-License-Identifier: Apache-2.0
"""Temporary upload storage for large resource ingestion."""

from __future__ import annotations

import json
import os
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from opencortex.storage.cfs import CFS

INLINE_STORE_MAX_BYTES = 5 * 1024 * 1024
TEMP_UPLOAD_MAX_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class TempUpload:
    """Resolved temporary upload content."""

    upload_id: str
    content: str
    source_path: str
    title: str
    source_format: str
    content_type: str
    size_bytes: int


class TempUploadStore:
    """Store upload bytes under CFS until a resource import consumes them."""

    upload_root = "_system/uploads"

    def __init__(self, *, cfs: CFS) -> None:
        self.cfs = cfs

    def save(
        self,
        content: bytes,
        *,
        filename: str = "",
        content_type: str = "",
        source_format: str = "",
    ) -> dict[str, Any]:
        """Persist one temporary upload and return its public metadata."""
        size_bytes = len(content)
        if size_bytes > TEMP_UPLOAD_MAX_BYTES:
            raise ValueError(
                f"upload exceeds {TEMP_UPLOAD_MAX_BYTES} bytes; split the file first"
            )
        upload_id = uuid4().hex
        safe_name = safe_filename(filename) or f"{upload_id}.txt"
        base = f"{self.upload_root}/{upload_id}"
        content_path = f"{base}/content"
        meta_path = f"{base}/meta.json"
        self.cfs.write_bytes(content_path, content)
        meta = {
            "upload_id": upload_id,
            "filename": safe_name,
            "content_type": content_type,
            "source_format": source_format or Path(safe_name).suffix.lstrip("."),
            "size_bytes": size_bytes,
            "created_at": int(time.time()),
            "content_path": content_path,
        }
        self.cfs.write_text(meta_path, json.dumps(meta, ensure_ascii=False))
        return dict(meta)

    def resolve(self, upload_id: str) -> TempUpload:
        """Read one upload and decode it as UTF-8 text."""
        clean_id = safe_upload_id(upload_id)
        if not clean_id:
            raise ValueError("upload_id is required")
        base = f"{self.upload_root}/{clean_id}"
        meta = json.loads(self.cfs.read_text(f"{base}/meta.json"))
        content = self.cfs.read_bytes(meta["content_path"]).decode(
            "utf-8",
            errors="replace",
        )
        filename = str(meta.get("filename", "") or "")
        return TempUpload(
            upload_id=clean_id,
            content=content,
            source_path=filename,
            title=Path(filename).stem,
            source_format=str(meta.get("source_format", "") or ""),
            content_type=str(meta.get("content_type", "") or ""),
            size_bytes=int(meta.get("size_bytes", 0) or 0),
        )

    def consume(self, upload_id: str) -> TempUpload:
        """Resolve and remove one temporary upload."""
        upload = self.resolve(upload_id)
        with suppress(FileNotFoundError, NotADirectoryError):
            self.cfs.remove(f"{self.upload_root}/{upload.upload_id}", recursive=True)
        return upload


def safe_upload_id(upload_id: str) -> str:
    """Return a path-safe upload id, or empty for invalid input."""
    value = str(upload_id or "").strip()
    if not value or "/" in value or "\\" in value or value in {".", ".."}:
        return ""
    if not all(character.isalnum() or character in {"-", "_"} for character in value):
        return ""
    return value


def safe_filename(filename: str) -> str:
    """Return a basename-only upload filename."""
    value = os.path.basename(str(filename or "").strip())
    if value in {"", ".", ".."}:
        return ""
    return value
