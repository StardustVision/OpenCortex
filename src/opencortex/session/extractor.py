# SPDX-License-Identifier: Apache-2.0
"""
Memory extractor for OpenCortex session analysis.

LLM-driven analysis of session conversations to extract persistent memories.
"""

import logging
from typing import Awaitable, Callable, List, Optional

from opencortex.session.types import ExtractedMemory, Message
from opencortex.utils.json_parse import parse_json_from_response

logger = logging.getLogger(__name__)

LLMCompletionCallable = Callable[[str], Awaitable[str]]


class MemoryExtractor:
    """LLM-driven memory extractor that analyzes session conversations.

    Produces a list of ExtractedMemory items from a conversation buffer,
    identifying user preferences, learned patterns, agent skills, and resources.

    Args:
        llm_completion: Async callable ``async def(prompt: str) -> str``.
    """

    def __init__(self, llm_completion: LLMCompletionCallable):
        self._llm_completion = llm_completion

    async def extract(
        self,
        messages: List[Message],
        quality_score: float = 0.0,
        session_summary: str = "",
    ) -> List[ExtractedMemory]:
        """Extract memories from session messages.

        Args:
            messages: Conversation messages from the session.
            quality_score: Overall session quality score (0-1).
            session_summary: Pre-computed session summary (if any).

        Returns:
            List of extracted memories with confidence scores.
        """
        if not messages:
            return []

        prompt = self._build_extraction_prompt(messages, quality_score, session_summary)
        try:
            response = await self._llm_completion(prompt)
            return self._parse_extraction_response(response)
        except Exception as exc:
            logger.warning("[MemoryExtractor] LLM extraction failed: %s", exc)
            return []

    def _build_extraction_prompt(
        self,
        messages: List[Message],
        quality_score: float,
        session_summary: str,
    ) -> str:
        """Build the LLM prompt for memory extraction."""
        # Format conversation (truncate to last 50 messages to control context)
        conv_lines = []
        for msg in messages[-50:]:
            role = msg.role.upper()
            content = msg.content[:500]
            conv_lines.append(f"[{role}] {content}")
        conversation = "\n".join(conv_lines)

        summary_section = ""
        if session_summary:
            summary_section = f"\nSession Summary: {session_summary}\n"

        return f"""You are a memory extraction system. Analyze the following conversation and extract persistent memories that should be saved for future sessions.

{summary_section}
Session Quality Score: {quality_score:.1f}/1.0

Conversation:
{conversation}

Extract memories in these categories:
- **preferences**: User preferences, settings, workflow habits
- **patterns**: Recurring patterns, common solutions, best practices
- **entities**: Important names, paths, URLs, configurations
- **skills**: Agent skills learned or improved during the session
- **errors**: Error patterns and their solutions

For each memory, provide:
- abstract: Short summary (1-2 sentences, used for vector search)
- content: Full details
- category: One of the categories above
- context_type: "memory" for user knowledge, "skill" for agent capabilities, "resource" for project info
- confidence: 0.0 to 1.0 (how confident this is a persistent, reusable memory)
- uri_hint: "user" for user-specific, "agent" for agent patterns, "shared" for project resources

Return ONLY a JSON array. Example:
[
  {{"abstract": "User prefers dark theme", "content": "User explicitly set dark theme in VS Code and terminal", "category": "preferences", "context_type": "memory", "confidence": 0.9, "uri_hint": "user"}},
  {{"abstract": "Fix import error by checking PYTHONPATH", "content": "When imports fail, check PYTHONPATH includes src/", "category": "errors", "context_type": "skill", "confidence": 0.7, "uri_hint": "agent"}}
]

If no meaningful memories can be extracted, return an empty array: []
Memories:"""

    def _parse_extraction_response(self, response: str) -> List[ExtractedMemory]:
        """Parse LLM response into ExtractedMemory list."""
        data = parse_json_from_response(response, expect_array=True)
        if isinstance(data, list):
            return self._convert_to_memories(data)
        logger.warning("[MemoryExtractor] Could not parse extraction response")
        return []

    def _convert_to_memories(self, data: list) -> List[ExtractedMemory]:
        """Convert parsed JSON dicts to ExtractedMemory objects."""
        memories = []
        for item in data:
            if not isinstance(item, dict):
                continue
            abstract = item.get("abstract", "").strip()
            if not abstract:
                continue
            memories.append(
                ExtractedMemory(
                    abstract=abstract,
                    content=item.get("content", ""),
                    category=item.get("category", ""),
                    context_type=item.get("context_type", "memory"),
                    confidence=min(1.0, max(0.0, float(item.get("confidence", 0.5)))),
                    uri_hint=item.get("uri_hint", "user"),
                )
            )
        return memories
