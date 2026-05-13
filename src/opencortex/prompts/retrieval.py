# SPDX-License-Identifier: Apache-2.0
"""Retrieval-path prompt builders."""

from __future__ import annotations

from opencortex.prompts.schemas import ReasonTreeSource

QUERY_DECOMPOSITION_SYSTEM_PROMPT = """You are OpenCortex's retrieval query planner.

Convert large user queries into short vector-search queries. Return JSON only.
Do not answer the user query and do not add facts that are not present in it."""

REASON_TREE_SELECTION_SYSTEM_PROMPT = """You are OpenCortex's reason-tree selector.

Select the best indexed tree entry points for a recall query. Return JSON only.
Use only candidate URIs. Prefer precise nodes over broad parent nodes."""

RECALL_RERANK_SYSTEM_PROMPT = """You are OpenCortex's memory relevance ranker.

Score candidate memories by how directly they help answer the user query,
including candidates that jointly support an inferred answer.
Return JSON only. Do not answer the query."""

RECALL_COMPOSER_SYSTEM_PROMPT = """You are OpenCortex's recall composer.

Use only the provided memories and facts. Return JSON only. Produce a short
reasoning chain, supporting URIs, and a calibrated confidence."""


def build_query_decomposition_prompt(
    query: str,
    *,
    max_queries: int,
    max_chars: int,
) -> str:
    """Build a narrow prompt for large-query probe planning."""
    return (
        "Split this user query into short retrieval queries for vector search.\n"
        "Return valid JSON only with this shape:\n"
        '{"retrieval_queries":["short query 1","short query 2"],'
        '"query_type":"factual|temporal|reasoning|multihop|summary"}\n\n'
        "Rules:\n"
        f"- Return 1 to {max_queries} queries.\n"
        f"- Each query must be <= {max_chars} characters.\n"
        "- Prefer concrete nouns, entities, systems, phases, and topics.\n"
        "- query_type is internal routing only; choose factual unless the query "
        "clearly requires time ordering, reasoning, multi-hop comparison, or summary.\n"
        "- Do not answer the query.\n"
        "- Do not add facts not present in the query.\n\n"
        f"<query>\n{query}\n</query>"
    )


def build_reason_tree_selection_prompt(
    query: str,
    candidates: list[ReasonTreeSource],
) -> str:
    """Build the LLM prompt for reason-tree entry selection."""
    lines = [
        "Select the best reason-tree entry URIs for this recall query.",
        'Return JSON only: {"selected_uris":["uri1","uri2"],"reason":"brief reason"}.',
        "Use only URIs from the candidates. Prefer precise entries over broad ones.",
        "Select at most 3 URIs.",
        "",
        f"Query: {query}",
        "",
        "Candidates:",
    ]
    for index, candidate in enumerate(candidates, start=1):
        facts = "; ".join(candidate.fact_points[:5])
        refs = "; ".join(candidate.source_refs[:5])
        lines.append(
            f"{index}. uri={candidate.uri} "
            f"title={candidate.title} "
            f"context={candidate.context_window} "
            f"summary={candidate.summary} "
            f"facts={facts} "
            f"source_refs={refs}"
        )
    return "\n".join(lines)


def build_recall_rerank_prompt(
    query: str,
    candidates: list[dict[str, str]],
) -> str:
    """Build the LLM prompt for business-facing recall rerank."""
    lines = [
        "Score these candidate memories for this user query.",
        'Return JSON only: {"scores":[{"uri":"candidate-uri","score":0.0}]}',
        "Use only candidate URIs. Score from 0.0 to 1.0.",
        (
            "Higher means the candidate contains or jointly supports facts needed "
            "to answer."
        ),
        "Prefer exact facts, named entities, and source sections over broad summaries.",
        "",
        f"Query: {query}",
        "",
        "Candidates:",
    ]
    for index, candidate in enumerate(candidates, start=1):
        lines.append(
            f"{index}. uri={candidate['uri']}\n"
            f"type={candidate['type']}\n"
            f"title={candidate['title']}\n"
            f"section={candidate['section']}\n"
            f"text={candidate['text']}"
        )
    return "\n\n".join(lines)


def build_recall_composition_prompt(
    query: str,
    memories: list[dict[str, object]],
) -> str:
    """Build the LLM prompt for composing reasoning evidence."""
    lines = [
        "Compose a compact reasoning trace for this recall query.",
        (
            'Return JSON only: {"reasoning_chain":["step"],'
            '"supporting_uris":["uri"],"confidence":0.0}'
        ),
        "Use only the provided memory snippets, fact_points, and retrieval surfaces.",
        "Do not invent facts or answer beyond the evidence.",
        "",
        f"Query: {query}",
        "",
        "Memories:",
    ]
    for index, memory in enumerate(memories, start=1):
        facts = "; ".join(str(item) for item in memory.get("fact_points", []) or [])
        surfaces = ", ".join(str(item) for item in memory.get("surfaces", []) or [])
        lines.append(
            f"{index}. uri={memory.get('uri', '')}\n"
            f"score={memory.get('score', 0.0)}\n"
            f"surfaces={surfaces}\n"
            f"abstract={memory.get('abstract', '')}\n"
            f"overview={memory.get('overview', '')}\n"
            f"evidence={memory.get('snippet', '')}\n"
            f"fact_points={facts}"
        )
    return "\n\n".join(lines)
