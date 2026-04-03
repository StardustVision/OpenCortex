"""LLM adapter — bridges OpenCortex's llm_completion callable to the skill engine's
complete(messages) interface.

OpenCortex's _llm_completion is Callable[[str], Awaitable[str]] (single prompt string).
The skill engine expects complete(messages: List[Dict]) → str (chat-style).
This adapter converts between the two.
"""

from typing import Awaitable, Callable, Dict, List, Protocol, Union


async def llm_complete(llm, prompt: str) -> str:
    """Call LLM with adapter compatibility."""
    if hasattr(llm, 'complete'):
        return await llm.complete([{"role": "user", "content": prompt}])
    return await llm([{"role": "user", "content": prompt}])


class LLMAdapter(Protocol):
    """Protocol for LLM completion."""

    async def complete(self, messages: List[Dict], **kwargs) -> str: ...


class LLMCompletionAdapter:
    """Wraps OpenCortex's llm_completion callable into the LLMAdapter interface.

    Concatenates chat messages into a single prompt string, since the
    underlying llm_completion only accepts a flat string.
    """

    def __init__(self, llm_fn: Callable[[str], Awaitable[str]]):
        self._fn = llm_fn

    async def complete(self, messages: List[Dict], **kwargs) -> str:
        """Convert messages to flat prompt, call underlying function."""
        prompt = self._messages_to_prompt(messages)
        return await self._fn(prompt)

    @staticmethod
    def _messages_to_prompt(messages: List[Dict]) -> str:
        """Flatten chat messages into a single prompt string."""
        parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                parts.append(f"[System] {content}")
            elif role == "assistant":
                parts.append(f"[Assistant] {content}")
            else:
                parts.append(content)
        return "\n\n".join(parts)
