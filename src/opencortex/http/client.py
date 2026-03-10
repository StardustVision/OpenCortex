# SPDX-License-Identifier: Apache-2.0
"""
Async HTTP client for the OpenCortex HTTP Server.

Provides :class:`OpenCortexClient` — a thin wrapper around ``httpx.AsyncClient``
that mirrors the current REST API exposed by ``opencortex.http.server``.

Usage::

    client = OpenCortexClient(base_url="http://127.0.0.1:8921")
    await client.connect()
    result = await client.memory_store(abstract="User prefers dark theme")
    await client.close()
"""

import logging
from typing import Any, Dict, List, Optional

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
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._tenant_id = tenant_id
        self._user_id = user_id
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

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET with retry logic."""
        return await self._request("GET", path, params=params)

    def _build_headers(self) -> Dict[str, str]:
        """Build per-request HTTP headers for identity."""
        hdrs: Dict[str, str] = {}
        if self._tenant_id:
            hdrs["X-Tenant-ID"] = self._tenant_id
        if self._user_id:
            hdrs["X-User-ID"] = self._user_id
        return hdrs

    async def _request(
        self,
        method: str,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        if not self._client:
            raise OpenCortexClientError("Client not connected — call connect() first")

        headers = self._build_headers()
        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await self._client.request(
                    method, path, json=json, params=params, headers=headers
                )
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

    async def memory_batch_store(
        self,
        items: List[Dict[str, Any]],
        source_path: str = "",
        scan_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"items": items, "source_path": source_path}
        if scan_meta is not None:
            payload["scan_meta"] = scan_meta
        return await self._post("/api/v1/memory/batch_store", payload)

    async def memory_promote_to_shared(
        self,
        uris: List[str],
        project_id: str,
    ) -> Dict[str, Any]:
        return await self._post("/api/v1/memory/promote_to_shared", {
            "uris": uris, "project_id": project_id,
        })

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

    async def memory_list(
        self,
        category: Optional[str] = None,
        context_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if category is not None:
            params["category"] = category
        if context_type is not None:
            params["context_type"] = context_type
        return await self._get("/api/v1/memory/list", params=params)

    async def memory_stats(self) -> Dict[str, Any]:
        return await self._get("/api/v1/memory/stats")

    async def memory_decay(self) -> Dict[str, Any]:
        return await self._post("/api/v1/memory/decay", {})

    async def memory_health(self) -> Dict[str, Any]:
        return await self._get("/api/v1/memory/health")

    # =====================================================================
    # Intent / Session
    # =====================================================================

    async def intent_should_recall(self, query: str) -> Dict[str, Any]:
        return await self._post("/api/v1/intent/should_recall", {"query": query})

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

    async def system_status(self, status_type: str = "doctor") -> Dict[str, Any]:
        return await self._get("/api/v1/system/status", params={"type": status_type})
