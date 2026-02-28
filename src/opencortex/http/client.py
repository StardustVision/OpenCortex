# SPDX-License-Identifier: Apache-2.0
"""
Async HTTP client for the OpenCortex HTTP Server.

Provides :class:`OpenCortexClient` — a thin wrapper around ``httpx.AsyncClient``
that mirrors the 25 REST endpoints exposed by ``opencortex.http.server``.

Usage::

    client = OpenCortexClient(base_url="http://127.0.0.1:8921")
    await client.connect()
    result = await client.memory_store(abstract="User prefers dark theme")
    await client.close()
"""

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 2


class OpenCortexClientError(RuntimeError):
    """Raised when the OpenCortex HTTP Server returns an error."""


class OpenCortexClient:
    """Async HTTP client for the OpenCortex HTTP Server."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8921",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def connect(self) -> None:
        """Open the underlying HTTP connection pool."""
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
        )
        logger.info("[OpenCortexClient] Connected to %s", self._base_url)

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.info("[OpenCortexClient] Closed")

    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------

    async def _post(self, path: str, json: Dict[str, Any]) -> Any:
        """POST with retry logic."""
        return await self._request("POST", path, json=json)

    async def _get(self, path: str) -> Any:
        """GET with retry logic."""
        return await self._request("GET", path)

    async def _request(
        self, method: str, path: str, json: Optional[Dict[str, Any]] = None
    ) -> Any:
        if not self._client:
            raise OpenCortexClientError("Client not connected — call connect() first")

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await self._client.request(method, path, json=json)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                raise OpenCortexClientError(
                    f"HTTP {exc.response.status_code}: {exc.response.text}"
                ) from exc
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "[OpenCortexClient] Retry %d/%d for %s %s: %s",
                        attempt + 1,
                        _MAX_RETRIES,
                        method,
                        path,
                        exc,
                    )
                    continue
        raise OpenCortexClientError(
            f"Failed after {_MAX_RETRIES + 1} attempts: {last_exc}"
        ) from last_exc

    # =====================================================================
    # Core Memory
    # =====================================================================

    async def memory_store(
        self,
        abstract: str,
        content: str = "",
        overview: str = "",
        category: str = "",
        context_type: str = "memory",
        uri: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "abstract": abstract, "content": content,
            "category": category, "context_type": context_type,
        }
        if overview:
            payload["overview"] = overview
        if uri is not None:
            payload["uri"] = uri
        if meta is not None:
            payload["meta"] = meta
        return await self._post("/api/v1/memory/store", payload)

    async def memory_search(
        self,
        query: str,
        limit: int = 5,
        context_type: Optional[str] = None,
        category: Optional[str] = None,
        detail_level: str = "l1",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"query": query, "limit": limit, "detail_level": detail_level}
        if context_type is not None:
            payload["context_type"] = context_type
        if category is not None:
            payload["category"] = category
        return await self._post("/api/v1/memory/search", payload)

    async def memory_feedback(self, uri: str, reward: float) -> Dict[str, Any]:
        return await self._post("/api/v1/memory/feedback", {"uri": uri, "reward": reward})

    async def memory_stats(self) -> Dict[str, Any]:
        return await self._get("/api/v1/memory/stats")

    async def memory_decay(self) -> Dict[str, Any]:
        return await self._post("/api/v1/memory/decay", {})

    async def memory_health(self) -> Dict[str, Any]:
        return await self._get("/api/v1/memory/health")

    # =====================================================================
    # Hooks Learn
    # =====================================================================

    async def hooks_learn(
        self,
        state: str,
        action: str,
        reward: float,
        available_actions: str = "",
    ) -> Dict[str, Any]:
        return await self._post("/api/v1/hooks/learn", {
            "state": state, "action": action,
            "reward": reward, "available_actions": available_actions,
        })

    async def hooks_remember(self, content: str, memory_type: str = "general") -> Dict[str, Any]:
        return await self._post("/api/v1/hooks/remember", {
            "content": content, "memory_type": memory_type,
        })

    async def hooks_recall(self, query: str, limit: int = 5) -> Any:
        return await self._post("/api/v1/hooks/recall", {"query": query, "limit": limit})

    async def hooks_stats(self) -> Dict[str, Any]:
        return await self._get("/api/v1/hooks/stats")

    # =====================================================================
    # Trajectory
    # =====================================================================

    async def trajectory_begin(self, trajectory_id: str, initial_state: str) -> Dict[str, Any]:
        return await self._post("/api/v1/hooks/trajectory/begin", {
            "trajectory_id": trajectory_id, "initial_state": initial_state,
        })

    async def trajectory_step(
        self,
        trajectory_id: str,
        action: str,
        reward: float,
        next_state: str = "",
    ) -> Dict[str, Any]:
        return await self._post("/api/v1/hooks/trajectory/step", {
            "trajectory_id": trajectory_id, "action": action,
            "reward": reward, "next_state": next_state,
        })

    async def trajectory_end(self, trajectory_id: str, quality_score: float) -> Dict[str, Any]:
        return await self._post("/api/v1/hooks/trajectory/end", {
            "trajectory_id": trajectory_id, "quality_score": quality_score,
        })

    # =====================================================================
    # Error
    # =====================================================================

    async def error_record(self, error: str, fix: str, context: str = "") -> Dict[str, Any]:
        return await self._post("/api/v1/hooks/error/record", {
            "error": error, "fix": fix, "context": context,
        })

    async def error_suggest(self, error: str) -> Any:
        return await self._post("/api/v1/hooks/error/suggest", {"error": error})

    # =====================================================================
    # Session
    # =====================================================================

    async def session_begin(self, session_id: str) -> Dict[str, Any]:
        return await self._post("/api/v1/session/begin", {"session_id": session_id})

    async def session_message(self, session_id: str, role: str, content: str) -> Dict[str, Any]:
        return await self._post("/api/v1/session/message", {
            "session_id": session_id, "role": role, "content": content,
        })

    async def session_end(self, session_id: str, quality_score: float = 0.5) -> Dict[str, Any]:
        return await self._post("/api/v1/session/end", {
            "session_id": session_id, "quality_score": quality_score,
        })

    # =====================================================================
    # Integration
    # =====================================================================

    async def integration_route(self, task: str, agents: str = "") -> Dict[str, Any]:
        return await self._post("/api/v1/integration/route", {"task": task, "agents": agents})

    async def integration_init(self, project_path: str = ".") -> Dict[str, Any]:
        return await self._post("/api/v1/integration/init", {"project_path": project_path})

    async def integration_pretrain(self, repo_path: str = ".") -> Dict[str, Any]:
        return await self._post("/api/v1/integration/pretrain", {"repo_path": repo_path})

    async def integration_verify(self) -> Dict[str, Any]:
        return await self._get("/api/v1/integration/verify")

    async def integration_doctor(self) -> Dict[str, Any]:
        return await self._get("/api/v1/integration/doctor")

    async def integration_export(self, format: str = "json") -> Dict[str, Any]:
        return await self._post("/api/v1/integration/export", {"format": format})

    async def integration_build_agents(self) -> Dict[str, Any]:
        return await self._get("/api/v1/integration/build-agents")
