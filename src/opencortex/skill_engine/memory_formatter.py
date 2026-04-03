"""
Format memory clusters for LLM context in skill extraction.

Adapted from OpenSpace's conversation_formatter.py — formats memories
instead of execution conversations.
"""

from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from opencortex.skill_engine.adapters.source_adapter import MemoryRecord
    from opencortex.skill_engine.types import SkillRecord

MAX_MEMORY_CHARS = 3000
MAX_CLUSTER_CHARS = 60000


def format_cluster_for_extraction(
    memories: List["MemoryRecord"],
    max_chars: int = MAX_CLUSTER_CHARS,
) -> str:
    """Format a memory cluster for the extraction LLM prompt."""
    parts = []
    total = 0
    for i, m in enumerate(memories):
        section = f"### Memory {i+1} [{m.memory_id}]\n"
        section += f"**Type**: {m.context_type} / {m.category}\n"
        if m.abstract:
            section += f"**Summary**: {m.abstract}\n"
        if m.overview:
            section += f"**Overview**: {m.overview}\n\n"
        if m.content:
            content = m.content[:MAX_MEMORY_CHARS]
            if len(m.content) > MAX_MEMORY_CHARS:
                content += "\n... (truncated)"
            section += f"{content}\n"
        section += "\n---\n\n"

        if total + len(section) > max_chars:
            break
        parts.append(section)
        total += len(section)

    return "".join(parts)


def format_existing_skills(
    skills: List["SkillRecord"],
    max_chars: int = 10000,
) -> str:
    """Format existing active skills for dedup context."""
    if not skills:
        return "(No existing skills)"
    parts = []
    total = 0
    for s in skills:
        line = f"- **{s.name}** ({s.category.value}): {s.description}\n"
        if total + len(line) > max_chars:
            break
        parts.append(line)
        total += len(line)
    return "".join(parts)
