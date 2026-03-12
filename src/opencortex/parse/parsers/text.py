"""Plain text parser — delegates to MarkdownParser."""

import logging
from pathlib import Path
from typing import List, Optional, Union

from opencortex.parse.base import ParsedChunk
from opencortex.parse.parsers.base_parser import BaseParser

logger = logging.getLogger(__name__)


class TextParser(BaseParser):
    @property
    def supported_extensions(self) -> List[str]:
        return [
            ".txt", ".text", ".log", ".csv", ".tsv", ".ini", ".cfg", ".conf",
            ".yaml", ".yml", ".toml", ".json", ".xml", ".html", ".htm",
            ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp",
            ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".zsh",
            ".css", ".scss", ".less", ".sql", ".r", ".m", ".swift",
        ]

    async def parse(self, source: Union[str, Path], **kwargs) -> List[ParsedChunk]:
        content = self._read_file(source)
        return await self.parse_content(content, source_path=str(source), **kwargs)

    async def parse_content(
        self, content: str, source_path: Optional[str] = None, **kwargs
    ) -> List[ParsedChunk]:
        from opencortex.parse.parsers.markdown import MarkdownParser

        md_parser = MarkdownParser()
        chunks = await md_parser.parse_content(content, source_path=source_path, **kwargs)
        for chunk in chunks:
            chunk.source_format = "text"
        return chunks
