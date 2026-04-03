"""
Sandbox TDD --- LLM-simulated RED-GREEN-REFACTOR for skill validation.

Default OFF --- enable via config. Generates pressure scenarios,
runs baseline (no skill) vs with-skill, compares behavior.
"""

import logging
from typing import List

import orjson

from opencortex.skill_engine.types import SkillRecord, TDDResult

logger = logging.getLogger(__name__)


class SandboxTDD:
    def __init__(self, llm, max_llm_calls: int = 20):
        self._llm = llm
        self._max_calls = max_llm_calls
        self._calls_used = 0

    async def evaluate(self, skill: SkillRecord) -> TDDResult:
        """Run RED-GREEN-REFACTOR cycle on a skill."""
        self._calls_used = 0

        scenarios = await self._generate_scenarios(skill)
        if not scenarios:
            return TDDResult(passed=False, llm_calls_used=self._calls_used)

        baseline = {}
        for s in scenarios:
            if self._calls_used >= self._max_calls:
                break
            baseline[s["scenario"]] = await self._run_baseline(s["scenario"])

        with_skill = {}
        for s in scenarios:
            if self._calls_used >= self._max_calls:
                break
            with_skill[s["scenario"]] = await self._run_with_skill(
                s["scenario"], skill.content
            )

        improved = same = worse = 0
        sections_cited: List[str] = []
        rationalizations: List[str] = []

        for s in scenarios:
            sc = s["scenario"]
            correct = s.get("correct", "A")
            b = baseline.get(sc, {})
            w = with_skill.get(sc, {})
            b_choice = b.get("choice", "")
            w_choice = w.get("choice", "")

            if w_choice == correct and b_choice != correct:
                improved += 1
            elif w_choice != correct and b_choice == correct:
                worse += 1
                rationalizations.append(w.get("reasoning", ""))
            else:
                same += 1

            sections_cited.extend(w.get("sections_cited", []))

        total = len(scenarios)
        passed = total > 0 and (improved >= total * 0.5) and (worse == 0)
        delta = improved / total if total > 0 else 0.0

        return TDDResult(
            passed=passed,
            scenarios_total=total,
            scenarios_improved=improved,
            scenarios_same=same,
            scenarios_worse=worse,
            sections_cited=sections_cited,
            rationalizations=rationalizations,
            quality_delta=delta,
            llm_calls_used=self._calls_used,
        )

    async def _llm_call(self, prompt: str) -> str:
        self._calls_used += 1
        if hasattr(self._llm, 'complete'):
            return await self._llm.complete([{"role": "user", "content": prompt}])
        return await self._llm([{"role": "user", "content": prompt}])

    async def _generate_scenarios(self, skill: SkillRecord) -> List[dict]:
        prompt = (
            f"Given this skill about {skill.name}:\n{skill.content[:2000]}\n\n"
            "Generate 2-3 realistic scenarios that test whether an agent would follow "
            "this skill correctly. Each scenario should:\n"
            "- Present a concrete situation with A/B/C options\n"
            "- Include time pressure or competing priorities\n"
            "- Have one clearly correct answer per the skill\n"
            "- Be answerable without external tools\n\n"
            'Return JSON array: [{"scenario": "...", "correct": "A"}]'
        )
        try:
            return orjson.loads(await self._llm_call(prompt))
        except Exception:
            return []

    async def _run_baseline(self, scenario: str) -> dict:
        prompt = (
            f"You are an AI assistant. A user asks:\n{scenario}\n"
            "Choose an option and explain your reasoning.\n"
            'Return JSON: {"choice": "A/B/C", "reasoning": "..."}'
        )
        try:
            return orjson.loads(await self._llm_call(prompt))
        except Exception:
            return {}

    async def _run_with_skill(self, scenario: str, skill_content: str) -> dict:
        prompt = (
            f"You are an AI assistant with this operational skill:\n"
            f"{skill_content[:2000]}\n\n"
            f"A user asks:\n{scenario}\n"
            "Choose an option and explain your reasoning. "
            "Cite which sections guided your choice.\n"
            'Return JSON: {"choice": "A/B/C", "reasoning": "...", '
            '"sections_cited": ["..."]}'
        )
        try:
            return orjson.loads(await self._llm_call(prompt))
        except Exception:
            return {}
