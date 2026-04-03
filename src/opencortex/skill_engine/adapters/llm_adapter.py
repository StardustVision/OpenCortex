"""LLM adapter — delegates to existing OpenCortex LLM client."""

from typing import Dict, List, Protocol


class LLMAdapter(Protocol):
    """Protocol for LLM completion."""

    async def complete(self, messages: List[Dict], **kwargs) -> str: ...
