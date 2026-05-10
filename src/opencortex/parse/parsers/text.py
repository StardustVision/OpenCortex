"""Plain text parser — delegates to MarkdownParser."""

import logging
from pathlib import Path
from typing import Any, List, Optional, Union

from opencortex.parse.base import ParsedChunk
from opencortex.parse.parsers.base_parser import BaseParser

logger = logging.getLogger(__name__)


class TextParser(BaseParser):
    """Parser for plain text and source-like files."""

    @property
    def supported_extensions(self) -> List[str]:
        """Return file extensions handled as plain text."""
        return [
            ".txt",
            ".text",
            ".log",
            ".csv",
            ".tsv",
            ".ini",
            ".cfg",
            ".conf",
            ".yaml",
            ".yml",
            ".toml",
            ".json",
            ".xml",
            ".html",
            ".htm",
            ".py",
            ".js",
            ".ts",
            ".java",
            ".c",
            ".cpp",
            ".h",
            ".hpp",
            ".go",
            ".rs",
            ".rb",
            ".php",
            ".sh",
            ".bash",
            ".zsh",
            ".css",
            ".scss",
            ".less",
            ".sql",
            ".r",
            ".m",
            ".swift",
        ]

    async def parse(self, source: Union[str, Path], **kwargs: Any) -> List[ParsedChunk]:
        """Parse a plain-text file path into document chunks."""
        content = self._read_file(source)
        return await self.parse_content(content, source_path=str(source), **kwargs)

    async def parse_content(
        self, content: str, source_path: Optional[str] = None, **kwargs: Any
    ) -> List[ParsedChunk]:
        """Parse plain text content through the markdown chunker."""
        from opencortex.parse.parsers.markdown import MarkdownParser

        md_parser = MarkdownParser()
        chunks = await md_parser.parse_content(
            content, source_path=source_path, **kwargs
        )
        for chunk in chunks:
            chunk.source_format = "text"
        return chunks
