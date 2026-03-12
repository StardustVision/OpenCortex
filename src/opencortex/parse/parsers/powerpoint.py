"""PowerPoint (.pptx) parser — converts to markdown, delegates to MarkdownParser."""

import logging
from pathlib import Path
from typing import List, Optional, Union

from opencortex.parse.base import ParsedChunk, lazy_import
from opencortex.parse.parsers.base_parser import BaseParser

logger = logging.getLogger(__name__)


class PowerPointParser(BaseParser):
    @property
    def supported_extensions(self) -> List[str]:
        return [".pptx", ".ppt"]

    async def parse(self, source: Union[str, Path], **kwargs) -> List[ParsedChunk]:
        pptx = lazy_import("pptx", "python-pptx")
        prs = pptx.Presentation(str(source))
        md_parts = []
        for slide_num, slide in enumerate(prs.slides, 1):
            texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        text = para.text.strip()
                        if text:
                            texts.append(text)
            if texts:
                title = texts[0]
                body = "\n\n".join(texts[1:])
                md_parts.append(f"## Slide {slide_num}: {title}\n\n{body}")
        content = "\n\n".join(md_parts)
        return await self.parse_content(content, source_path=str(source), **kwargs)

    async def parse_content(
        self, content: str, source_path: Optional[str] = None, **kwargs
    ) -> List[ParsedChunk]:
        from opencortex.parse.parsers.markdown import MarkdownParser

        md_parser = MarkdownParser()
        chunks = await md_parser.parse_content(content, source_path=source_path, **kwargs)
        for chunk in chunks:
            chunk.source_format = "pptx"
        return chunks
