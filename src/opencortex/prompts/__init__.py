# SPDX-License-Identifier: Apache-2.0
"""Prompt builders and output schemas for opencortex LLM calls."""

from opencortex.prompts.retrieval import (
    QUERY_DECOMPOSITION_SYSTEM_PROMPT,
    REASON_TREE_SELECTION_SYSTEM_PROMPT,
    build_query_decomposition_prompt,
    build_reason_tree_selection_prompt,
)
from opencortex.prompts.schemas import (
    LayerDerivationOutput,
    QueryDecompositionOutput,
    ReasonTreeSelectionOutput,
    ReasonTreeSource,
)
from opencortex.prompts.write import (
    LAYER_DERIVATION_SYSTEM_PROMPT,
    MEMORY_EXTRACTION_SYSTEM_PROMPT,
    RESOURCE_TREE_SYSTEM_PROMPT,
    SESSION_TREE_SYSTEM_PROMPT,
    build_layer_derivation_prompt,
    build_memory_extraction_prompt,
    build_resource_tree_prompt,
    build_session_tree_prompt,
)

__all__ = [
    "LAYER_DERIVATION_SYSTEM_PROMPT",
    "MEMORY_EXTRACTION_SYSTEM_PROMPT",
    "QUERY_DECOMPOSITION_SYSTEM_PROMPT",
    "REASON_TREE_SELECTION_SYSTEM_PROMPT",
    "RESOURCE_TREE_SYSTEM_PROMPT",
    "SESSION_TREE_SYSTEM_PROMPT",
    "LayerDerivationOutput",
    "QueryDecompositionOutput",
    "ReasonTreeSelectionOutput",
    "ReasonTreeSource",
    "build_layer_derivation_prompt",
    "build_memory_extraction_prompt",
    "build_query_decomposition_prompt",
    "build_reason_tree_selection_prompt",
    "build_resource_tree_prompt",
    "build_session_tree_prompt",
]
