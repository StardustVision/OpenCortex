"""Base types and utilities for the OpenCortex parser subsystem."""

import importlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ParsedChunk:
    """Output unit from any parser.

    Represents a single chunk of content with hierarchy metadata.
    The orchestrator writes each chunk to CortexFS (L2) + Qdrant.
    """

    content: str
    title: str
    level: int
    parent_index: int
    source_format: str
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParserConfig:
    """Configuration for parser chunking behavior."""

    max_section_size: int = 1024  # max tokens per chunk
    min_section_tokens: int = 512  # below this, merge with adjacent


def format_table_to_markdown(rows: List[List[str]], has_header: bool = True) -> str:
    """Format table data as a Markdown table."""
    if not rows:
        return ""
    col_count = max(len(row) for row in rows)
    col_widths = [0] * col_count
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))
    lines = []
    for row_idx, row in enumerate(rows):
        padded = list(row) + [""] * (col_count - len(row))
        cells = [str(cell).ljust(col_widths[i]) for i, cell in enumerate(padded)]
        lines.append("| " + " | ".join(cells) + " |")
        if row_idx == 0 and has_header and len(rows) > 1:
            separator = ["-" * w for w in col_widths]
            lines.append("| " + " | ".join(separator) + " |")
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """Estimate token count (CJK chars * 0.7 + other * 0.3)."""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cjk
    return int(cjk * 0.7 + other * 0.3)


def lazy_import(module_name: str, package_name: Optional[str] = None) -> Any:
    """Import a module lazily, raising ImportError with install hint if missing."""
    try:
        return importlib.import_module(module_name)
    except ImportError:
        pkg = package_name or module_name
        raise ImportError(
            f"Module '{module_name}' not available. Install: pip install {pkg}"
        )
