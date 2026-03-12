"""Parser registry for extension-based dispatch."""

import importlib
import logging
from pathlib import Path
from typing import Dict, List, Optional, Union

from opencortex.parse.base import ParsedChunk
from opencortex.parse.parsers.base_parser import BaseParser

logger = logging.getLogger(__name__)


class ParserRegistry:
    """Registry that maps file extensions to parser instances."""

    def __init__(self):
        self._parsers: Dict[str, BaseParser] = {}
        self._extension_map: Dict[str, str] = {}
        self._register_defaults()

    def _register_defaults(self):
        from opencortex.parse.parsers.text import TextParser
        from opencortex.parse.parsers.markdown import MarkdownParser

        self.register("text", TextParser())
        self.register("markdown", MarkdownParser())

        for name, cls_path in [
            ("word", "opencortex.parse.parsers.word.WordParser"),
            ("excel", "opencortex.parse.parsers.excel.ExcelParser"),
            ("powerpoint", "opencortex.parse.parsers.powerpoint.PowerPointParser"),
            ("pdf", "opencortex.parse.parsers.pdf.PDFParser"),
            ("epub", "opencortex.parse.parsers.epub.EPubParser"),
        ]:
            try:
                module_name, class_name = cls_path.rsplit(".", 1)
                mod = importlib.import_module(module_name)
                cls = getattr(mod, class_name)
                self.register(name, cls())
            except (ImportError, AttributeError) as e:
                logger.debug("Optional parser '%s' not available: %s", name, e)

    def register(self, name: str, parser: BaseParser) -> None:
        self._parsers[name] = parser
        for ext in parser.supported_extensions:
            self._extension_map[ext.lower()] = name

    def get_parser_for_file(self, path: Union[str, Path]) -> Optional[BaseParser]:
        ext = Path(path).suffix.lower()
        name = self._extension_map.get(ext)
        return self._parsers.get(name) if name else None

    async def parse_content(
        self, content: str, source_format: str = "text", **kwargs
    ) -> List[ParsedChunk]:
        """Parse content string using appropriate parser."""
        format_map = {
            "markdown": "markdown", "md": "markdown",
            "text": "text", "txt": "text",
        }
        parser_name = format_map.get(source_format, "text")
        parser = self._parsers.get(parser_name, self._parsers.get("text"))
        if parser:
            return await parser.parse_content(content, **kwargs)
        return []

    def list_supported_extensions(self) -> List[str]:
        return list(self._extension_map.keys())
