# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""
RuVector Hooks Client - Native self-learning capabilities.

Wraps npx ruvector hooks commands:
- remember/recall: Semantic memory storage and retrieval
- learn/batch-learn: Q-learning with reward signals
- trajectory-*: Execution trajectory tracking
- error-*: Error pattern learning
- watch: Real-time auto-learning
"""

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class LearningResult:
    """Result from a learn operation."""
    success: bool
    state: str
    best_action: Optional[str] = None
    message: str = ""


@dataclass
class TrajectoryStep:
    """A single step in a trajectory."""
    step_id: str
    state: str
    action: str
    reward: float
    timestamp: float


@dataclass
class HooksStats:
    """Statistics from ruvector hooks."""
    q_learning_patterns: int = 0
    vector_memories: int = 0
    learning_trajectories: int = 0
    error_patterns: int = 0


class RuVectorHooksError(RuntimeError):
    """Raised when ruvector hooks CLI fails."""
    pass


class RuVectorHooks:
    """
    Client for RuVector native self-learning hooks.

    Wraps npx ruvector hooks commands to provide:
    - Semantic memory (remember/recall)
    - Q-learning (learn/batch-learn)
    - Trajectory tracking (trajectory-begin/step/end)
    - Error pattern learning (error-record/suggest)
    - Auto-learning (watch)
    """

    def __init__(
        self,
        data_dir: str = "./data/ruvector",
        cli_path: str = "npx",
        timeout: int = 30,
    ):
        self.data_dir = data_dir
        self.cli_path = cli_path
        self.timeout = timeout
        self._initialized = False

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _run(
        self,
        args: List[str],
        stdin_data: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute npx ruvector hooks <args> and return parsed JSON."""
        cmd = [self.cli_path, "ruvector", "hooks"] + args
        logger.debug("ruvector hooks command: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.data_dir,
            )
        except subprocess.TimeoutExpired:
            raise RuVectorHooksError(f"Command timed out after {self.timeout}s")
        except FileNotFoundError:
            raise RuVectorHooksError(
                f"CLI not found: {self.cli_path}. Install ruvector first."
            )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            # Some commands output to stdout even on error
            stdout = result.stdout.strip()
            if stdout and not stdout.startswith("Error"):
                try:
                    return json.loads(stdout)
                except json.JSONDecodeError:
                    pass
            raise RuVectorHooksError(f"Command failed: {stderr}")

        stdout = result.stdout.strip()
        if not stdout:
            return {}

        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            # Some commands return plain text
            return {"output": stdout}

    def _run_async(self, args: List[str], stdin_data: Optional[str] = None):
        """Async wrapper for _run."""
        return asyncio.to_thread(self._run, args, stdin_data)

    # -------------------------------------------------------------------------
    # Memory: remember / recall
    # -------------------------------------------------------------------------

    async def remember(
        self,
        content: str,
        memory_type: str = "general",
        silent: bool = True,
    ) -> Dict[str, Any]:
        """
        Store content in semantic memory.

        Args:
            content: The content to remember
            memory_type: Type of memory (default: "general")
            silent: Suppress output

        Returns:
            Dict with success status
        """
        args = ["remember"]
        if memory_type != "general":
            args.extend(["--type", memory_type])
        if silent:
            args.append("--silent")
        args.append("--")
        args.append(content)

        try:
            result = await self._run_async(args)
            return {"success": True, "content": content}
        except RuVectorHooksError as e:
            logger.warning("remember failed: %s", e)
            return {"success": False, "error": str(e)}

    async def recall(
        self,
        query: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Search semantic memory for relevant content.

        Args:
            query: Search query
            limit: Maximum results

        Returns:
            List of matching memories
        """
        args = ["recall", "--limit", str(limit), "--"]
        args.extend(query.split())

        try:
            result = await self._run_async(args)
            # Parse recall output
            output = result.get("output", "")
            if not output:
                return []
            # Return as list of matches
            return [{"content": line, "score": 1.0} for line in output.split("\n") if line]
        except RuVectorHooksError as e:
            logger.warning("recall failed: %s", e)
            return []

    # -------------------------------------------------------------------------
    # Q-learning: learn / batch-learn
    # -------------------------------------------------------------------------

    async def learn(
        self,
        state: str,
        action: str,
        reward: float,
        available_actions: Optional[List[str]] = None,
        task_type: str = "agent-routing",
    ) -> LearningResult:
        """
        Record a learning outcome and get best action recommendation.

        Args:
            state: Current state (e.g., URI, context_type)
            action: Action taken
            reward: Reward value (-1 to 1)
            available_actions: List of available actions
            task_type: Type of task

        Returns:
            LearningResult with best action recommendation
        """
        args = ["learn"]
        args.extend(["--state", state])
        args.extend(["--action", action])
        args.extend(["--reward", str(reward)])
        if available_actions:
            args.extend(["--actions", ",".join(available_actions)])
        args.extend(["--task", task_type])

        try:
            result = await self._run_async(args)
            return LearningResult(
                success=True,
                state=state,
                best_action=result.get("recommended_action"),
                message=result.get("message", ""),
            )
        except RuVectorHooksError as e:
            logger.warning("learn failed: %s", e)
            return LearningResult(
                success=False,
                state=state,
                message=str(e),
            )

    async def batch_learn(
        self,
        experiences: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Record multiple learning experiences in batch.

        Args:
            experiences: List of {state, action, reward} dicts

        Returns:
            Dict with batch results
        """
        args = ["batch-learn"]

        # Build experiences as JSON stdin
        payload = {"experiences": experiences}
        stdin = json.dumps(payload)

        try:
            result = await self._run_async(args, stdin_data=stdin)
            return {"success": True, "count": len(experiences)}
        except RuVectorHooksError as e:
            logger.warning("batch_learn failed: %s", e)
            return {"success": False, "error": str(e)}

    # -------------------------------------------------------------------------
    # Trajectory tracking
    # -------------------------------------------------------------------------

    async def trajectory_begin(
        self,
        trajectory_id: str,
        initial_state: str,
    ) -> Dict[str, Any]:
        """
        Begin tracking a new execution trajectory.

        Args:
            trajectory_id: Unique trajectory identifier
            initial_state: Starting state

        Returns:
            Dict with trajectory info
        """
        args = [
            "trajectory-begin",
            "--id", trajectory_id,
            "--state", initial_state,
        ]

        try:
            result = await self._run_async(args)
            return {"success": True, "trajectory_id": trajectory_id}
        except RuVectorHooksError as e:
            logger.warning("trajectory_begin failed: %s", e)
            return {"success": False, "error": str(e)}

    async def trajectory_step(
        self,
        trajectory_id: str,
        action: str,
        reward: float,
        next_state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Add a step to the current trajectory.

        Args:
            trajectory_id: Trajectory identifier
            action: Action taken
            reward: Reward for this step
            next_state: Resulting state

        Returns:
            Dict with step info
        """
        args = [
            "trajectory-step",
            "--id", trajectory_id,
            "--action", action,
            "--reward", str(reward),
        ]
        if next_state:
            args.extend(["--state", next_state])

        try:
            result = await self._run_async(args)
            return {"success": True, "trajectory_id": trajectory_id}
        except RuVectorHooksError as e:
            logger.warning("trajectory_step failed: %s", e)
            return {"success": False, "error": str(e)}

    async def trajectory_end(
        self,
        trajectory_id: str,
        quality_score: float,
    ) -> Dict[str, Any]:
        """
        End the current trajectory with a quality score.

        Args:
            trajectory_id: Trajectory identifier
            quality_score: Overall quality score (0-1)

        Returns:
            Dict with trajectory summary
        """
        args = [
            "trajectory-end",
            "--id", trajectory_id,
            "--score", str(quality_score),
        ]

        try:
            result = await self._run_async(args)
            return {
                "success": True,
                "trajectory_id": trajectory_id,
                "quality_score": quality_score,
            }
        except RuVectorHooksError as e:
            logger.warning("trajectory_end failed: %s", e)
            return {"success": False, "error": str(e)}

    # -------------------------------------------------------------------------
    # Error pattern learning
    # -------------------------------------------------------------------------

    async def error_record(
        self,
        error: str,
        fix: str,
        context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Record an error and its fix for learning.

        Args:
            error: Error message or code
            fix: Fix applied
            context: Optional context information

        Returns:
            Dict with record status
        """
        args = ["error-record", "--error", error, "--fix", fix]
        if context:
            args.extend(["--context", context])

        try:
            result = await self._run_async(args)
            return {"success": True, "error": error}
        except RuVectorHooksError as e:
            logger.warning("error_record failed: %s", e)
            return {"success": False, "error": str(e)}

    async def error_suggest(
        self,
        error: str,
    ) -> List[Dict[str, Any]]:
        """
        Get suggested fixes for an error based on learned patterns.

        Args:
            error: Error message or code

        Returns:
            List of suggested fixes
        """
        args = ["error-suggest", "--error", error]

        try:
            result = await self._run_async(args)
            output = result.get("output", "")
            if not output:
                return []
            # Parse suggestions
            suggestions = []
            for line in output.split("\n"):
                if line.strip():
                    suggestions.append({"fix": line.strip()})
            return suggestions
        except RuVectorHooksError as e:
            logger.warning("error_suggest failed: %s", e)
            return []

    # -------------------------------------------------------------------------
    # Auto-learning (watch mode)
    # -------------------------------------------------------------------------

    async def watch(
        self,
        watch_path: str = ".",
        auto_learn: bool = True,
    ) -> Dict[str, Any]:
        """
        Watch for changes and auto-learn patterns in real-time.

        Args:
            watch_path: Path to watch
            auto_learn: Enable auto-learning

        Returns:
            Dict with watch status
        """
        args = ["watch", "--path", watch_path]
        if auto_learn:
            args.append("--auto-learn")

        try:
            result = await self._run_async(args)
            return {"success": True, "watching": watch_path}
        except RuVectorHooksError as e:
            logger.warning("watch failed: %s", e)
            return {"success": False, "error": str(e)}

    # -------------------------------------------------------------------------
    # Statistics
    # -------------------------------------------------------------------------

    async def stats(self) -> HooksStats:
        """
        Get intelligence statistics.

        Returns:
            HooksStats with current counts
        """
        args = ["stats"]

        try:
            result = await self._run_async(args)
            return HooksStats(
                q_learning_patterns=result.get("q_learning_patterns", 0),
                vector_memories=result.get("vector_memories", 0),
                learning_trajectories=result.get("learning_trajectories", 0),
                error_patterns=result.get("error_patterns", 0),
            )
        except RuVectorHooksError as e:
            logger.warning("stats failed: %s", e)
            return HooksStats()

    async def learning_stats(self) -> Dict[str, Any]:
        """
        Get learning algorithm statistics.

        Returns:
            Dict with detailed learning stats
        """
        args = ["learning-stats"]

        try:
            result = await self._run_async(args)
            return result
        except RuVectorHooksError as e:
            logger.warning("learning_stats failed: %s", e)
            return {}

    # -------------------------------------------------------------------------
    # Utility methods
    # -------------------------------------------------------------------------

    async def verify(self) -> Dict[str, Any]:
        """
        Verify hooks are working correctly.

        Returns:
            Dict with verification status
        """
        args = ["verify"]

        try:
            result = await self._run_async(args)
            return {"success": True, "details": result}
        except RuVectorHooksError as e:
            return {"success": False, "error": str(e)}

    async def force_learn(self) -> Dict[str, Any]:
        """
        Force an immediate learning cycle.

        Returns:
            Dict with learning result
        """
        args = ["force-learn"]

        try:
            result = await self._run_async(args)
            return {"success": True}
        except RuVectorHooksError as e:
            logger.warning("force_learn failed: %s", e)
            return {"success": False, "error": str(e)}
