# SPDX-License-Identifier: Apache-2.0
"""
Memory extractor for OpenCortex session analysis.

LLM-driven analysis of session conversations to extract persistent memories.
"""

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from opencortex.prompts import build_extraction_prompt
from opencortex.session.types import ExtractedMemory, Message
from opencortex.utils.json_parse import parse_json_from_response

logger = logging.getLogger(__name__)

LLMCompletionCallable = Callable[[str], Awaitable[str]]

CASE_META_SCHEMA_VERSION = 1


def validate_case_meta(meta: dict) -> dict:
    """Read-time compat: fill missing fields with defaults."""
    meta.setdefault("schema_version", CASE_META_SCHEMA_VERSION)
    meta.setdefault("task_objective", "")
    meta.setdefault("action_path", [])
    meta.setdefault("result", "")
    meta.setdefault("evaluation", {"status": "unknown", "score": 0.0})
    meta.setdefault("error_cause", "")
    meta.setdefault("improvement", "")
    return meta


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
        """Build the LLM prompt for memory extraction (thin wrapper).

        Formats Message objects into text, then delegates to the
        centralized prompt in opencortex.prompts.
        """
        # Format conversation (truncate to last 50 messages to control context)
        conv_lines = []
        for msg in messages[-50:]:
            role = msg.role.upper()
            content = msg.content[:500]
            conv_lines.append(f"[{role}] {content}")
        conversation = "\n".join(conv_lines)

        return build_extraction_prompt(conversation, quality_score, session_summary)

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
            context_type = item.get("context_type", "memory")
            raw_meta = item.get("meta", {})
            if not isinstance(raw_meta, dict):
                raw_meta = {}
            if context_type == "case":
                validate_case_meta(raw_meta)
            memories.append(
                ExtractedMemory(
                    abstract=abstract,
                    content=item.get("content", ""),
                    category=item.get("category", ""),
                    context_type=context_type,
                    confidence=min(1.0, max(0.0, float(item.get("confidence", 0.5)))),
                    uri_hint=item.get("uri_hint", "user"),
                    meta=raw_meta,
                )
            )
        return memories
