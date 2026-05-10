# SPDX-License-Identifier: Apache-2.0
"""LLM layer derivation for store records."""

from __future__ import annotations

from typing import Any

from opencortex.prompts.schemas import LayerDerivationOutput
from opencortex.prompts.write import (
    LAYER_DERIVATION_SYSTEM_PROMPT,
    build_layer_derivation_prompt,
)
from opencortex.utils.json_parse import parse_json_from_response


def normalize_layer_derivation(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize LLM-derived layer JSON."""
    return LayerDerivationOutput.model_validate(data).model_dump(mode="json")


async def derive_layers(
    *,
    llm_completion: Any,
    content: str,
    record_kind: str = "memory",
    uri: str = "",
    parent_uri: str = "",
) -> dict[str, Any]:
    """Derive semantic layers from full content with the required LLM."""
    prompt = build_layer_derivation_prompt(
        content,
        record_kind=record_kind,
        uri=uri,
        parent_uri=parent_uri,
    )
    response = await llm_completion(
        prompt,
        system_prompt=LAYER_DERIVATION_SYSTEM_PROMPT,
    )
    parsed = parse_json_from_response(response)
    if not isinstance(parsed, dict):
        raise ValueError("LLM layer derivation must return a JSON object")
    return normalize_layer_derivation(parsed)
