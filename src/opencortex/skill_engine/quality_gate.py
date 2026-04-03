"""
Quality Gate — dual-layer validation for skill drafts.

Layer 1: Rule-based deterministic checks
Layer 2: LLM-based semantic checks (optional)

Runs on concrete SkillRecord AFTER evolution, BEFORE saving.
"""

import logging
import re
from typing import List, Optional

from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, QualityCheck, QualityReport,
)

logger = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')
STEP_PATTERN = re.compile(r'(?:^\d+\.|^#{1,3}\s+Step\s+\d)', re.MULTILINE)


class QualityGate:
    def __init__(self, llm=None):
        self._llm = llm

    def rule_check(self, skill: SkillRecord) -> QualityReport:
        """Layer 1: deterministic rule checks."""
        checks = []

        name_ok = bool(NAME_PATTERN.match(skill.name)) and len(skill.name) <= 50
        checks.append(QualityCheck(
            name="name_format", severity="ERROR", passed=name_ok,
            message="OK" if name_ok else f"Name '{skill.name}' must be lowercase-hyphenated, <= 50 chars",
        ))

        content_ok = len(skill.content) > 50
        checks.append(QualityCheck(
            name="content_length", severity="ERROR", passed=content_ok,
            message="OK" if content_ok else f"Content too short ({len(skill.content)} chars, need > 50)",
            fix_suggestion="Add detailed steps to the skill content",
        ))

        has_steps = bool(STEP_PATTERN.search(skill.content))
        checks.append(QualityCheck(
            name="has_steps", severity="ERROR", passed=has_steps,
            message="OK" if has_steps else "Content must contain numbered steps or ## Step sections",
        ))

        desc_ok = bool(skill.description and len(skill.description.strip()) > 0)
        checks.append(QualityCheck(
            name="has_description", severity="ERROR", passed=desc_ok,
            message="OK" if desc_ok else "Description is empty",
        ))

        cat_ok = skill.category.value in [m.value for m in SkillCategory]
        checks.append(QualityCheck(
            name="category_valid", severity="ERROR", passed=cat_ok,
            message="OK" if cat_ok else f"Invalid category: {skill.category}",
        ))

        token_ok = len(skill.content) < 20000
        checks.append(QualityCheck(
            name="token_budget", severity="WARNING", passed=token_ok,
            message="OK" if token_ok else f"Content too long ({len(skill.content)} chars)",
        ))

        empty_sections = bool(re.search(r'##\s+\S+.*\n##\s+\S+', skill.content))
        checks.append(QualityCheck(
            name="no_empty_sections", severity="WARNING", passed=not empty_sections,
            message="OK" if not empty_sections else "Found empty sections",
        ))

        errors = sum(1 for c in checks if not c.passed and c.severity == "ERROR")
        warnings = sum(1 for c in checks if not c.passed and c.severity == "WARNING")
        score = max(0, min(100, 100 - errors * 20 - warnings * 5))

        return QualityReport(score=score, checks=checks, errors=errors, warnings=warnings)

    async def evaluate(self, skill: SkillRecord) -> QualityReport:
        """Full evaluation: rule checks + optional LLM semantic checks."""
        report = self.rule_check(skill)

        if self._llm and report.score >= 40:
            try:
                semantic = await self._semantic_check(skill)
                report.checks.extend(semantic)
                sem_errors = sum(1 for c in semantic if not c.passed and c.severity == "ERROR")
                sem_warnings = sum(1 for c in semantic if not c.passed and c.severity == "WARNING")
                report.errors += sem_errors
                report.warnings += sem_warnings
                report.score = max(0, min(100, report.score - sem_errors * 20 - sem_warnings * 5))
            except Exception as exc:
                logger.warning("[QualityGate] LLM semantic check failed: %s", exc)

        return report

    async def _semantic_check(self, skill: SkillRecord) -> List[QualityCheck]:
        """Layer 2: LLM semantic checks."""
        prompt = (
            "Evaluate this skill for quality. Return JSON with boolean fields:\n"
            "- actionable: Can an agent follow these steps without ambiguity?\n"
            "- consistent: Does the description match the steps?\n"
            "- specific: Are steps concrete, not vague platitudes?\n"
            "- duplicate: Does this duplicate common knowledge that doesn't need a skill?\n\n"
            f"Skill name: {skill.name}\n"
            f"Description: {skill.description}\n"
            f"Content:\n{skill.content[:3000]}\n\n"
            'Return ONLY valid JSON: {"actionable": true/false, "consistent": true/false, '
            '"specific": true/false, "duplicate": true/false}'
        )

        import orjson
        if hasattr(self._llm, 'complete'):
            response = await self._llm.complete([{"role": "user", "content": prompt}])
        else:
            response = await self._llm([{"role": "user", "content": prompt}])
        data = orjson.loads(response)

        checks = []
        checks.append(QualityCheck(
            name="actionability", severity="ERROR",
            passed=data.get("actionable", True),
            message="OK" if data.get("actionable") else "Steps are ambiguous",
        ))
        checks.append(QualityCheck(
            name="consistency", severity="WARNING",
            passed=data.get("consistent", True),
            message="OK" if data.get("consistent") else "Description doesn't match content",
        ))
        checks.append(QualityCheck(
            name="specificity", severity="WARNING",
            passed=data.get("specific", True),
            message="OK" if data.get("specific") else "Steps are too vague",
        ))
        checks.append(QualityCheck(
            name="overlap", severity="ERROR",
            passed=not data.get("duplicate", False),
            message="OK" if not data.get("duplicate") else "Duplicates common knowledge",
        ))

        return checks
