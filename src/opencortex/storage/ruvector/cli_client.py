# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""
Subprocess wrapper for rvf-cli (RuVector CLI).

All methods are synchronous; async callers should wrap with asyncio.to_thread().
The binary is located lazily — if ``rvf`` is not installed, a clear error is
raised on the first operation rather than at construction time.
"""

import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from opencortex.storage.ruvector.types import RuVectorConfig

logger = logging.getLogger(__name__)

# Sentinel value used to detect a missing CLI binary.
_BINARY_NOT_FOUND = object()


class RuVectorCLIError(RuntimeError):
    """Raised when rvf-cli returns a non-zero exit code or cannot be found."""


class RuVectorCLI:
    """
    Thin subprocess wrapper around the ``rvf`` CLI binary.

    The CLI is expected to accept JSON payloads on stdin for large inputs and
    return JSON on stdout.  If the binary is not installed the client raises a
    :class:`RuVectorCLIError` on the first operation.
    """

    def __init__(self, config: RuVectorConfig) -> None:
        self.config = config
        self._initialized = False

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _run(
        self,
        args: List[str],
        stdin_data: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute ``rvf <args>`` and return parsed JSON output.

        Args:
            args: CLI sub-command and flags (e.g. ``["search", "--top-k", "5"]``).
            stdin_data: Optional JSON string to send on stdin for large payloads.

        Returns:
            Parsed JSON dict from stdout.

        Raises:
            RuVectorCLIError: Binary not found or non-zero exit code.
        """
        cmd = [self.config.cli_path] + args
        logger.debug("rvf-cli command: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=self.config.cli_timeout,
            )
        except FileNotFoundError:
            raise RuVectorCLIError(
                f"rvf CLI binary not found at '{self.config.cli_path}'. "
                "Please install RuVector and ensure 'rvf' is on your PATH, "
                "or set RuVectorConfig.cli_path to the correct binary location."
            )
        except subprocess.TimeoutExpired:
            raise RuVectorCLIError(
                f"rvf-cli timed out after {self.config.cli_timeout}s "
                f"running: {' '.join(cmd)}"
            )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuVectorCLIError(
                f"rvf-cli exited with code {result.returncode}. "
                f"stderr: {stderr}"
            )

        stdout = result.stdout.strip()
        if not stdout:
            return {}

        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuVectorCLIError(
                f"rvf-cli returned invalid JSON: {stdout[:200]}"
            ) from exc

    # -------------------------------------------------------------------------
    # Initialisation
    # -------------------------------------------------------------------------

    def init_db(self) -> None:
        """
        Initialise the RuVector database directory.

        Runs ``rvf init`` once; subsequent calls are no-ops.
        """
        if self._initialized:
            return
        os.makedirs(self.config.data_dir, exist_ok=True)
        self._run([
            "init",
            "--data-dir", self.config.data_dir,
            "--dimension", str(self.config.dimension),
            "--distance", self.config.distance_metric,
        ])
        self._initialized = True
        logger.info("RuVector DB initialised at %s", self.config.data_dir)

    def _ensure_init(self) -> None:
        """Lazily initialise the DB on first use."""
        if not self._initialized:
            self.init_db()

    # -------------------------------------------------------------------------
    # Single-record CRUD
    # -------------------------------------------------------------------------

    def insert(
        self,
        id: str,
        vector: List[float],
        content: str,
        metadata: Dict[str, Any],
    ) -> str:
        """
        Insert a single record.

        The vector and metadata are sent via stdin to avoid command-line length
        limits.

        Returns:
            The record ID.
        """
        self._ensure_init()
        payload = {
            "id": id,
            "vector": vector,
            "content": content,
            "metadata": metadata,
        }
        self._run(
            ["insert", "--data-dir", self.config.data_dir],
            stdin_data=json.dumps(payload),
        )
        return id

    def insert_batch(self, entries: List[Dict[str, Any]]) -> List[str]:
        """
        Batch insert multiple records.

        Args:
            entries: List of dicts with keys ``id``, ``vector``, ``content``,
                     ``metadata``.

        Returns:
            List of inserted IDs.
        """
        self._ensure_init()
        payload = {"entries": entries}
        self._run(
            ["insert-batch", "--data-dir", self.config.data_dir],
            stdin_data=json.dumps(payload),
        )
        return [e["id"] for e in entries]

    def upsert(
        self,
        id: str,
        vector: List[float],
        content: str,
        metadata: Dict[str, Any],
    ) -> str:
        """
        Insert or update a single record.

        Returns:
            The record ID.
        """
        self._ensure_init()
        payload = {
            "id": id,
            "vector": vector,
            "content": content,
            "metadata": metadata,
        }
        self._run(
            ["upsert", "--data-dir", self.config.data_dir],
            stdin_data=json.dumps(payload),
        )
        return id

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve a single record by ID.

        Returns:
            Record dict, or ``None`` if not found.
        """
        self._ensure_init()
        try:
            result = self._run([
                "get",
                "--data-dir", self.config.data_dir,
                "--id", id,
            ])
            return result if result else None
        except RuVectorCLIError as exc:
            # Treat "not found" errors as None rather than an exception.
            if "not found" in str(exc).lower():
                return None
            raise

    def get_batch(self, ids: List[str]) -> List[Dict[str, Any]]:
        """
        Retrieve multiple records by their IDs.

        Returns:
            List of found records (may be shorter than ``ids``).
        """
        self._ensure_init()
        payload = {"ids": ids}
        result = self._run(
            ["get-batch", "--data-dir", self.config.data_dir],
            stdin_data=json.dumps(payload),
        )
        return result.get("entries", [])

    def delete(self, id: str) -> bool:
        """
        Delete a single record by ID.

        Returns:
            True if the record was deleted, False if it did not exist.
        """
        self._ensure_init()
        try:
            self._run([
                "delete",
                "--data-dir", self.config.data_dir,
                "--id", id,
            ])
            return True
        except RuVectorCLIError as exc:
            if "not found" in str(exc).lower():
                return False
            raise

    def delete_batch(self, ids: List[str]) -> int:
        """
        Delete multiple records.

        Returns:
            Number of records successfully deleted.
        """
        self._ensure_init()
        payload = {"ids": ids}
        result = self._run(
            ["delete-batch", "--data-dir", self.config.data_dir],
            stdin_data=json.dumps(payload),
        )
        return int(result.get("deleted_count", 0))

    def update_metadata(self, id: str, metadata: Dict[str, Any]) -> bool:
        """
        Partially update the metadata of an existing record.

        Returns:
            True on success, False if the record was not found.
        """
        self._ensure_init()
        payload = {"id": id, "metadata": metadata}
        try:
            self._run(
                ["update-metadata", "--data-dir", self.config.data_dir],
                stdin_data=json.dumps(payload),
            )
            return True
        except RuVectorCLIError as exc:
            if "not found" in str(exc).lower():
                return False
            raise

    # -------------------------------------------------------------------------
    # Search
    # -------------------------------------------------------------------------

    def search(
        self,
        vector: List[float],
        top_k: int = 10,
        filter_dict: Optional[Dict[str, Any]] = None,
        use_reinforcement: bool = True,
        min_score: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Nearest-neighbour search.

        Args:
            vector: Dense query vector.
            top_k: Maximum number of results.
            filter_dict: Metadata equality filter forwarded to rvf-cli.
            use_reinforcement: Whether to apply SONA reinforcement scoring.
            min_score: Minimum similarity score threshold.

        Returns:
            List of result dicts (each has ``id``, ``similarity_score``,
            optionally ``reinforced_score``, and ``metadata``).
        """
        self._ensure_init()
        payload: Dict[str, Any] = {
            "vector": vector,
            "top_k": top_k,
            "use_reinforcement": use_reinforcement,
        }
        if filter_dict:
            payload["filter"] = filter_dict
        if min_score is not None:
            payload["min_score"] = min_score

        result = self._run(
            ["search", "--data-dir", self.config.data_dir],
            stdin_data=json.dumps(payload),
        )
        return result.get("results", [])

    # -------------------------------------------------------------------------
    # Aggregation
    # -------------------------------------------------------------------------

    def count(self) -> int:
        """
        Return the total number of records in the database.
        """
        self._ensure_init()
        result = self._run(["count", "--data-dir", self.config.data_dir])
        return int(result.get("count", 0))

    def stats(self) -> Dict[str, Any]:
        """
        Return storage statistics from rvf-cli.
        """
        self._ensure_init()
        return self._run(["stats", "--data-dir", self.config.data_dir])

    # -------------------------------------------------------------------------
    # SONA reinforcement interface
    # -------------------------------------------------------------------------

    def update_reward(self, id: str, reward: float) -> None:
        """
        Submit a reward signal for a single record.

        Args:
            id: Record ID (with collection prefix).
            reward: Scalar reward value (positive for good, negative for bad).
        """
        self._ensure_init()
        payload = {"id": id, "reward": reward}
        self._run(
            ["sona", "reward", "--data-dir", self.config.data_dir],
            stdin_data=json.dumps(payload),
        )

    def update_reward_batch(self, rewards: List[Tuple[str, float]]) -> None:
        """
        Submit reward signals for multiple records in one call.

        Args:
            rewards: List of ``(id, reward)`` tuples.
        """
        self._ensure_init()
        payload = {
            "rewards": [{"id": id, "reward": reward} for id, reward in rewards]
        }
        self._run(
            ["sona", "reward-batch", "--data-dir", self.config.data_dir],
            stdin_data=json.dumps(payload),
        )

    def get_profile(self, id: str) -> Dict[str, Any]:
        """
        Retrieve the SONA behavior profile for a record.

        Returns:
            Profile dict with keys: ``reward_score``, ``retrieval_count``,
            ``positive_feedback_count``, ``negative_feedback_count``,
            ``last_retrieved_at``, ``last_feedback_at``, ``effective_score``,
            ``is_protected``.
        """
        self._ensure_init()
        result = self._run([
            "sona", "profile",
            "--data-dir", self.config.data_dir,
            "--id", id,
        ])
        return result

    def apply_decay(self) -> Dict[str, Any]:
        """
        Run time-decay across all records and return a summary.

        Returns:
            Dict with keys: ``records_processed``, ``records_decayed``,
            ``records_below_threshold``, ``records_archived``.
        """
        self._ensure_init()
        result = self._run([
            "sona", "decay",
            "--data-dir", self.config.data_dir,
            "--decay-rate", str(self.config.sona_decay_rate),
            "--protected-decay-rate", str(self.config.sona_protected_decay_rate),
            "--min-score", str(self.config.sona_min_score),
        ])
        return result
