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
        '{"retrieval_queries":["short query 1","short query 2"]}\n\n'
        "Rules:\n"
        f"- Return 1 to {max_queries} queries.\n"
        f"- Each query must be <= {max_chars} characters.\n"
        "- Prefer concrete nouns, entities, systems, phases, and topics.\n"
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
