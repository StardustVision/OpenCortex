"""
Skill Engine LLM prompts — extraction, evolution, and analysis.
"""

SKILL_EXTRACT_PROMPT = """You are analyzing a cluster of related memories to extract reusable operational skills.

## Memory Cluster
{cluster_content}

## Existing Skills (do NOT duplicate these)
{existing_skills}

## Instructions

1. Read all memories in the cluster carefully.
2. Identify repeated operational patterns — workflows, debugging procedures, deployment steps, or tool usage guides.
3. For each pattern found, produce a skill with:
   - name: concise, lowercase-hyphenated (max 50 chars)
   - description: one sentence
   - category: "workflow" | "tool_guide" | "pattern"
   - confidence: 0.0-1.0
   - content: Markdown instructions with numbered steps

4. **Conflict handling**: If memories show different approaches for the same step:
   - Do NOT pick one and discard the other
   - Merge into conditional branches:
     - Step N (choose by scenario):
       - Scenario A → approach 1 (from X memories, Y users)
       - Scenario B → approach 2 (from X memories, Y users)

5. Skip patterns that duplicate existing skills listed above.

## Output Format (JSON array)

```json
[
  {{
    "name": "skill-name",
    "description": "One sentence",
    "category": "workflow",
    "confidence": 0.85,
    "content": "# Skill Name\\n\\n1. Step one\\n2. Step two\\n...",
    "source_memory_ids": ["id1", "id2"]
  }}
]
```

Return an empty array [] if no reusable patterns are found."""

SKILL_EVOLVE_FIX_PROMPT = """You are fixing an existing operational skill.

## Current Skill
{current_content}

## What needs fixing
{direction}

## Instructions
1. Analyze the issue described above
2. Fix the affected content while preserving the overall structure
3. Keep the skill name and purpose intact
4. Be surgical — fix what's broken without unnecessary rewrites

## Output
Provide the complete fixed skill content in Markdown.
End with <EVOLUTION_COMPLETE> if the fix is satisfactory.
End with <EVOLUTION_FAILED> Reason: ... if you cannot complete the fix."""

SKILL_EVOLVE_DERIVED_PROMPT = """You are creating an enhanced version of an existing skill.

## Parent Skill
{parent_content}

## Enhancement Direction
{direction}

## Instructions
1. Create an improved version addressing the enhancement direction
2. Give a different, concise name (max 50 chars, lowercase, hyphens)
3. Should be self-contained (no reference to parent needed)
4. Preserve what works, improve what doesn't

## Output
Provide the complete enhanced skill content in Markdown.
End with <EVOLUTION_COMPLETE> if the derived skill is a meaningful improvement.
End with <EVOLUTION_FAILED> Reason: ... if not a worthwhile enhancement."""

SKILL_EVOLVE_CAPTURED_PROMPT = """You are creating a brand-new operational skill from observed patterns.

## Pattern to Capture
{direction}

## Category
{category}

## Source Context
{source_context}

## Instructions
1. Distill the observed pattern into clear, reusable instructions
2. Choose a concise name (max 50 chars, lowercase, hyphens)
3. Write a brief description
4. Structure as clear, actionable steps
5. Generalize — abstract away task-specific details

## Output
Provide the complete skill content in Markdown.
End with <EVOLUTION_COMPLETE> if the skill is genuinely reusable.
End with <EVOLUTION_FAILED> Reason: ... if the pattern is too task-specific."""
