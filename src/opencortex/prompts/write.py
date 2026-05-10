# SPDX-License-Identifier: Apache-2.0
"""Write-path prompt builders for semantic derivation and reason-tree creation."""

from __future__ import annotations

from typing import Any

LAYER_DERIVATION_SYSTEM_PROMPT = (
    "You are OpenCortex's write-path layer derivation engine.\n"
    """

Derive retrieval layers from the complete L2 content. Return valid JSON only.
Do not include markdown fences, commentary, or explanations outside the JSON.

Required output fields:
- abstract: one factual sentence for L0 retrieval preview.
- overview: structured Markdown for L1 retrieval detail.
- keywords: an array of concrete key terms.
- entities: an array of named entities.
- anchor_handles: an array of compact retrieval handles.
- fact_points: an array of atomic fact statements.

Rules:
- The overview is the primary retrieval surface. It must be specific enough for a
  reader to answer factual questions without reading the original content.
- Preserve concrete names, dates, numbers, locations, identities, relationships,
  paths, tools, plans, decisions, and commitments verbatim when present.
- Preserve exact facts instead of compressing them into generic summaries.
- Do not invent facts that are not in the content.
- Do not truncate the input content yourself.
- For session/message content, prioritize durable facts, exact answers, user
  preferences, plans, relationships, and decisions.
- fact_points must be self-contained atomic facts, not topic labels."""
)

MEMORY_EXTRACTION_SYSTEM_PROMPT = (
    "You are OpenCortex's long-term memory extraction engine.\n"
    """

Extract durable user or agent memories from session content. Return JSON only.
Favor high recall: when a specific fact may be useful later, extract it as a
separate memory. Downstream writers handle deduplication and updates."""
)

RESOURCE_TREE_SYSTEM_PROMPT = """You are OpenCortex's resource reason-tree builder.

Build a compact tree that helps retrieval locate the exact resource section that
answers a query. Return JSON only. Prefer precise section nodes over broad
generic summaries, and preserve source references such as headings, line ranges,
page ranges, paths, or chunk URIs when available."""

SESSION_TREE_SYSTEM_PROMPT = """You are OpenCortex's session reason-tree builder.

Build a compact tree from session/end content for future recall. Return JSON
only. Preserve message ids, turn ranges, exact names, dates, decisions,
preferences, plans, and facts. Do not reduce the session to a generic summary."""


def build_layer_derivation_prompt(
    content: str,
    *,
    record_kind: str = "memory",
    uri: str = "",
    parent_uri: str = "",
) -> str:
    """Build the write-path derivation prompt for one stored record."""
    return (
        "Derive OpenCortex L0/L1 retrieval layers from this L2 content.\n\n"
        "Return a JSON object with exactly these fields:\n"
        "{\n"
        '  "abstract": "one factual sentence",\n'
        '  "overview": "structured Markdown summary",\n'
        '  "keywords": ["concrete term"],\n'
        '  "entities": ["named entity"],\n'
        '  "anchor_handles": ["compact retrieval handle"],\n'
        '  "fact_points": ["atomic fact"]\n'
        "}\n\n"
        "Field rules:\n"
        "- abstract: concise, specific, and not a truncation.\n"
        "- overview: primary retrieval surface; preserve exact facts and values.\n"
        "- keywords: concrete searchable terms only.\n"
        "- entities: named people, systems, tools, organizations, or places.\n"
        "- anchor_handles: short handles containing entities, dates, paths, or terms.\n"
        "- fact_points: self-contained atomic facts that can answer QA.\n\n"
        f"Record kind: {record_kind}\n"
        f"URI: {uri}\n"
        f"Parent URI: {parent_uri}\n\n"
        f"<content>\n{content}\n</content>"
    )


def build_memory_extraction_prompt(
    *,
    content: str,
    user_id: str = "",
    source_refs: list[str] | None = None,
) -> str:
    """Build a prompt for extracting durable memories from session content."""
    refs = "\n".join(f"- {ref}" for ref in source_refs or [])
    return (
        "Analyze the session content and extract durable long-term memories.\n\n"
        "Return JSON with this shape:\n"
        "{\n"
        '  "memories": [\n'
        "    {\n"
        '      "category": "profile|preference|entity|event|case|pattern",\n'
        '      "abstract": "one-line L0 memory",\n'
        '      "overview": "structured Markdown L1 memory",\n'
        '      "content": "complete L2 memory narrative",\n'
        '      "confidence": 0.0,\n'
        '      "source_refs": ["message_id_or_uri"]\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Extract each independently updatable fact as its own memory.\n"
        "- Do not mix unrelated preference facets in one memory.\n"
        "- event memories describe what happened, is happening, or is planned.\n"
        "- case memories require problem plus cause, solution, workaround, or "
        "outcome.\n"
        "- Convert relative time expressions to exact text only when present in "
        "input;\n"
        "  otherwise omit time rather than inventing it.\n"
        '- If nothing durable exists, return {"memories": []}.\n\n'
        f"User: {user_id}\n"
        f"Source refs:\n{refs}\n\n"
        f"<content>\n{content}\n</content>"
    )


def build_resource_tree_prompt(
    *,
    content: str,
    uri: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Build a resource-to-reason-tree prompt."""
    return build_reason_tree_prompt(
        content=content,
        uri=uri,
        metadata=metadata,
        source_kind="resource",
        source_ref_hint="page, line range, section path, heading, path, or chunk URI",
    )


def build_session_tree_prompt(
    *,
    content: str,
    uri: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Build a session/end-to-reason-tree prompt."""
    return build_reason_tree_prompt(
        content=content,
        uri=uri,
        metadata=metadata,
        source_kind="session",
        source_ref_hint="message id, turn id, turn range, event URI, or merged URI",
    )


def build_reason_tree_prompt(
    *,
    content: str,
    uri: str,
    metadata: dict[str, Any] | None,
    source_kind: str,
    source_ref_hint: str,
) -> str:
    """Build the common resource/session reason-tree prompt body."""
    return (
        f"Build a Reason Tree for this {source_kind} content.\n\n"
        "Return JSON with this shape:\n"
        "{\n"
        '  "abstract": "one factual sentence for the whole tree",\n'
        '  "overview": "specific overview of the whole tree",\n'
        '  "nodes": [\n'
        "    {\n"
        '      "title": "stable node title",\n'
        '      "summary": "specific node summary",\n'
        '      "fact_points": ["atomic fact"],\n'
        '      "source_refs": ["source reference"],\n'
        '      "children": []\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Use the tree to locate exact raw content during recall.\n"
        "- Prefer precise nodes over broad buckets.\n"
        "- Preserve concrete names, dates, numbers, paths, tools, and decisions.\n"
        f"- source_refs should use {source_ref_hint} when available.\n"
        "- Do not invent nodes or facts that are not grounded in content.\n\n"
        f"URI: {uri}\n"
        f"Metadata: {metadata or {}}\n\n"
        f"<content>\n{content}\n</content>"
    )
