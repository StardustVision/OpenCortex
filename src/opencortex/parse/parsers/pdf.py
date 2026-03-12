"""PDF parser — converts to markdown via pdfplumber, delegates to MarkdownParser."""

import logging
from pathlib import Path
from typing import List, Optional, Union

from opencortex.parse.base import ParsedChunk, lazy_import
from opencortex.parse.parsers.base_parser import BaseParser

logger = logging.getLogger(__name__)


class PDFParser(BaseParser):
    @property
    def supported_extensions(self) -> List[str]:
        return [".pdf"]

    async def parse(self, source: Union[str, Path], **kwargs) -> List[ParsedChunk]:
        pdfplumber = lazy_import("pdfplumber")
        md_parts = []
        with pdfplumber.open(str(source)) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ""
                if text.strip():
                    md_parts.append(f"## Page {page_num}\n\n{text}")
        content = "\n\n".join(md_parts)
        return await self.parse_content(content, source_path=str(source), **kwargs)

    async def parse_content(
        self, content: str, source_path: Optional[str] = None, **kwargs
    ) -> List[ParsedChunk]:
        from opencortex.parse.parsers.markdown import MarkdownParser

        md_parser = MarkdownParser()
        chunks = await md_parser.parse_content(content, source_path=source_path, **kwargs)
        for chunk in chunks:
            chunk.source_format = "pdf"
        return chunks
