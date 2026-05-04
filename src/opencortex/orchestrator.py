# SPDX-License-Identifier: Apache-2.0
"""Backward-compatible memory facade exports.

`CortexMemory` is the canonical memory facade. `MemoryOrchestrator` remains as
an import-compatible alias for existing users and tests.
"""

from opencortex.cortex_memory import (
    LLMCompletionCallable,
    CortexMemory,
    _CONTEXT_COLLECTION,
)

MemoryOrchestrator = CortexMemory

__all__ = [
    "CortexMemory",
    "MemoryOrchestrator",
    "LLMCompletionCallable",
    "_CONTEXT_COLLECTION",
]
