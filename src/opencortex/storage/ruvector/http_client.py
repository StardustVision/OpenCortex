# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""
HTTP client for the RuVector server (ruvector-server.js).

All methods are synchronous; async callers should wrap with asyncio.to_thread().
The server is started independently — this client does not manage the process.
"""

import json
import logging
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from opencortex.storage.ruvector.types import RuVectorConfig

logger = logging.getLogger(__name__)


async def check_ruvector_health(host: str, port: int, timeout: float = 3.0) -> dict:
    """Check if RuVector server is reachable.

    Makes an HTTP GET request to http://{host}:{port}/health (falling back to
    the root path ``/``) and returns a summary of reachability.

    Args:
        host: RuVector server hostname or IP address.
        port: RuVector server port number.
        timeout: Connection/read timeout in seconds (default 3.0).

    Returns:
        dict with keys:
            available (bool)  — True if the server responded successfully.
            version (str or None) — Server version string if reported.
            error (str or None)   — Human-readable error message when unavailable.
    """
    import asyncio

    def _do_check() -> dict:
        url = f"http://{host}:{port}/health"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    data = {}
                version = data.get("version") or data.get("v") or None
                return {"available": True, "version": version, "error": None}
        except urllib.error.HTTPError as exc:
            # A non-2xx HTTP status is still a sign the server is *up*, so treat
            # anything below 500 as "reachable" (e.g. 404 if /health is absent).
            if exc.code < 500:
                return {"available": True, "version": None, "error": None}
            return {
                "available": False,
                "version": None,
                "error": f"HTTP {exc.code}",
            }
        except urllib.error.URLError as exc:
            return {
                "available": False,
                "version": None,
                "error": str(exc.reason),
            }
        except (OSError, socket.timeout) as exc:
            return {
                "available": False,
                "version": None,
                "error": str(exc),
            }

    return await asyncio.to_thread(_do_check)


class RuVectorHTTPError(RuntimeError):
    """Raised when the RuVector HTTP server returns an error."""


class RuVectorHTTPClient:
    """
    HTTP client for the RuVector server.

    Connects to the standalone ruvector-server.js process via HTTP.
    No subprocess management — the server must be started independently.
    """

    def __init__(self, config: RuVectorConfig) -> None:
        self.config = config
        self._base_url = (
            f"http://{config.server_host}:{config.server_port}"
        )
        self._timeout = config.cli_timeout

    def _request(
        self,
        path: str,
        method: str = "POST",
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send an HTTP request and return the parsed JSON response."""
        url = f"{self._base_url}{path}"
        body = json.dumps(data).encode("utf-8") if data else None

        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuVectorHTTPError(
                f"RuVector server returned {exc.code}: {body_text}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuVectorHTTPError(
                f"Cannot connect to RuVector server at {self._base_url}: {exc}"
            ) from exc

    # -------------------------------------------------------------------------
    # Initialisation (no-op: DB is managed by the server)
    # -------------------------------------------------------------------------

    def init_db(self) -> None:
        """No-op: the server manages DB lifecycle."""

    def _ensure_init(self) -> None:
        """No-op: the server manages DB lifecycle."""

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
        """Insert a single record."""
        meta = dict(metadata)
        if content:
            meta["_content"] = content
        self._request("/insert", data={"id": id, "vector": vector, "metadata": meta})
        return id

    def insert_batch(self, entries: List[Dict[str, Any]]) -> List[str]:
        """Batch insert multiple records."""
        formatted = []
        for e in entries:
            meta = dict(e.get("metadata", {}))
            if e.get("content"):
                meta["_content"] = e["content"]
            formatted.append(
                {"id": e["id"], "vector": e["vector"], "metadata": meta}
            )
        self._request("/insert-batch", data={"entries": formatted})
        return [e["id"] for e in entries]

    def upsert(
        self,
        id: str,
        vector: List[float],
        content: str,
        metadata: Dict[str, Any],
    ) -> str:
        """Insert or update a single record."""
        meta = dict(metadata)
        if content:
            meta["_content"] = content
        self._request("/upsert", data={"id": id, "vector": vector, "metadata": meta})
        return id

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a single record by ID."""
        try:
            return self._request("/get", data={"id": id})
        except RuVectorHTTPError as exc:
            if "404" in str(exc):
                return None
            raise

    def delete(self, id: str) -> bool:
        """Delete a single record by ID."""
        result = self._request("/delete", data={"id": id})
        return result.get("deleted", False)

    def update_metadata(self, id: str, metadata: Dict[str, Any]) -> bool:
        """Update metadata via upsert (get + re-insert with merged metadata)."""
        existing = self.get(id)
        if not existing:
            return False
        merged_meta = dict(existing.get("metadata", {}))
        merged_meta.update(metadata)
        vector = existing.get("vector", [])
        if isinstance(vector, dict):
            # Float32Array comes back as {0: val, 1: val, ...}
            vector = [vector[str(i)] for i in range(len(vector))]
        self._request(
            "/upsert",
            data={"id": id, "vector": vector, "metadata": merged_meta},
        )
        return True

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
        """Nearest-neighbour search."""
        payload: Dict[str, Any] = {
            "vector": vector,
            "top_k": top_k,
            "use_reinforcement": use_reinforcement,
        }
        if filter_dict:
            payload["filter"] = filter_dict
        if min_score is not None:
            payload["min_score"] = min_score

        result = self._request("/search", data=payload)
        return result.get("results", [])

    # -------------------------------------------------------------------------
    # Aggregation
    # -------------------------------------------------------------------------

    def count(self) -> int:
        """Return the total number of records."""
        result = self._request("/count", method="GET")
        return int(result.get("count", 0))

    def stats(self) -> Dict[str, Any]:
        """Return storage statistics."""
        return self._request("/stats", method="GET")

    # -------------------------------------------------------------------------
    # SONA reinforcement interface
    # -------------------------------------------------------------------------

    def update_reward(self, id: str, reward: float) -> None:
        """Submit a reward signal for a single record."""
        self._request("/sona/reward", data={"id": id, "reward": reward})

    def update_reward_batch(self, rewards: List[Tuple[str, float]]) -> None:
        """Submit reward signals for multiple records."""
        payload = {"rewards": [{"id": id, "reward": r} for id, r in rewards]}
        self._request("/sona/reward-batch", data=payload)

    def get_profile(self, id: str) -> Dict[str, Any]:
        """Retrieve the SONA behavior profile for a record."""
        return self._request("/sona/profile", data={"id": id})

    def apply_decay(self) -> Dict[str, Any]:
        """Run time-decay across all records."""
        return self._request(
            "/sona/decay",
            data={
                "decay_rate": self.config.sona_decay_rate,
                "protected_decay_rate": self.config.sona_protected_decay_rate,
                "min_score": self.config.sona_min_score,
            },
        )

    def set_protected(self, id: str, protected: bool = True) -> None:
        """Mark or unmark a record as protected."""
        self._request("/sona/protect", data={"id": id, "protected": protected})
