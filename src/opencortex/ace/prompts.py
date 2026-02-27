# SPDX-License-Identifier: Apache-2.0
"""Prompt templates for Reflector and SkillManager."""

from typing import List

from opencortex.ace.types import Skill


def build_reflector_prompt(
    question: str,
    reasoning: str,
    answer: str,
    feedback: str,
    skills_excerpt: str,
) -> str:
    """Build the Reflector analysis prompt.

    Args:
        question: The original question/task
        reasoning: The agent's reasoning process
        answer: The agent's answer/action
        feedback: Outcome feedback (success/failure details)
        skills_excerpt: Tab-separated table of relevant skills

    Returns:
        Formatted prompt string
    """
    return f"""You are a Reflector in an Agentic Context Engine. Your job is to analyze an agent's execution and extract reusable learnings.

## Execution Record

**Question/Task**: {question}

**Reasoning**: {reasoning}

**Answer/Action**: {answer}

**Feedback**: {feedback}

## Existing Skills Context

{skills_excerpt if skills_excerpt else "(no existing skills)"}

## Diagnostic Protocol

Classify the outcome using this protocol:
1. **SUCCESS** — The answer was correct and feedback is positive
2. **PARTIAL_SUCCESS** — Mostly correct but with minor issues
3. **WRONG_STRATEGY** — Approach was fundamentally wrong
4. **WRONG_EXECUTION** — Right approach, wrong execution
5. **MISSING_KNOWLEDGE** — Lacked necessary information
6. **MISSING_STRATEGY** — No applicable strategy existed

## Instructions

1. Analyze the execution thoroughly
2. Identify the root cause of success or failure
3. Extract concrete, reusable learnings (imperative sentences, <20 words each)
4. Tag existing skills as helpful/harmful/neutral based on this execution
5. Do NOT give vague or generic advice — every learning must have specific evidence

## Output Format

Return a single JSON object:

```json
{{
    "reasoning": "Step-by-step analysis of what happened",
    "error_identification": "none" or "specific error description",
    "root_cause_analysis": "Why the outcome occurred",
    "key_insight": "The single most important takeaway",
    "extracted_learnings": [
        {{
            "learning": "Imperative sentence (<20 words)",
            "evidence": "Concrete evidence from this execution",
            "justification": "Why this generalizes beyond this case"
        }}
    ],
    "skill_tags": [
        {{
            "skill_id": "existing-skill-id",
            "tag": "helpful|harmful|neutral"
        }}
    ]
}}
```

Output JSON only:"""


def build_skill_manager_prompt(
    skillbook_state: str,
    reflection_json: str,
    context: str,
) -> str:
    """Build the SkillManager decision prompt.

    Args:
        skillbook_state: Current skillbook as tab-separated table
        reflection_json: Reflector output as JSON string
        context: Additional context (question, feedback summary)

    Returns:
        Formatted prompt string
    """
    return f"""You are a SkillManager in an Agentic Context Engine. Based on a Reflector's analysis, decide how to update the Skillbook.

## Current Skillbook

{skillbook_state if skillbook_state else "(empty skillbook)"}

## Reflector Analysis

{reflection_json}

## Context

{context}

## Available Operations

- **ADD**: Add a new skill. Requires `section` and `content`. Use for genuinely new knowledge.
- **UPDATE**: Modify an existing skill. Requires `skill_id` and new `content`. Prefer UPDATE over ADD when a similar skill exists.
- **REMOVE**: Remove a skill that is proven wrong. Requires `skill_id`.

## Rules

1. **UPDATE over ADD**: If a similar skill exists, update it rather than adding a duplicate
2. **Atomic operations**: Each operation should be self-contained
3. **Evidence required**: Every ADD/UPDATE must have evidence from the execution
4. **Sections**: strategies, error_fixes, patterns, general
5. **Conservative**: Only make changes supported by strong evidence

## Output Format

Return a JSON array of operations:

```json
[
    {{
        "type": "ADD",
        "section": "strategies",
        "content": "Imperative sentence describing the skill",
        "justification": "Why this is useful",
        "evidence": "Evidence from the execution"
    }},
    {{
        "type": "UPDATE",
        "skill_id": "strat-00001",
        "section": "strategies",
        "content": "Updated skill content",
        "justification": "Why this update is needed",
        "evidence": "Evidence from the execution"
    }},
    {{
        "type": "REMOVE",
        "skill_id": "strat-00002",
        "section": "strategies"
    }}
]
```

If no changes are needed, return an empty array: []

Output JSON only:"""


def format_skills_excerpt(skills: List[Skill]) -> str:
    """Format a list of skills as a tab-separated table for prompt injection.

    Args:
        skills: List of Skill objects

    Returns:
        Tab-separated table string
    """
    if not skills:
        return ""
    lines = ["ID\tSection\tContent\tHelpful\tHarmful"]
    for s in skills:
        lines.append(f"{s.id}\t{s.section}\t{s.content}\t{s.helpful}\t{s.harmful}")
    return "\n".join(lines)
