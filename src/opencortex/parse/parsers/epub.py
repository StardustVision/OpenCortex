"""EPUB parser — extracts text from EPUB, delegates to MarkdownParser."""

import logging
import re
from pathlib import Path
from typing import List, Optional, Union

from opencortex.parse.base import ParsedChunk, lazy_import
from opencortex.parse.parsers.base_parser import BaseParser

logger = logging.getLogger(__name__)


class EPubParser(BaseParser):
    @property
    def supported_extensions(self) -> List[str]:
        return [".epub"]

    async def parse(self, source: Union[str, Path], **kwargs) -> List[ParsedChunk]:
        ebooklib = lazy_import("ebooklib")
        epub = lazy_import("ebooklib.epub")
        book = epub.read_epub(str(source))
        md_parts = []
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            html_content = item.get_body_content().decode("utf-8", errors="replace")
            text = self._html_to_text(html_content)
            if text.strip():
                md_parts.append(text)
        content = "\n\n".join(md_parts)
        return await self.parse_content(content, source_path=str(source), **kwargs)

    async def parse_content(
        self, content: str, source_path: Optional[str] = None, **kwargs
    ) -> List[ParsedChunk]:
        from opencortex.parse.parsers.markdown import MarkdownParser

        md_parser = MarkdownParser()
        chunks = await md_parser.parse_content(content, source_path=source_path, **kwargs)
        for chunk in chunks:
            chunk.source_format = "epub"
        return chunks

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Simple HTML to text conversion."""
        text = re.sub(r"<h([1-6])[^>]*>(.*?)</h\1>", lambda m: "#" * int(m.group(1)) + " " + m.group(2), html)
        text = re.sub(r"<p[^>]*>(.*?)</p>", r"\1\n\n", text)
        text = re.sub(r"<br\s*/?>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        return text.strip()
