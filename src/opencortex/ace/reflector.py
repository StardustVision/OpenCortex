# SPDX-License-Identifier: Apache-2.0
"""Reflector — LLM-driven execution analysis and learning extraction."""

import json
import logging
import re
from typing import Awaitable, Callable, List, Optional

from opencortex.ace.prompts import build_reflector_prompt, format_skills_excerpt
from opencortex.ace.types import Learning, ReflectorOutput, Skill, SkillTag

logger = logging.getLogger(__name__)

LLMCompletionCallable = Callable[[str], Awaitable[str]]

_VALID_TAGS = {"helpful", "harmful", "neutral"}


def _parse_json_from_response(response: str) -> Optional[dict]:
    """Parse JSON from an LLM response string.

    Handles: pure JSON, ```json code blocks, embedded {...}.
    """
    if not response:
        return None

    try:
        return json.loads(response.strip())
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    match = re.search(r"\{.*\}", response, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


class Reflector:
    """Analyzes agent executions and extracts reusable learnings via LLM."""

    def __init__(self, llm_completion: LLMCompletionCallable):
        self._llm_completion = llm_completion

    async def reflect(
        self,
        question: str,
        reasoning: str,
        answer: str,
        feedback: str,
        skills: Optional[List[Skill]] = None,
    ) -> ReflectorOutput:
        """Analyze an execution and extract learnings.

        Args:
            question: The original question/task
            reasoning: Agent's reasoning process
            answer: Agent's answer/action
            feedback: Outcome feedback
            skills: Relevant existing skills for context

        Returns:
            ReflectorOutput with analysis and extracted learnings
        """
        skills_excerpt = format_skills_excerpt(skills or [])
        prompt = build_reflector_prompt(
            question=question,
            reasoning=reasoning,
            answer=answer,
            feedback=feedback,
            skills_excerpt=skills_excerpt,
        )

        try:
            response = await self._llm_completion(prompt)
        except Exception as e:
            logger.warning(f"[Reflector] LLM call failed: {e}")
            return self._degraded_output()

        parsed = _parse_json_from_response(response)
        if parsed is None:
            logger.warning("[Reflector] Could not parse LLM response, returning degraded output")
            return self._degraded_output()

        return self._build_output(parsed)

    def _build_output(self, parsed: dict) -> ReflectorOutput:
        """Build ReflectorOutput from parsed JSON, validating entries."""
        learnings = []
        for item in parsed.get("extracted_learnings", []):
            if not isinstance(item, dict):
                continue
            learning_text = item.get("learning", "").strip()
            evidence_text = item.get("evidence", "").strip()
            if not learning_text or not evidence_text:
                continue
            learnings.append(
                Learning(
                    learning=learning_text,
                    evidence=evidence_text,
                    justification=item.get("justification", ""),
                )
            )

        skill_tags = []
        for item in parsed.get("skill_tags", []):
            if not isinstance(item, dict):
                continue
            skill_id = item.get("skill_id", "").strip()
            tag = item.get("tag", "").strip()
            if not skill_id or tag not in _VALID_TAGS:
                continue
            skill_tags.append(SkillTag(skill_id=skill_id, tag=tag))

        return ReflectorOutput(
            reasoning=parsed.get("reasoning", ""),
            error_identification=parsed.get("error_identification", "none"),
            root_cause_analysis=parsed.get("root_cause_analysis", ""),
            key_insight=parsed.get("key_insight", ""),
            extracted_learnings=learnings,
            skill_tags=skill_tags,
        )

    @staticmethod
    def _degraded_output() -> ReflectorOutput:
        """Return a degraded ReflectorOutput when parsing fails."""
        return ReflectorOutput(
            reasoning="parse_error",
            error_identification="unknown",
            root_cause_analysis="",
            key_insight="",
        )
