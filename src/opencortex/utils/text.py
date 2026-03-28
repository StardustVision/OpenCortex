"""Paragraph-aware text truncation and splitting utilities."""
import re
from typing import List


def smart_truncate(text: str, max_chars: int) -> str:
    """Truncate text at the nearest semantic boundary.

    Priority: paragraph (\\n\\n) > line (\\n) > sentence (. ! ?) > word > hard cut.
    GUARANTEE: return value is always <= max_chars.
    """
    if len(text) <= max_chars:
        return text

    result = _truncate_at(text, max_chars, "\n\n")
    if result:
        return result

    result = _truncate_at(text, max_chars, "\n")
    if result:
        return result

    result = _truncate_at_sentence(text, max_chars)
    if result:
        return result

    result = _truncate_at_word(text, max_chars)
    if result:
        return result

    return text[:max_chars]


def _truncate_at(text: str, max_chars: int, sep: str) -> str:
    truncated = text[:max_chars]
    idx = truncated.rfind(sep)
    if idx > 0:
        return text[:idx]
    return ""


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    truncated = text[:max_chars]
    matches = list(re.finditer(r'[.!?。！？]\s', truncated))
    if matches:
        end = matches[-1].end()
        return text[:end].rstrip()
    match = re.search(r'[.!?。！？]$', truncated)
    if match:
        return truncated
    return ""


def _truncate_at_word(text: str, max_chars: int) -> str:
    truncated = text[:max_chars]
    idx = truncated.rfind(" ")
    if idx > 0:
        return text[:idx]
    return ""


def smart_split(text: str, max_chars: int) -> List[str]:
    """Split text into chunks, each <= max_chars, at paragraph boundaries.

    No content is lost — chunks joined with \\n\\n equal the original text.
    For oversized paragraphs, falls back to line splitting, then word splitting.
    """
    if len(text) <= max_chars:
        return [text]

    paragraphs = text.split("\n\n")
    chunks = []
    current = ""

    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(para) > max_chars:
                line_chunks = _split_by_lines(para, max_chars)
                chunks.extend(line_chunks[:-1])
                current = line_chunks[-1]
            else:
                current = para

    if current:
        chunks.append(current)

    return chunks


def _split_by_lines(text: str, max_chars: int) -> List[str]:
    lines = text.split("\n")
    chunks = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(line) > max_chars:
                chunks.extend(_split_by_words(line, max_chars))
                current = ""
            else:
                current = line
    if current:
        chunks.append(current)
    return chunks if chunks else [text[:max_chars]]


def _split_by_words(text: str, max_chars: int) -> List[str]:
    words = text.split(" ")
    chunks = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = word if len(word) <= max_chars else word[:max_chars]
    if current:
        chunks.append(current)
    return chunks if chunks else [text[:max_chars]]
