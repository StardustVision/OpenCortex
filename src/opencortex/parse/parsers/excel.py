"""Excel (.xlsx) parser — converts to markdown tables, delegates to MarkdownParser."""

import logging
from pathlib import Path
from typing import List, Optional, Union

from opencortex.parse.base import ParsedChunk, format_table_to_markdown, lazy_import
from opencortex.parse.parsers.base_parser import BaseParser

logger = logging.getLogger(__name__)


class ExcelParser(BaseParser):
    @property
    def supported_extensions(self) -> List[str]:
        return [".xlsx", ".xls"]

    async def parse(self, source: Union[str, Path], **kwargs) -> List[ParsedChunk]:
        openpyxl = lazy_import("openpyxl")
        wb = openpyxl.load_workbook(str(source), read_only=True, data_only=True)
        md_parts = []
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            rows = []
            for row in ws.iter_rows(values_only=True):
                rows.append([str(cell) if cell is not None else "" for cell in row])
            if rows:
                md_parts.append(f"## {sheet}\n\n{format_table_to_markdown(rows)}")
        wb.close()
        content = "\n\n".join(md_parts)
        return await self.parse_content(content, source_path=str(source), **kwargs)

    async def parse_content(
        self, content: str, source_path: Optional[str] = None, **kwargs
    ) -> List[ParsedChunk]:
        from opencortex.parse.parsers.markdown import MarkdownParser

        md_parser = MarkdownParser()
        chunks = await md_parser.parse_content(content, source_path=source_path, **kwargs)
        for chunk in chunks:
            chunk.source_format = "xlsx"
        return chunks
