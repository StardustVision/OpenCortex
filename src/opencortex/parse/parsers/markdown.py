"""
Markdown parser for OpenCortex — ported from OpenViking v5.0.

Returns List[ParsedChunk] instead of writing to VikingFS.
All chunking logic preserved: heading detection, section merge, smart split.

Scenarios:
1. Small files (< 4000 tokens) → single ParsedChunk
2. Large files with sections → split by heading hierarchy
3. Small sections (< min_section_tokens) → merged with adjacent
4. Oversized sections without subsections → split by paragraphs
"""

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from opencortex.parse.base import ParsedChunk, ParserConfig, estimate_tokens
from opencortex.parse.parsers.base_parser import BaseParser

logger = logging.getLogger(__name__)

# Default thresholds
_SMALL_DOC_THRESHOLD = 4000  # tokens


class MarkdownParser(BaseParser):
    """Markdown parser that returns ParsedChunk list with hierarchy."""

    MAX_MERGED_FILENAME_LENGTH = 32

    def __init__(
        self,
        extract_frontmatter: bool = True,
        config: Optional[ParserConfig] = None,
    ):
        self.extract_frontmatter = extract_frontmatter
        self.config = config or ParserConfig()
        self._heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
        self._code_block_pattern = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
        self._html_comment_pattern = re.compile(r"<!--.*?-->", re.DOTALL)
        self._indented_code_pattern = re.compile(r"^(?:    |\t).+$", re.MULTILINE)
        self._frontmatter_pattern = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)

    @property
    def supported_extensions(self) -> List[str]:
        return [".md", ".markdown", ".mdown", ".mkd"]

    async def parse(self, source: Union[str, Path], **kwargs) -> List[ParsedChunk]:
        path = Path(source)
        if path.exists():
            content = self._read_file(path)
            return await self.parse_content(content, source_path=str(path), **kwargs)
        return await self.parse_content(str(source), **kwargs)

    async def parse_content(
        self,
        content: str,
        source_path: Optional[str] = None,
        **kwargs,
    ) -> List[ParsedChunk]:
        """Parse markdown content into ParsedChunk list."""
        meta: Dict[str, Any] = {}

        # Extract frontmatter
        if self.extract_frontmatter:
            content, frontmatter = self._extract_frontmatter(content)
            if frontmatter:
                meta["frontmatter"] = frontmatter

        doc_title = meta.get("frontmatter", {}).get(
            "title", Path(source_path).stem if source_path else "Document"
        )

        # Find headings
        headings = self._find_headings(content)
        estimated_tokens = estimate_tokens(content)

        max_size = self.config.max_section_size
        min_size = self.config.min_section_tokens

        # Small document → single chunk
        if estimated_tokens <= _SMALL_DOC_THRESHOLD:
            return [
                ParsedChunk(
                    content=content,
                    title=doc_title,
                    level=0,
                    parent_index=-1,
                    source_format="markdown",
                    meta=meta,
                )
            ]

        # No headings → split by paragraphs
        if not headings:
            parts = self._smart_split_content(content, max_size)
            return [
                ParsedChunk(
                    content=part,
                    title=f"{doc_title} (part {i})" if len(parts) > 1 else doc_title,
                    level=0,
                    parent_index=-1,
                    source_format="markdown",
                )
                for i, part in enumerate(parts, 1)
            ]

        # Build sections and process with merge logic
        chunks: List[ParsedChunk] = []
        sections = self._build_top_level_sections(content, headings)
        self._process_sections_to_chunks(
            content, headings, sections, chunks,
            parent_index=-1, parent_level=0,
            max_size=max_size, min_size=min_size,
        )
        return chunks

    # ========== Heading Detection ==========

    def _find_headings(self, content: str) -> List[Tuple[int, int, str, int]]:
        """Find all headings, excluding code blocks and comments."""
        excluded_ranges = []
        for match in self._code_block_pattern.finditer(content):
            excluded_ranges.append((match.start(), match.end()))
        for match in self._html_comment_pattern.finditer(content):
            excluded_ranges.append((match.start(), match.end()))
        for match in self._indented_code_pattern.finditer(content):
            excluded_ranges.append((match.start(), match.end()))

        headings = []
        for match in self._heading_pattern.finditer(content):
            pos = match.start()
            in_excluded = any(start <= pos < end for start, end in excluded_ranges)
            if in_excluded:
                continue
            if pos > 0 and content[pos - 1] == "\\":
                continue
            level = len(match.group(1))
            title = match.group(2).strip()
            headings.append((match.start(), match.end(), title, level))
        return headings

    # ========== Section Building ==========

    def _build_top_level_sections(
        self, content: str, headings: List[Tuple[int, int, str, int]]
    ) -> List[Dict[str, Any]]:
        """Build section list from top-level headings."""
        sections = []
        min_level = min(h[3] for h in headings)

        # Pre-heading content
        first_heading_start = headings[0][0]
        if first_heading_start > 0:
            pre_content = content[:first_heading_start].strip()
            if pre_content:
                sections.append({
                    "name": "preamble",
                    "content": pre_content,
                    "tokens": estimate_tokens(pre_content),
                    "has_children": False,
                    "heading_idx": None,
                    "level": 0,
                })

        # Top-level headings
        for i, h in enumerate(headings):
            if h[3] == min_level:
                sections.append({"heading_idx": i})

        return sections

    def _get_section_info(
        self, content: str, headings: List[Tuple[int, int, str, int]], idx: int
    ) -> Dict[str, Any]:
        """Get section info including content, tokens, children."""
        start_pos, end_pos, title, level = headings[idx]

        # Find section end
        section_end = len(content)
        next_same_level_idx = len(headings)
        for j in range(idx + 1, len(headings)):
            if headings[j][3] <= level:
                section_end = headings[j][0]
                next_same_level_idx = j
                break

        # Find children
        child_indices = []
        direct_content_end = section_end
        for j in range(idx + 1, next_same_level_idx):
            if headings[j][3] == level + 1:
                if not child_indices:
                    direct_content_end = headings[j][0]
                child_indices.append(j)

        has_children = len(child_indices) > 0
        heading_prefix = "#" * level
        section_start = end_pos
        full_content = f"{heading_prefix} {title}\n\n{content[section_start:section_end].strip()}"
        full_tokens = estimate_tokens(full_content)

        direct_content = ""
        if has_children:
            direct_text = content[section_start:direct_content_end].strip()
            if direct_text:
                direct_content = f"{heading_prefix} {title}\n\n{direct_text}"

        return {
            "name": title,
            "content": full_content,
            "tokens": full_tokens,
            "has_children": has_children,
            "heading_idx": idx,
            "direct_content": direct_content,
            "child_indices": child_indices,
            "level": level,
        }

    # ========== Chunk Processing ==========

    def _process_sections_to_chunks(
        self,
        content: str,
        headings: List[Tuple[int, int, str, int]],
        sections: List[Dict[str, Any]],
        chunks: List[ParsedChunk],
        parent_index: int,
        parent_level: int,
        max_size: int,
        min_size: int,
    ) -> None:
        """Process sections with merge logic, appending to chunks list."""
        # Expand section info
        expanded = []
        for sec in sections:
            if sec.get("heading_idx") is not None and "content" not in sec:
                expanded.append(self._get_section_info(content, headings, sec["heading_idx"]))
            else:
                expanded.append(sec)

        pending: List[Dict[str, Any]] = []

        for sec in expanded:
            tokens = sec["tokens"]
            has_children = sec.get("has_children", False)

            # Small section → accumulate in pending
            if tokens < min_size:
                pending = self._try_add_to_pending(pending, sec, max_size, chunks, parent_index)
                continue

            # Can merge with pending?
            if pending and self._can_merge(pending, tokens, max_size, has_children):
                pending.append(sec)
                self._save_merged_chunks(pending, chunks, parent_index)
                pending = []
                continue

            # Flush pending, then process current
            self._flush_pending(pending, chunks, parent_index)
            pending = []
            self._save_section_chunk(
                content, headings, sec, chunks, parent_index, max_size, min_size
            )

        # Flush remaining
        self._flush_pending(pending, chunks, parent_index)

    def _can_merge(self, pending: List, tokens: int, max_size: int, has_children: bool) -> bool:
        return sum(s["tokens"] for s in pending) + tokens <= max_size and not has_children

    def _try_add_to_pending(
        self, pending: List, sec: Dict, max_size: int,
        chunks: List[ParsedChunk], parent_index: int
    ) -> List:
        if pending and sum(s["tokens"] for s in pending) + sec["tokens"] > max_size:
            self._save_merged_chunks(pending, chunks, parent_index)
            pending = []
        pending.append(sec)
        return pending

    def _flush_pending(
        self, pending: List, chunks: List[ParsedChunk], parent_index: int
    ) -> None:
        if pending:
            self._save_merged_chunks(pending, chunks, parent_index)

    def _save_merged_chunks(
        self, sections: List[Dict], chunks: List[ParsedChunk], parent_index: int
    ) -> None:
        """Save merged sections as a single chunk."""
        combined = "\n\n".join(s["content"] for s in sections)
        names = [s["name"] for s in sections]
        title = names[0] if len(names) == 1 else f"{names[0]} (+{len(names)-1} more)"
        level = sections[0].get("level", 1)
        chunks.append(ParsedChunk(
            content=combined,
            title=title,
            level=level,
            parent_index=parent_index,
            source_format="markdown",
        ))

    def _save_section_chunk(
        self,
        content: str,
        headings: List[Tuple[int, int, str, int]],
        sec: Dict[str, Any],
        chunks: List[ParsedChunk],
        parent_index: int,
        max_size: int,
        min_size: int,
    ) -> None:
        """Save a section as chunk(s), recursing for children or splitting if needed."""
        tokens = sec["tokens"]
        has_children = sec.get("has_children", False)
        level = sec.get("level", 1)

        # Fits in one chunk
        if tokens <= max_size:
            chunks.append(ParsedChunk(
                content=sec["content"],
                title=sec["name"],
                level=level,
                parent_index=parent_index,
                source_format="markdown",
            ))
            return

        if has_children:
            # Create directory chunk, then recurse
            dir_index = len(chunks)
            dir_content = sec.get("direct_content", "") or sec["name"]
            chunks.append(ParsedChunk(
                content=dir_content,
                title=sec["name"],
                level=level,
                parent_index=parent_index,
                source_format="markdown",
            ))

            # Build children
            children = []
            if sec.get("direct_content"):
                children.append({
                    "name": sec["name"],
                    "content": sec["direct_content"],
                    "tokens": estimate_tokens(sec["direct_content"]),
                    "has_children": False,
                    "heading_idx": None,
                    "level": level + 1,
                })
            for child_idx in sec.get("child_indices", []):
                children.append({"heading_idx": child_idx})

            self._process_sections_to_chunks(
                content, headings, children, chunks,
                parent_index=dir_index, parent_level=level,
                max_size=max_size, min_size=min_size,
            )
        else:
            # Split by paragraphs
            parts = self._smart_split_content(sec["content"], max_size)
            for i, part in enumerate(parts):
                chunks.append(ParsedChunk(
                    content=part,
                    title=f"{sec['name']} (part {i+1})" if len(parts) > 1 else sec["name"],
                    level=level,
                    parent_index=parent_index,
                    source_format="markdown",
                ))

    # ========== Utilities ==========

    def _extract_frontmatter(self, content: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Extract YAML frontmatter from content."""
        match = self._frontmatter_pattern.match(content)
        if not match:
            return content, None
        frontmatter_text = match.group(1)
        content_without = content[match.end():]
        frontmatter = {}
        for line in frontmatter_text.split("\n"):
            line = line.strip()
            if ":" in line:
                key, value = line.split(":", 1)
                frontmatter[key.strip()] = value.strip()
        return content_without, frontmatter

    def _smart_split_content(self, content: str, max_size: int) -> List[str]:
        """Split oversized content by paragraphs."""
        paragraphs = content.split("\n\n")
        parts = []
        current = ""
        current_tokens = 0

        for para in paragraphs:
            para_tokens = estimate_tokens(para)
            if para_tokens > max_size:
                if current:
                    parts.append(current.strip())
                    current = ""
                    current_tokens = 0
                char_split_size = int(max_size * 3)
                for i in range(0, len(para), char_split_size):
                    parts.append(para[i:i + char_split_size].strip())
            elif current_tokens + para_tokens > max_size and current:
                parts.append(current.strip())
                current = para
                current_tokens = para_tokens
            else:
                current = current + "\n\n" + para if current else para
                current_tokens += para_tokens

        if current.strip():
            parts.append(current.strip())
        return parts if parts else [content]
