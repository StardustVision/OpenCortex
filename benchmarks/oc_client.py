"""
OpenCortex evaluation HTTP client.

Extracted from benchmarks/locomo_eval.py with extended parameters:
- store() gains meta and context_type parameters
- search() gains context_type parameter

All retry + error handling logic preserved from the original.
"""

import asyncio
from typing import Any, Dict, List, Optional

import httpx


def _is_retryable_http_error(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return False


class OCClient:
    def __init__(
        self,
        base: str,
        token: str,
        timeout: float = 120.0,
        retries: int = 8,
        retry_delay: float = 2.0,
        collection: str = "",
    ):
        self._base = base.rstrip("/")
        self._hdrs = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if collection:
            self._hdrs["X-Collection"] = collection
        self._collection = collection
        self._client = httpx.AsyncClient(timeout=timeout)
        self._retries = retries
        self._retry_delay = retry_delay

    async def create_collection(self, name: str) -> bool:
        try:
            await self._post("/api/v1/admin/collection", {"name": name})
            return True
        except Exception:
            return False

    async def delete_collection(self, name: str) -> None:
        try:
            url = f"{self._base}/api/v1/admin/collection/{name}"
            r = await self._client.delete(url, headers=self._hdrs)
            r.raise_for_status()
        except Exception:
            pass

    async def close(self):
        await self._client.aclose()

    async def store(
        self,
        abstract: str,
        content: str = "",
        category: str = "",
        context_type: str = "memory",
        meta: Optional[Dict[str, Any]] = None,
        dedup: bool = False,
        embed_text: str = "",
    ) -> Dict:
        """Store a memory/document. Supports meta for ingest_mode override."""
        payload: Dict[str, Any] = {
            "abstract": abstract,
            "content": content,
            "category": category,
            "context_type": context_type,
            "dedup": dedup,
        }
        if meta:
            payload["meta"] = meta
        if embed_text:
            payload["embed_text"] = embed_text
        return await self._post("/api/v1/memory/store", payload)

    async def search(
        self,
        query: str,
        limit: int = 10,
        category: str = "",
        detail_level: str = "l2",
        context_type: Optional[str] = None,
    ) -> List[Dict]:
        """Search memories. context_type filters results (e.g. 'resource' for documents)."""
        payload: Dict[str, Any] = {
            "query": query,
            "limit": limit,
            "detail_level": detail_level,
        }
        if category:
            payload["category"] = category
        if context_type:
            payload["context_type"] = context_type
        result = await self._post("/api/v1/memory/search", payload)
        return result.get("results", [])

    async def forget(self, uri: str) -> Dict:
        """Delete a memory by URI."""
        return await self._post("/api/v1/memory/forget", {"uri": uri})

    async def context_recall(
        self,
        session_id: str,
        query: str,
        turn_id: str = "t0",
        limit: int = 10,
        detail_level: Optional[str] = None,
    ) -> Dict:
        """MCP recall: context phase=prepare with messages containing the query.

        When detail_level is None, the server's IntentRouter decides the
        appropriate level (L0/L1/L2) based on query analysis.
        """
        config: Dict[str, Any] = {"max_items": limit}
        if detail_level is not None:
            config["detail_level"] = detail_level
        return await self._post(
            "/api/v1/context",
            {
                "session_id": session_id,
                "phase": "prepare",
                "turn_id": turn_id,
                "messages": [{"role": "user", "content": query}],
                "config": config,
            },
        )

    async def context_commit(
        self,
        session_id: str,
        turn_id: str,
        messages: List[Dict[str, str]],
    ) -> Dict:
        """MCP commit: write messages via conversation mode (immediate + merge)."""
        return await self._post(
            "/api/v1/context",
            {
                "session_id": session_id,
                "phase": "commit",
                "turn_id": turn_id,
                "messages": [
                    {"role": m["role"], "content": m["content"]} for m in messages
                ],
            },
        )

    async def context_end(self, session_id: str) -> Dict:
        """MCP end: flush session → Alpha pipeline."""
        return await self._post(
            "/api/v1/context",
            {
                "session_id": session_id,
                "phase": "end",
            },
        )

    async def _post(self, path: str, payload: Dict) -> Dict:
        """POST with retry logic (retryable on 429/5xx and transport errors)."""
        url = f"{self._base}{path}"
        last_error: Optional[Exception] = None
        for attempt in range(1, self._retries + 1):
            try:
                r = await self._client.post(url, json=payload, headers=self._hdrs)
                r.raise_for_status()
                return r.json()
            except (
                httpx.TimeoutException,
                httpx.HTTPStatusError,
                httpx.TransportError,
            ) as exc:
                last_error = exc
                if attempt >= self._retries or not _is_retryable_http_error(exc):
                    raise
                await asyncio.sleep(min(2**attempt, 120))
        if last_error:
            raise last_error
        return {}
