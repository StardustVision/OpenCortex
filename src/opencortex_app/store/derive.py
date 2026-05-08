# SPDX-License-Identifier: Apache-2.0
"""LLM layer derivation for store records."""

from __future__ import annotations

from typing import Any

from opencortex_app.utils.json_parse import parse_json_from_response


def build_layer_derivation_prompt(content: str) -> str:
    """Build a compact user prompt for layer derivation."""
    return (
        "Derive OpenCortex L0/L1 layers from this L2 content.\n\n"
        f"<content>\n{content}\n</content>"
    )


def normalize_layer_derivation(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize LLM-derived layer JSON."""
    abstract = str(data.get("abstract", "") or "")
    overview = str(data.get("overview", "") or "")
    if not abstract:
        raise ValueError("LLM layer derivation missing abstract")
    if not overview:
        raise ValueError("LLM layer derivation missing overview")
    return {
        "abstract": abstract,
        "overview": overview,
        "keywords": data.get("keywords", []),
        "entities": data.get("entities", []),
        "anchor_handles": data.get("anchor_handles", []),
        "fact_points": data.get("fact_points", []),
    }


async def derive_layers(*, llm_completion: Any, content: str) -> dict[str, Any]:
    """Derive semantic layers from full content with the required LLM."""
    response = await llm_completion(build_layer_derivation_prompt(content))
    parsed = parse_json_from_response(response)
    if not isinstance(parsed, dict):
        raise ValueError("LLM layer derivation must return a JSON object")
    return normalize_layer_derivation(parsed)
