# SPDX-License-Identifier: Apache-2.0
"""Utilities for preserving concrete fact text across recall surfaces."""

from __future__ import annotations

import re
from typing import Any

MAX_FACT_TEXT_LENGTH = 240

_ROLE_PREFIX_RE = re.compile(r"^\s*(?:user|assistant|system)\s*:\s*", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(?:\b(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])"
    r"(?:[-/.](?:0?[1-9]|[12]\d|3[01]))?\b)"
    r"|(?:\b(?:19|20)\d{2}年(?:0?[1-9]|1[0-2])月"
    r"(?:(?:0?[1-9]|[12]\d|3[01])日)?)"
    r"|(?:\b(?:19|20)\d{2}\b)"
)
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?%?\b")
_PATH_RE = re.compile(r"(?:^|\s)(?:/[\w./-]+|[\w.-]+/[\w./-]+)")
_NAMED_TOKEN_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*\b")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+|\n+")


def normalize_fact_text(value: Any) -> str:
    """Return one compact fact string with conversational role prefixes removed."""
    text = " ".join(str(value or "").split())
    return _ROLE_PREFIX_RE.sub("", text).strip()


def extract_time_refs(text: str) -> list[str]:
    """Return explicit time references from text, preserving first-seen order."""
    refs: list[str] = []
    for match in _DATE_RE.finditer(str(text or "")):
        value = match.group(0).strip()
        if not value:
            continue
        containing_index = next(
            (index for index, existing in enumerate(refs) if existing in value),
            None,
        )
        if containing_index is not None:
            refs[containing_index] = value
            continue
        if any(value in existing for existing in refs):
            continue
        refs.append(value)
    return refs


def fact_specificity_score(text: str) -> int:
    """Score how likely a fact is to carry answerable concrete details."""
    value = normalize_fact_text(text)
    if len(value) < 8:
        return 0
    score = 0
    if _DATE_RE.search(value):
        score += 5
    if _NUMBER_RE.search(value):
        score += 2
    if _PATH_RE.search(value):
        score += 2
    score += min(4, len(_NAMED_TOKEN_RE.findall(value)))
    if len(value) >= 24:
        score += 1
    if any(separator in value for separator in (":", "：", "-", "->")):
        score += 1
    return score


def is_answerable_fact(text: str) -> bool:
    """Return whether text is useful as an atomic retrieval fact."""
    value = normalize_fact_text(text)
    if len(value) < 8 or len(value) > MAX_FACT_TEXT_LENGTH:
        return False
    if fact_specificity_score(value) > 0:
        return True
    return len(value.split()) >= 4


def content_fact_candidates(content: str) -> list[str]:
    """Extract concrete sentence-sized fact candidates from raw content."""
    candidates: list[str] = []
    for part in _SENTENCE_SPLIT_RE.split(str(content or "")):
        text = normalize_fact_text(part)
        if is_answerable_fact(text):
            candidates.append(text)
    return unique_fact_points(candidates)


def unique_fact_points(fact_points: Any) -> list[str]:
    """Return de-duplicated fact points in first-seen order."""
    if isinstance(fact_points, str):
        values: list[Any] = [fact_points]
    elif isinstance(fact_points, list):
        values = fact_points
    else:
        values = []
    facts: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = normalize_fact_text(raw)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        facts.append(text)
    return facts


def merge_preserved_fact_points(
    generated_fact_points: Any,
    *,
    content: str,
    max_points: int = 16,
) -> list[str]:
    """Merge LLM facts with concrete raw-content facts without losing precision."""
    facts = unique_fact_points(generated_fact_points)
    seen = {fact.casefold() for fact in facts}
    for candidate in content_fact_candidates(content):
        key = candidate.casefold()
        if key in seen:
            continue
        facts.append(candidate)
        seen.add(key)
        if len(facts) >= max_points:
            break
    return facts[:max_points]


def sorted_answerable_facts(fact_points: Any, *, limit: int | None = None) -> list[str]:
    """Return answerable facts ordered by concrete-detail density."""
    facts = [
        fact for fact in unique_fact_points(fact_points) if is_answerable_fact(fact)
    ]
    indexed = list(enumerate(facts))
    indexed.sort(
        key=lambda item: (
            -fact_specificity_score(item[1]),
            item[0],
        )
    )
    values = [fact for _, fact in indexed]
    return values[:limit] if limit is not None else values


def best_fact_point(record: dict[str, Any]) -> str:
    """Return the strongest fact point carried by a retrieval payload."""
    fact_points: list[Any] = []
    direct_points = record.get("fact_points")
    if isinstance(direct_points, list):
        fact_points.extend(direct_points)
    abstract_json = record.get("abstract_json")
    if isinstance(abstract_json, dict):
        json_points = abstract_json.get("fact_points")
        if isinstance(json_points, list):
            fact_points.extend(json_points)
    facts = sorted_answerable_facts(fact_points, limit=1)
    return facts[0] if facts else ""
