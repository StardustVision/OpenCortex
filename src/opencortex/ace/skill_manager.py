# SPDX-License-Identifier: Apache-2.0
"""SkillManager — LLM-driven Skillbook update decisions."""

import json
import logging
import re
from typing import Awaitable, Callable, List, Optional

from opencortex.ace.prompts import build_skill_manager_prompt
from opencortex.ace.types import ReflectorOutput, SkillTag, UpdateOperation

logger = logging.getLogger(__name__)

LLMCompletionCallable = Callable[[str], Awaitable[str]]


def _parse_json_array_from_response(response: str) -> Optional[list]:
    """Parse a JSON array from an LLM response string.

    Handles: pure JSON array, ```json code blocks, embedded [...].
    """
    if not response:
        return None

    try:
        data = json.loads(response.strip())
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1).strip())
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    match = re.search(r"\[.*\]", response, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    return None


class SkillManager:
    """Decides how to update the Skillbook based on Reflector analysis."""

    def __init__(self, llm_completion: LLMCompletionCallable):
        self._llm_completion = llm_completion

    async def decide(
        self,
        reflection: ReflectorOutput,
        skillbook_state: str,
        context: str,
    ) -> List[UpdateOperation]:
        """Decide on Skillbook update operations.

        TAG operations come directly from Reflector's skill_tags (deterministic).
        ADD/UPDATE/REMOVE operations are decided by LLM when learnings exist.

        Args:
            reflection: Reflector output with learnings and tags
            skillbook_state: Current skillbook as tab-separated table
            context: Additional context string

        Returns:
            List of UpdateOperation to apply
        """
        # TAG ops are deterministic from Reflector
        tag_ops = self._build_tag_ops(reflection.skill_tags)

        # No learnings → only TAG ops, skip LLM
        if not reflection.extracted_learnings:
            return tag_ops

        # Build reflection JSON for prompt
        reflection_json = json.dumps(
            {
                "reasoning": reflection.reasoning,
                "error_identification": reflection.error_identification,
                "root_cause_analysis": reflection.root_cause_analysis,
                "key_insight": reflection.key_insight,
                "extracted_learnings": [
                    {
                        "learning": l.learning,
                        "evidence": l.evidence,
                        "justification": l.justification,
                    }
                    for l in reflection.extracted_learnings
                ],
            },
            indent=2,
        )

        prompt = build_skill_manager_prompt(
            skillbook_state=skillbook_state,
            reflection_json=reflection_json,
            context=context,
        )

        try:
            response = await self._llm_completion(prompt)
        except Exception as e:
            logger.warning(f"[SkillManager] LLM call failed: {e}")
            return tag_ops

        parsed = _parse_json_array_from_response(response)
        if parsed is None:
            logger.warning("[SkillManager] Could not parse LLM response, returning TAG ops only")
            return tag_ops

        llm_ops = self._parse_operations(parsed)
        return self._merge_operations(tag_ops, llm_ops)

    def _build_tag_ops(self, skill_tags: List[SkillTag]) -> List[UpdateOperation]:
        """Convert SkillTags to TAG UpdateOperations."""
        ops = []
        for st in skill_tags:
            ops.append(
                UpdateOperation(
                    type="TAG",
                    section="",  # TAG doesn't need section
                    skill_id=st.skill_id,
                    metadata={st.tag: 1},
                )
            )
        return ops

    def _parse_operations(self, parsed: list) -> List[UpdateOperation]:
        """Parse and validate operations from LLM response."""
        ops = []
        for item in parsed:
            if not isinstance(item, dict):
                continue

            op_type = item.get("type", "").upper()
            if op_type not in ("ADD", "UPDATE", "REMOVE"):
                continue

            section = item.get("section", "general")
            content = item.get("content", "").strip() if item.get("content") else None
            skill_id = item.get("skill_id", "").strip() if item.get("skill_id") else None
            justification = item.get("justification")
            evidence = item.get("evidence")

            # Validate: ADD requires content
            if op_type == "ADD" and not content:
                continue
            # Validate: UPDATE/REMOVE requires skill_id
            if op_type in ("UPDATE", "REMOVE") and not skill_id:
                continue

            ops.append(
                UpdateOperation(
                    type=op_type,
                    section=section,
                    content=content,
                    skill_id=skill_id,
                    justification=justification,
                    evidence=evidence,
                )
            )
        return ops

    def _merge_operations(
        self,
        tag_ops: List[UpdateOperation],
        llm_ops: List[UpdateOperation],
    ) -> List[UpdateOperation]:
        """Merge TAG ops with LLM ops, deduplicating TAGs by skill_id."""
        # Collect skill_ids that LLM ops target (for TAG dedup)
        llm_skill_ids = {op.skill_id for op in llm_ops if op.skill_id}

        # Keep TAG ops whose skill_id isn't already targeted by LLM ops
        filtered_tags = [op for op in tag_ops if op.skill_id not in llm_skill_ids]

        return filtered_tags + llm_ops
