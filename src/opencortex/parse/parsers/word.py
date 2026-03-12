"""Word (.docx) parser — converts to markdown, delegates to MarkdownParser."""

import logging
from pathlib import Path
from typing import List, Optional, Union

from opencortex.parse.base import ParsedChunk, lazy_import
from opencortex.parse.parsers.base_parser import BaseParser

logger = logging.getLogger(__name__)


class WordParser(BaseParser):
    @property
    def supported_extensions(self) -> List[str]:
        return [".docx", ".doc"]

    async def parse(self, source: Union[str, Path], **kwargs) -> List[ParsedChunk]:
        docx = lazy_import("docx", "python-docx")
        doc = docx.Document(str(source))
        md_lines = []
        for para in doc.paragraphs:
            style = para.style.name if para.style else ""
            text = para.text.strip()
            if not text:
                continue
            if "Heading 1" in style:
                md_lines.append(f"# {text}")
            elif "Heading 2" in style:
                md_lines.append(f"## {text}")
            elif "Heading 3" in style:
                md_lines.append(f"### {text}")
            elif "Heading" in style:
                md_lines.append(f"#### {text}")
            else:
                md_lines.append(text)
            md_lines.append("")
        content = "\n".join(md_lines)
        return await self.parse_content(content, source_path=str(source), **kwargs)

    async def parse_content(
        self, content: str, source_path: Optional[str] = None, **kwargs
    ) -> List[ParsedChunk]:
        from opencortex.parse.parsers.markdown import MarkdownParser

        md_parser = MarkdownParser()
        chunks = await md_parser.parse_content(content, source_path=source_path, **kwargs)
        for chunk in chunks:
            chunk.source_format = "docx"
        return chunks
