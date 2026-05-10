# SPDX-License-Identifier: Apache-2.0
"""Document parser integration and tree primary-record writes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from opencortex.core.identity import IdentityProfile
from opencortex.parse.registry import ParserRegistry
from opencortex.storage.namespace import slug
from opencortex.store.common import extract_category_from_uri
from opencortex.store.event.events import StoreEvents
from opencortex.store.schemas import (
    Context,
    PrimaryRecordInput,
    RawPrimaryRecord,
    StoredRecord,
)
from opencortex.store.types import ContextType
from opencortex.store.writer.primary_record_writer import PrimaryRecordWriter


class DocumentChunk(BaseModel):
    """Parser output normalized for opencortex writers."""

    content: str
    title: str
    level: int = 0
    parent_index: int = -1
    source_format: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_parsed(cls, parsed: Any) -> "DocumentChunk":
        """Build a normalized chunk from parser output."""
        return cls(
            content=str(getattr(parsed, "content", "") or ""),
            title=str(getattr(parsed, "title", "") or ""),
            level=int(getattr(parsed, "level", 0) or 0),
            parent_index=int(getattr(parsed, "parent_index", -1) or -1),
            source_format=str(getattr(parsed, "source_format", "") or ""),
            meta=dict(getattr(parsed, "meta", {}) or {}),
        )


class DocumentParser:
    """Thin adapter around the parser registry used by document ingestion."""

    format_aliases = {
        "md": "markdown",
        "markdown": "markdown",
        "text": "text",
        "txt": "text",
        "plain": "text",
        "plain/text": "text",
        "text/plain": "text",
        "pdf": "pdf",
        "application/pdf": "pdf",
        "doc": "word",
        "docx": "word",
        "word": "word",
        "application/msword": "word",
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ): "word",
        "xls": "excel",
        "xlsx": "excel",
        "excel": "excel",
        "spreadsheet": "excel",
        "application/vnd.ms-excel": "excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "excel",
        "ppt": "powerpoint",
        "pptx": "powerpoint",
        "powerpoint": "powerpoint",
        "presentation": "powerpoint",
        "application/vnd.ms-powerpoint": "powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
            "powerpoint"
        ),
        "epub": "epub",
        "application/epub+zip": "epub",
    }

    def __init__(self, registry: ParserRegistry | None = None) -> None:
        self.registry = registry or ParserRegistry()

    async def parse(
        self,
        *,
        content: str,
        source_path: str = "",
        source_format: str = "markdown",
    ) -> list[DocumentChunk]:
        """Parse content into normalized non-empty chunks."""
        normalized_format = self.normalize_format(
            source_format=source_format,
            source_path=source_path,
        )
        parser = self.parser_for(
            source_path=source_path, source_format=normalized_format
        )
        if parser is not None:
            parsed_chunks = await parser.parse_content(content, source_path=source_path)
        else:
            parsed_chunks = await self.registry.parse_content(
                content,
                source_format=normalized_format,
                source_path=source_path or None,
            )
        return [
            DocumentChunk.from_parsed(chunk)
            for chunk in parsed_chunks
            if str(getattr(chunk, "content", "") or "").strip()
        ]

    def parser_for(self, *, source_path: str, source_format: str) -> Any | None:
        """Return the parser selected by path extension or explicit format."""
        if source_path:
            parser = self.registry.get_parser_for_file(source_path)
            if parser is not None:
                return parser
        parser_name = self.format_aliases.get(
            source_format.strip().lower(), source_format
        )
        return getattr(self.registry, "_parsers", {}).get(parser_name)

    def normalize_format(self, *, source_format: str, source_path: str) -> str:
        """Return a parser format name from explicit format, MIME type, or extension."""
        explicit = source_format.strip().lower()
        if explicit:
            return self.format_aliases.get(explicit, explicit)
        extension = Path(source_path).suffix.lower().lstrip(".")
        if extension:
            return self.format_aliases.get(extension, extension)
        return "markdown"

    @staticmethod
    def is_tree(chunks: list[DocumentChunk]) -> bool:
        """Return whether parsed chunks should be written as a tree."""
        return (
            len(chunks) > 1
            or any(chunk.parent_index >= 0 for chunk in chunks)
            or any(chunk.meta.get("section_path") for chunk in chunks)
        )


class DocumentTreeWriter:
    """Write parsed document chunks as primary records under a root URI."""

    def __init__(
        self,
        *,
        parser: DocumentParser,
        writer: PrimaryRecordWriter,
        events: StoreEvents,
    ) -> None:
        self.parser = parser
        self.writer = writer
        self.events = events

    async def write_children(
        self,
        *,
        root_input: PrimaryRecordInput,
        source_path: str = "",
        source_format: str = "markdown",
        chunk_role: str = "document_section",
        chunks: list[DocumentChunk] | None = None,
    ) -> list[StoredRecord]:
        """Parse root content and write child records when it forms a tree."""
        chunks = chunks or await self.parser.parse(
            content=root_input.content,
            source_path=source_path,
            source_format=source_format,
        )
        if not self.parser.is_tree(chunks):
            return []

        uris = self.chunk_uris(root_input.ctx.uri, chunks)
        child_parent_indexes = {chunk.parent_index for chunk in chunks}
        written: list[StoredRecord] = []
        for index, chunk in enumerate(chunks):
            record_input = self.chunk_record_input(
                root_input=root_input,
                chunk=chunk,
                chunk_index=index,
                uri=uris[index],
                parent_uri=(
                    uris[chunk.parent_index]
                    if chunk.parent_index >= 0
                    else root_input.ctx.uri
                ),
                is_leaf=index not in child_parent_indexes,
                source_path=source_path,
                chunk_role=chunk_role,
            )
            stored = await self.writer.write(record_input)
            self.events.memory_stored(record_input, stored)
            written.append(stored)
        return written

    def chunk_record_input(
        self,
        *,
        root_input: PrimaryRecordInput,
        chunk: DocumentChunk,
        chunk_index: int,
        uri: str,
        parent_uri: str,
        is_leaf: bool,
        source_path: str,
        chunk_role: str,
    ) -> PrimaryRecordInput:
        """Build one primary-record input for a parsed chunk."""
        root_ctx = root_input.ctx
        context_type = (
            root_ctx.context_type
            if isinstance(root_ctx.context_type, ContextType)
            else ContextType(str(root_ctx.context_type))
        )
        meta = self.chunk_meta(
            root_meta=dict(root_ctx.meta),
            chunk=chunk,
            root_uri=root_ctx.uri,
            chunk_index=chunk_index,
            source_path=source_path,
            chunk_role=chunk_role,
        )
        ctx = Context(
            uri=uri,
            parent_uri=parent_uri,
            is_leaf=is_leaf,
            context_type=context_type,
            category=root_ctx.category or extract_category_from_uri(root_ctx.uri),
            related_uri=[],
            meta=meta,
            session_id=root_input.session_id,
            profile=root_ctx.profile,
        )
        raw_record = RawPrimaryRecord.from_context(
            ctx=ctx,
            content=chunk.content,
            effective_category=ctx.category or extract_category_from_uri(ctx.uri),
            tenant_id=root_input.tenant_id,
            user_id=root_input.user_id,
            project_id=str(meta.get("project_id", "") or "public"),
            session_id=root_input.session_id,
            meta=meta,
            ttl_expires_at=str(root_input.payload.get("ttl_expires_at", "") or ""),
        )
        return PrimaryRecordInput(
            ctx=ctx,
            payload=raw_record.model_dump(mode="json"),
            effective_category=raw_record.category,
            meta=meta,
            context_type=context_type,
            session_id=root_input.session_id,
            tenant_id=root_input.tenant_id,
            user_id=root_input.user_id,
            content=chunk.content,
        )

    @staticmethod
    def chunk_meta(
        *,
        root_meta: dict[str, Any],
        chunk: DocumentChunk,
        root_uri: str,
        chunk_index: int,
        source_path: str,
        chunk_role: str,
    ) -> dict[str, Any]:
        """Return metadata inherited by a parsed chunk."""
        meta = dict(root_meta)
        section_path = str(chunk.meta.get("section_path", "") or chunk.title)
        meta.update(
            {
                "ingest_mode": "document_tree",
                "source_uri": root_uri,
                "tree_root_uri": root_uri,
                "source_section_path": section_path,
                "section_title": chunk.title,
                "section_level": chunk.level,
                "section_index": chunk_index,
                "source_format": chunk.source_format,
                "chunk_role": chunk_role,
            }
        )
        if source_path:
            meta.setdefault("source_path", source_path)
            meta.setdefault("file_path", source_path)
        return meta

    @staticmethod
    def chunk_uris(root_uri: str, chunks: list[DocumentChunk]) -> list[str]:
        """Return stable child URIs for parsed chunks."""
        used: set[str] = set()
        uris: list[str] = []
        for index, chunk in enumerate(chunks):
            base = slug(chunk.title) or f"section-{index + 1}"
            candidate = f"{root_uri.rstrip('/')}/{index + 1:04d}-{base}"
            while candidate in used:
                candidate = f"{candidate}-{len(used) + 1}"
            used.add(candidate)
            uris.append(candidate)
        return uris


def should_parse_session_tree(content: str) -> bool:
    """Return whether session final content looks structured enough to parse."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return any(line.startswith("#") for line in lines)


def document_root_meta(
    *,
    meta: dict[str, Any],
    profile: IdentityProfile,
    root_uri: str,
    chunk_role: str,
) -> dict[str, Any]:
    """Return root metadata for a document tree."""
    result = dict(meta)
    result["project_id"] = profile.project_id
    result.setdefault("ingest_mode", "document_tree")
    result.setdefault("tree_root_uri", root_uri)
    result["chunk_role"] = chunk_role
    return result
