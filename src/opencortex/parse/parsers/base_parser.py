"""Abstract base class for document parsers."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Union

from opencortex.parse.base import ParsedChunk


class BaseParser(ABC):
    """Abstract parser that converts documents into ParsedChunk lists."""

    @abstractmethod
    async def parse(self, source: Union[str, Path], **kwargs) -> List[ParsedChunk]:
        """Parse a document from file path or content string."""
        ...

    @abstractmethod
    async def parse_content(
        self, content: str, source_path: Optional[str] = None, **kwargs
    ) -> List[ParsedChunk]:
        """Parse document content directly."""
        ...

    @property
    @abstractmethod
    def supported_extensions(self) -> List[str]:
        """List of supported file extensions."""
        ...

    def can_parse(self, path: Union[str, Path]) -> bool:
        """Check if this parser can handle the given file."""
        return Path(path).suffix.lower() in self.supported_extensions

    def _read_file(self, path: Union[str, Path]) -> str:
        """Read file content with encoding detection."""
        path = Path(path)
        for encoding in ["utf-8", "utf-8-sig", "latin-1", "cp1252"]:
            try:
                return path.read_text(encoding=encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError(f"Unable to decode file: {path}")
