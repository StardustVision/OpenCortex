"""Paragraph-aware text truncation and splitting utilities."""
import asyncio
import re
from typing import Any, Awaitable, Callable, Dict, List

_SENTENCE_END_RE = re.compile(r'[.!?。！？][\s]')
_SENTENCE_TAIL_RE = re.compile(r'[.!?。！？]$')
_SENTENCE_CHARS = set('.!?。！？')


def smart_truncate(text: str, max_chars: int) -> str:
    """Truncate text at the nearest semantic boundary.

    Priority: paragraph (\\n\\n) > line (\\n) > sentence (. ! ?) > word > hard cut.
    GUARANTEE: return value is always <= max_chars.
    """
    if max_chars <= 0:
        return text
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
    # Scan backward for last sentence boundary (punctuation + whitespace)
    for i in range(len(truncated) - 1, 0, -1):
        if truncated[i - 1] in _SENTENCE_CHARS and truncated[i].isspace():
            return text[:i].rstrip()
    # Check if truncated text ends with sentence punctuation
    if truncated and truncated[-1] in _SENTENCE_CHARS:
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

    Each chunk is a complete paragraph or group of paragraphs.
    Used for chunked LLM processing of oversized content.
    Content is preserved within each chunk but separators between chunks
    may differ from the original when line-level splitting is needed.
    """
    if max_chars <= 0:
        return [text] if text else [""]
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


async def chunked_llm_derive(
    content: str,
    prompt_builder: Callable[[str], str],
    llm_fn: Callable[[str], Awaitable[str]],
    parse_fn: Callable[[str], dict],
    merge_policy: str = "default",
    max_chars_per_chunk: int = 3000,
    max_parallel_chunks: int = 5,
) -> Dict[str, Any]:
    """Split content into chunks, call LLM on each, merge results.

    prompt_builder: Takes content string, returns prompt. Wrap multi-arg
                    builders with lambda/partial before passing.
    parse_fn:       Parses LLM response into dict.
    merge_policy:   "default" — merges {abstract, overview, keywords}
                    "abstract_overview" — merges {abstract, overview} only
    """
    chunks = smart_split(content, max_chars_per_chunk)

    if len(chunks) == 1:
        prompt = prompt_builder(chunks[0])
        response = await llm_fn(prompt)
        result = parse_fn(response)
        if merge_policy == "abstract_overview":
            return {
                "abstract": result.get("abstract", ""),
                "overview": result.get("overview", ""),
            }
        return result

    # Process each chunk with bounded async concurrency.
    import logging
    _logger = logging.getLogger(__name__)

    semaphore = asyncio.Semaphore(max(1, max_parallel_chunks))

    async def _run_chunk(index: int, chunk: str) -> tuple[int, dict] | None:
        prompt = prompt_builder(chunk)
        try:
            async with semaphore:
                response = await llm_fn(prompt)
            parsed = parse_fn(response)
            if isinstance(parsed, dict):
                return index, parsed
        except Exception as exc:
            _logger.warning(
                "chunked_llm_derive: chunk %d/%d failed: %s",
                index + 1,
                len(chunks),
                exc,
            )
        return None

    chunk_outcomes = await asyncio.gather(
        *(_run_chunk(i, chunk) for i, chunk in enumerate(chunks)),
        return_exceptions=False,
    )
    chunk_results = [
        parsed
        for _, parsed in sorted(
            (outcome for outcome in chunk_outcomes if outcome is not None),
            key=lambda item: item[0],
        )
    ]

    if not chunk_results:
        return {"abstract": "", "overview": ""}

    # Merge: abstract from first chunk
    abstract = chunk_results[0].get("abstract", "")

    # Merge: overview via second-pass compression
    all_overviews = "\n\n".join(
        r.get("overview", "") for r in chunk_results if r.get("overview")
    )
    if all_overviews and len(chunk_results) > 1:
        try:
            from opencortex.prompts import build_overview_compression_prompt
            compress_prompt = build_overview_compression_prompt(all_overviews)
            compress_response = await llm_fn(compress_prompt)
            overview = compress_response.strip()
        except Exception:
            overview = chunk_results[0].get("overview", "")
    else:
        overview = all_overviews

    if merge_policy == "abstract_overview":
        return {"abstract": abstract, "overview": overview}

    all_keywords = []
    all_entities = []
    all_anchor_handles = []
    seen_keywords: set[str] = set()
    seen_entities: set[str] = set()
    seen_anchor_handles: set[str] = set()
    for result in chunk_results:
        keywords = result.get("keywords", [])
        if isinstance(keywords, list):
            for keyword in keywords:
                normalized = str(keyword).strip()
                lowered = normalized.lower()
                if normalized and lowered not in seen_keywords:
                    seen_keywords.add(lowered)
                    all_keywords.append(normalized)
        entities = result.get("entities", [])
        if isinstance(entities, list):
            for entity in entities:
                normalized = str(entity).strip()
                lowered = normalized.lower()
                if normalized and lowered not in seen_entities:
                    seen_entities.add(lowered)
                    all_entities.append(normalized)
        anchor_handles = result.get("anchor_handles", [])
        if isinstance(anchor_handles, list):
            for handle in anchor_handles:
                normalized = str(handle).strip()
                lowered = normalized.lower()
                if normalized and lowered not in seen_anchor_handles:
                    seen_anchor_handles.add(lowered)
                    all_anchor_handles.append(normalized)

    return {
        "abstract": abstract,
        "overview": overview,
        "keywords": all_keywords,
        "entities": all_entities,
        "anchor_handles": all_anchor_handles,
    }


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
            if len(word) <= max_chars:
                current = word
            else:
                for i in range(0, len(word), max_chars):
                    piece = word[i:i + max_chars]
                    if i + max_chars < len(word):
                        chunks.append(piece)
                    else:
                        current = piece
    if current:
        chunks.append(current)
    return chunks if chunks else [text[:max_chars]]
