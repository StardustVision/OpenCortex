# SPDX-License-Identifier: Apache-2.0
"""
Memory extractor for OpenCortex session analysis.

LLM-driven analysis of session conversations to extract persistent memories.
"""

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

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

User memories (private to this user):
- **profile**: User identity, roles, background attributes
- **preferences**: User preferences, settings, workflow habits
- **entities**: Important entities — people, projects, paths, URLs, configurations
- **events**: Decisions, milestones, key events (each unique, never merge)

Agent knowledge (shared at project level):
- **cases**: Problem + solution pairs (each unique, never merge)
- **patterns**: Reusable patterns, best practices, recurring solutions

For each memory, provide:
- abstract: Short summary (1-2 sentences, used for vector search)
- content: Full details
- category: One of: profile, preferences, entities, events, cases, patterns
- context_type: "memory" for user categories (profile/preferences/entities/events), "case" for cases, "pattern" for patterns
- confidence: 0.0 to 1.0 (how confident this is a persistent, reusable memory)

**For case-type memories ONLY** (context_type="case"), also include a "meta" object:
- schema_version: 1 (integer, always 1)
- task_objective: What the user was trying to accomplish (string)
- action_path: Ordered list of key steps taken (array of strings)
- result: What actually happened (string)
- evaluation: {{"status": "success"|"partial"|"failure", "score": 0.0-1.0}}
- error_cause: What went wrong (empty string if success)
- improvement: What could be done better next time (string)

Return ONLY a JSON array. Example:
[
  {{"abstract": "User prefers dark theme", "content": "User explicitly set dark theme in VS Code and terminal", "category": "preferences", "context_type": "memory", "confidence": 0.9}},
  {{"abstract": "Fix import error by checking PYTHONPATH", "content": "When imports fail, check PYTHONPATH includes src/", "category": "cases", "context_type": "case", "confidence": 0.7, "meta": {{"schema_version": 1, "task_objective": "Fix Python import error", "action_path": ["Check PYTHONPATH", "Add src/ to path", "Verify import"], "result": "Import resolved after adding src/ to PYTHONPATH", "evaluation": {{"status": "success", "score": 0.9}}, "error_cause": "", "improvement": "Add src/ to PYTHONPATH in project setup"}}}}
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
