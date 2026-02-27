# SPDX-License-Identifier: Apache-2.0
"""ACEngine — Implements HooksProtocol, assembles Skillbook."""

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from opencortex.ace.reflector import Reflector
from opencortex.ace.skill_manager import SkillManager
from opencortex.ace.skillbook import Skillbook
from opencortex.ace.types import HooksStats, LearnResult, ReflectorOutput, UpdateOperation
from opencortex.models.embedder.base import EmbedderBase
from opencortex.storage.viking_fs import VikingFS
from opencortex.storage.vikingdb_interface import VikingDBInterface

logger = logging.getLogger(__name__)

# Separator used in structured state strings: "q|||r|||a|||f"
_STATE_SEP = "|||"


class ACEngine:
    """Agentic Context Engine — Skillbook + Reflector + SkillManager.

    Implements the 9 HooksProtocol methods expected by MemoryOrchestrator._hooks.
    """

    def __init__(
        self,
        storage: VikingDBInterface,
        embedder: EmbedderBase,
        viking_fs: VikingFS,
        llm_fn: Optional[Callable] = None,
        tenant_id: str = "default",
        user_id: str = "default",
    ):
        dim = embedder.get_dimension() if hasattr(embedder, "get_dimension") else 1024
        prefix = f"opencortex://tenant/{tenant_id}/user/{user_id}/skillbooks"
        self._skillbook = Skillbook(
            storage=storage,
            embedder=embedder,
            viking_fs=viking_fs,
            prefix=prefix,
            embedding_dim=dim,
        )
        self._llm_fn = llm_fn
        self._reflector = Reflector(llm_fn) if llm_fn else None
        self._skill_manager = SkillManager(llm_fn) if llm_fn else None
        # In-memory trajectory buffer
        self._trajectories: Dict[str, dict] = {}

    async def init(self) -> None:
        """Initialize underlying Skillbook collection."""
        await self._skillbook.init()

    @property
    def skillbook(self) -> Skillbook:
        return self._skillbook

    # =========================================================================
    # HooksProtocol methods
    # =========================================================================

    async def learn(
        self,
        state: str,
        action: str,
        reward: float,
        available_actions: Optional[List[str]] = None,
    ) -> LearnResult:
        """Learn from execution feedback.

        With LLM: full Reflector → SkillManager → Apply pipeline.
        Without LLM: simple TAG-based learning (reward > 0 → helpful, < 0 → harmful).
        """
        question, reasoning, answer, feedback = self._parse_state(state, reward)

        if self._reflector is None or self._skill_manager is None:
            return await self._learn_simple(question, reward)

        try:
            return await self._learn_full(question, reasoning, answer, feedback)
        except Exception as e:
            logger.warning(f"[ACEngine] Full learn failed, falling back to simple: {e}")
            return await self._learn_simple(question, reward)

    async def remember(
        self,
        content: str,
        memory_type: str = "general",
    ) -> Dict[str, Any]:
        """Store content in the Skillbook as a skill."""
        skill = await self._skillbook.add_skill(section=memory_type, content=content)
        uri = f"{self._skillbook._prefix}/{skill.section}/{skill.id}"
        return {
            "success": True,
            "uri": uri,
            "skill_id": skill.id,
            "section": skill.section,
        }

    async def recall(
        self,
        query: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Search the Skillbook for relevant skills."""
        skills = await self._skillbook.search(query=query, limit=limit)
        return [
            {
                "content": s.content,
                "skill_id": s.id,
                "section": s.section,
                "helpful": s.helpful,
                "harmful": s.harmful,
            }
            for s in skills
        ]

    async def trajectory_begin(
        self,
        trajectory_id: str,
        initial_state: str,
    ) -> Dict[str, Any]:
        """Begin a learning trajectory."""
        self._trajectories[trajectory_id] = {
            "initial_state": initial_state,
            "steps": [],
            "completed": False,
        }
        return {"trajectory_id": trajectory_id}

    async def trajectory_step(
        self,
        trajectory_id: str,
        action: str,
        reward: float,
        next_state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add a step to a trajectory."""
        traj = self._trajectories.get(trajectory_id)
        if traj is None:
            return {"error": f"Trajectory {trajectory_id} not found"}

        traj["steps"].append({
            "action": action,
            "reward": reward,
            "next_state": next_state,
        })
        return {"step": len(traj["steps"])}

    async def trajectory_end(
        self,
        trajectory_id: str,
        quality_score: float,
    ) -> Dict[str, Any]:
        """End a trajectory with quality score. Triggers learn if steps exist."""
        traj = self._trajectories.get(trajectory_id)
        if traj is None:
            return {"error": f"Trajectory {trajectory_id} not found"}

        traj["completed"] = True
        traj["quality_score"] = quality_score

        result: Dict[str, Any] = {
            "trajectory_id": trajectory_id,
            "steps": len(traj["steps"]),
            "quality_score": quality_score,
        }

        # Trigger learn if there are steps
        if traj["steps"]:
            state = self._trajectory_to_state(traj)
            actions = ", ".join(s["action"] for s in traj["steps"])
            reward = (quality_score * 2) - 1  # Map [0,1] → [-1,1]
            learn_result = await self.learn(state=state, action=actions, reward=reward)
            result["learn_result"] = {
                "success": learn_result.success,
                "operations_applied": learn_result.operations_applied,
                "message": learn_result.message,
            }

        return result

    async def error_record(
        self,
        error: str,
        fix: str,
        context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record an error pattern and its fix as a skill."""
        skill = await self._skillbook.add_skill(
            section="error_fixes",
            content=fix,
            evidence=error,
            justification=context,
        )
        return {
            "success": True,
            "skill_id": skill.id,
            "section": skill.section,
        }

    async def error_suggest(
        self,
        error: str,
    ) -> List[Dict[str, Any]]:
        """Get suggested fixes for an error from learned patterns."""
        skills = await self._skillbook.search(
            query=error, limit=5, section="error_fixes"
        )
        return [
            {
                "fix": s.content,
                "error_pattern": s.evidence or "",
                "skill_id": s.id,
                "helpful": s.helpful,
                "harmful": s.harmful,
            }
            for s in skills
        ]

    async def stats(self) -> HooksStats:
        """Return HooksStats based on Skillbook statistics."""
        sb_stats = await self._skillbook.stats()
        by_section = sb_stats.get("by_section", {})
        return HooksStats(
            q_learning_patterns=by_section.get("strategies", 0) + by_section.get("patterns", 0),
            vector_memories=sb_stats.get("total", 0),
            learning_trajectories=len([t for t in self._trajectories.values() if t.get("completed")]),
            error_patterns=by_section.get("error_fixes", 0),
        )

    # =========================================================================
    # learn() internals
    # =========================================================================

    def _parse_state(
        self, state: str, reward: float
    ) -> Tuple[str, str, str, str]:
        """Parse state string into (question, reasoning, answer, feedback).

        Supports "q|||r|||a|||f" structured format or plain text fallback.
        """
        if _STATE_SEP in state:
            parts = state.split(_STATE_SEP)
            question = parts[0].strip() if len(parts) > 0 else state
            reasoning = parts[1].strip() if len(parts) > 1 else ""
            answer = parts[2].strip() if len(parts) > 2 else ""
            feedback = parts[3].strip() if len(parts) > 3 else ""
            return question, reasoning, answer, feedback

        # Plain text fallback
        feedback = "positive" if reward > 0 else ("negative" if reward < 0 else "neutral")
        return state, "", "", feedback

    async def _learn_simple(self, question: str, reward: float) -> LearnResult:
        """Simple TAG-based learning without LLM.

        Tags existing relevant skills based on reward sign.
        """
        tag = "helpful" if reward > 0 else ("harmful" if reward < 0 else "neutral")
        ops_applied = 0

        try:
            skills = await self._skillbook.search(query=question, limit=5)
            for skill in skills:
                op = UpdateOperation(
                    type="TAG",
                    section=skill.section,
                    skill_id=skill.id,
                    metadata={tag: 1},
                )
                await self._skillbook.apply(op)
                ops_applied += 1
        except Exception as e:
            logger.debug(f"[ACEngine] Simple learn tagging failed: {e}")

        return LearnResult(
            success=True,
            best_action="",
            message=f"simple: tagged {ops_applied} skills as {tag}",
            operations_applied=ops_applied,
        )

    async def _learn_full(
        self,
        question: str,
        reasoning: str,
        answer: str,
        feedback: str,
    ) -> LearnResult:
        """Full LLM-driven learn pipeline: Reflector → SkillManager → Apply."""
        # 1. Search for relevant skills
        skills = await self._skillbook.search(query=question, limit=10)

        # 2. Reflect
        reflection = await self._reflector.reflect(
            question=question,
            reasoning=reasoning,
            answer=answer,
            feedback=feedback,
            skills=skills,
        )

        # 3. Get skillbook state for SkillManager
        skillbook_state = await self._skillbook.as_prompt()

        # 4. Decide operations
        context = f"Question: {question}\nFeedback: {feedback}"
        operations = await self._skill_manager.decide(
            reflection=reflection,
            skillbook_state=skillbook_state,
            context=context,
        )

        # 5. Apply operations
        trace = self._build_trace(question, reasoning, answer, feedback, reflection)
        affected_sections = set()
        ops_applied = 0

        for op in operations:
            try:
                result = await self._skillbook.apply(op, trace=trace)
                ops_applied += 1
                # Track sections for summary update
                if op.section:
                    affected_sections.add(op.section)
                elif result and result.section:
                    affected_sections.add(result.section)
            except Exception as e:
                logger.warning(f"[ACEngine] Failed to apply operation {op.type}: {e}")

        # 6. Update affected section summaries
        for section in affected_sections:
            try:
                await self._skillbook.update_section_summary(section)
            except Exception as e:
                logger.debug(f"[ACEngine] Failed to update section summary for {section}: {e}")

        return LearnResult(
            success=True,
            best_action=answer,
            message=f"full: {ops_applied} operations applied",
            operations_applied=ops_applied,
            reflection_key_insight=reflection.key_insight,
        )

    def _build_trace(
        self,
        question: str,
        reasoning: str,
        answer: str,
        feedback: str,
        reflection: ReflectorOutput,
    ) -> str:
        """Build L2 markdown trace from execution context."""
        parts = [f"# Execution Trace\n"]
        parts.append(f"## Question\n{question}\n")
        if reasoning:
            parts.append(f"## Reasoning\n{reasoning}\n")
        if answer:
            parts.append(f"## Answer\n{answer}\n")
        parts.append(f"## Feedback\n{feedback}\n")
        parts.append(f"## Reflection\n")
        parts.append(f"**Key Insight**: {reflection.key_insight}\n")
        if reflection.error_identification != "none":
            parts.append(f"**Error**: {reflection.error_identification}\n")
        parts.append(f"**Root Cause**: {reflection.root_cause_analysis}\n")
        return "\n".join(parts)

    def _trajectory_to_state(self, traj: dict) -> str:
        """Convert trajectory data to a structured state string."""
        initial = traj.get("initial_state", "")
        steps = traj.get("steps", [])

        # Build question from initial state
        question = initial

        # Build reasoning from step sequence
        reasoning_parts = []
        for i, step in enumerate(steps):
            reward_str = f"reward={step['reward']}"
            reasoning_parts.append(f"Step {i + 1}: {step['action']} ({reward_str})")
        reasoning = "; ".join(reasoning_parts)

        # Last action as answer
        answer = steps[-1]["action"] if steps else ""

        # Quality as feedback
        quality = traj.get("quality_score", 0.5)
        if quality >= 0.7:
            feedback = f"positive (quality={quality})"
        elif quality <= 0.3:
            feedback = f"negative (quality={quality})"
        else:
            feedback = f"mixed (quality={quality})"

        return _STATE_SEP.join([question, reasoning, answer, feedback])
