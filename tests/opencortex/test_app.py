# SPDX-License-Identifier: Apache-2.0
"""Tests for the opencortex FastAPI application."""

from __future__ import annotations

import json
import sys
import unittest
from hashlib import sha256
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from httpx import ASGITransport

from opencortex.app import create_app
from opencortex.auth.token import (
    ensure_secret,
    generate_token,
    load_token_records,
    save_token_record,
)
from opencortex.prompts.retrieval import (
    QUERY_DECOMPOSITION_SYSTEM_PROMPT,
    REASON_TREE_SELECTION_SYSTEM_PROMPT,
    RECALL_RERANK_SYSTEM_PROMPT,
)
from opencortex.prompts.write import LAYER_DERIVATION_SYSTEM_PROMPT
from opencortex.settings import Settings, get_settings
from opencortex.store.document_tree import DocumentParser
from opencortex.vector.retrieval.reranker import (
    HostedVLLMRerankClient,
    LiteLLMRerankClient,
    OpenAIRerankClient,
)

captured_system_prompts: list[str] = []
MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def app_settings(data_root: str) -> Settings:
    """Return settings with required LLM config for app lifespan tests."""
    return Settings(data_root=data_root, llm_api_key="test-key")


def auth_headers(
    data_root: str,
    *,
    role: str = "user",
    tenant_id: str = "default",
    user_id: str = "default",
) -> dict[str, str]:
    """Return authorization headers for app tests."""
    secret = ensure_secret(data_root)
    token = generate_token(
        tenant_id,
        user_id,
        secret,
        role=role,
    )
    save_token_record(
        data_root,
        token,
        tenant_id,
        user_id,
        role=role,
    )
    return {"authorization": f"Bearer {token}"}


def fast_merge_settings(data_root: str) -> Settings:
    """Return settings that make one session turn trigger merge."""
    settings = app_settings(data_root)
    settings.conversation_merge_token_budget = 1
    return settings


def derived_json(content: str) -> str:
    """Return fake LLM derivation JSON."""
    return json.dumps(
        {
            "abstract": f"derived abstract: {content}",
            "overview": f"derived overview: {content}",
            "keywords": ["python"],
            "entities": ["Alice"],
            "anchor_handles": ["Alice"],
            "fact_points": ["Alice uses Python"],
        }
    )


async def fake_llm_completion(
    prompt: str,
    *,
    temperature: float = 0.0,
    system_prompt: str | None = None,
) -> str:
    """Return fake LLM JSON for layer derivation and query planning."""
    _ = temperature
    _ = system_prompt
    if "Split this user query into short retrieval queries" in prompt:
        return (
            '{"retrieval_queries":['
            '"写入链路设计",'
            '"召回链路设计",'
            '"Qdrant 向量写入检索"'
            "]}"
        )
    if "Select the best reason-tree entry URIs" in prompt:
        return '{"selected_uris":["' + extract_first_reason_tree_uri(prompt) + '"]}'
    if "Score these candidate memories" in prompt:
        return json.dumps(
            {
                "scores": [
                    {"uri": uri, "score": 1.0 - index * 0.05}
                    for index, uri in enumerate(extract_candidate_uris(prompt))
                ]
            }
        )
    if "Build a Reason Tree" in prompt:
        return json.dumps(
            {
                "abstract": "Reason tree for stored content.",
                "overview": "Reason tree overview for stored content.",
                "nodes": [
                    {
                        "title": "Stored Content",
                        "summary": extract_prompt_content(prompt),
                        "fact_points": ["Stored content has retrievable facts."],
                        "source_refs": ["content"],
                        "children": [],
                    }
                ],
            }
        )
    return derived_json(extract_prompt_content(prompt))


async def capture_system_prompt_llm_completion(
    prompt: str,
    *,
    temperature: float = 0.0,
    system_prompt: str | None = None,
) -> str:
    """Capture prompt-specific system messages while returning fake JSON."""
    captured_system_prompts.append(system_prompt or "")
    return await fake_llm_completion(prompt, temperature=temperature)


async def oversized_query_llm_completion(
    prompt: str,
    *,
    temperature: float = 0.0,
    system_prompt: str | None = None,
) -> str:
    """Return an oversized retrieval query for error-path tests."""
    _ = system_prompt
    if "Split this user query into short retrieval queries" in prompt:
        return '{"retrieval_queries":["' + ("x" * 120) + '"]}'
    return await fake_llm_completion(prompt, temperature=temperature)


async def invalid_reason_tree_llm_completion(
    prompt: str,
    *,
    temperature: float = 0.0,
    system_prompt: str | None = None,
) -> str:
    """Return an invalid reason-tree URI for error-path tests."""
    _ = system_prompt
    if "Select the best reason-tree entry URIs" in prompt:
        return '{"selected_uris":["opencortex://invalid/reason-tree"]}'
    return await fake_llm_completion(prompt, temperature=temperature)


async def reason_tree_node_llm_completion(
    prompt: str,
    *,
    temperature: float = 0.0,
    system_prompt: str | None = None,
) -> str:
    """Prefer a reason-tree node URI during selection tests."""
    _ = system_prompt
    if "Select the best reason-tree entry URIs" in prompt:
        return json.dumps({"selected_uris": [extract_reason_tree_node_uri(prompt)]})
    return await fake_llm_completion(prompt, temperature=temperature)


def extract_prompt_content(prompt: str) -> str:
    """Return the content block from a derivation prompt."""
    start = prompt.find("<content>")
    end = prompt.rfind("</content>")
    if start >= 0 and end > start:
        return prompt[start + len("<content>") : end].strip()
    return prompt


def extract_first_reason_tree_uri(prompt: str) -> str:
    """Return the first candidate URI from a reason-tree prompt."""
    marker = " uri="
    start = prompt.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = prompt.find(" ", start)
    return prompt[start:] if end < 0 else prompt[start:end]


def extract_reason_tree_node_uri(prompt: str) -> str:
    """Return the first reason-tree node candidate URI."""
    for line in prompt.splitlines():
        marker = " uri="
        if marker not in line:
            continue
        start = line.find(marker) + len(marker)
        end = line.find(" ", start)
        uri = line[start:] if end < 0 else line[start:end]
        if "/reason_tree/" in uri:
            return uri
    return extract_first_reason_tree_uri(prompt)


def extract_candidate_uris(prompt: str) -> list[str]:
    """Return candidate URIs from a rerank prompt."""
    values: list[str] = []
    for line in prompt.splitlines():
        text = line.strip()
        if ". uri=" not in text:
            continue
        uri = text.split("uri=", maxsplit=1)[1].strip()
        if uri:
            values.append(uri)
    return values


def fake_embedding(_text: str) -> object:
    """Return one deterministic embedding."""
    return type(
        "Embedding",
        (),
        {"dense_vector": [0.1] * 1024, "sparse_vector": None},
    )()


def keyword_embedding(text: str) -> object:
    """Return a deterministic embedding keyed by distinctive test terms."""
    vector = [0.0] * 1024
    lowered = text.lower()
    if "zephyr" in lowered:
        vector[0] = 1.0
    if "atlas" in lowered:
        vector[1] = 1.0
    if "orion" in lowered:
        vector[2] = 1.0
    if not any(vector):
        vector[3] = 1.0
    return type(
        "Embedding",
        (),
        {"dense_vector": vector, "sparse_vector": None},
    )()


def fake_embedding_batch(texts: list[str]) -> list[object]:
    """Return one deterministic embedding per text."""
    return [fake_embedding(text) for text in texts]


def keyword_embedding_batch(texts: list[str]) -> list[object]:
    """Return one keyword embedding per text."""
    return [keyword_embedding(text) for text in texts]


class TestOpenCortexApp(unittest.IsolatedAsyncioTestCase):
    """Verify the standalone opencortex FastAPI surface."""

    async def test_create_app_exposes_write_and_search_routes(self) -> None:
        """The app exposes write endpoints and memory search."""
        app = create_app()

        paths = {route.path for route in app.routes}
        self.assertIn("/api/v1/memory/store", paths)
        self.assertIn("/api/v1/memory/search", paths)
        self.assertIn("/api/v1/memory/forget", paths)
        self.assertIn("/api/v1/auth/me", paths)
        self.assertIn("/admin/v1/tokens", paths)
        self.assertIn("/console/v1/memories", paths)
        self.assertIn("/console/v1/stats", paths)
        self.assertIn("/api/v1/session/message", paths)
        self.assertIn("/api/v1/session/end", paths)
        self.assertIn("/mcp", paths)

    async def test_mcp_get_returns_405(self) -> None:
        """Streamable HTTP GET is rejected until SSE sessions are supported."""
        app = create_app(settings=app_settings(":memory:"))
        headers = auth_headers(":memory:")
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=headers,
        ) as client:
            response = await client.get("/mcp")

        self.assertEqual(response.status_code, 405)

    async def test_mcp_rejects_missing_streamable_http_accept(self) -> None:
        """MCP POST requires the Streamable HTTP Accept header pair."""
        app = create_app(settings=app_settings(":memory:"))
        headers = auth_headers(":memory:")
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=headers,
        ) as client:
            response = await client.post(
                "/mcp",
                headers={
                    "content-type": "application/json",
                    "accept": "application/json",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )

        self.assertEqual(response.status_code, 406)

    async def test_api_and_mcp_require_bearer_token(self) -> None:
        """Protected API and MCP endpoints reject unauthenticated requests."""
        app = create_app(settings=app_settings(":memory:"))
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            api_response = await client.post(
                "/api/v1/memory/search",
                json={"query": "anything"},
            )
            mcp_response = await client.post(
                "/mcp",
                headers=MCP_HEADERS,
                json={"jsonrpc": "2.0", "id": "ping-1", "method": "ping"},
            )
            console_response = await client.get("/console/v1/stats")

        self.assertEqual(api_response.status_code, 401)
        self.assertEqual(mcp_response.status_code, 401)
        self.assertEqual(console_response.status_code, 401)

    async def test_auth_still_enforced_when_identity_context_disabled(self) -> None:
        """Disabling context injection does not disable protected-route auth."""
        app = create_app(
            settings=app_settings(":memory:").model_copy(
                update={"identity_context_enabled": False}
            )
        )
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.get("/console/v1/stats")

        self.assertEqual(response.status_code, 401)

    async def test_auth_me_reads_bearer_claims(self) -> None:
        """Bearer JWT claims populate request identity."""
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            secret = ensure_secret(data_root)
            token = generate_token(
                "tenant-a",
                "alice",
                secret,
                role="admin",
            )
            save_token_record(
                data_root,
                token,
                "tenant-a",
                "alice",
                role="admin",
            )
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                headers={"authorization": f"Bearer {token}"},
            ) as client:
                response = await client.get("/api/v1/auth/me")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "tenant_id": "tenant-a",
                "user_id": "alice",
                "project_id": "public",
                "role": "admin",
            },
        )

    async def test_admin_can_create_list_and_revoke_user_token(self) -> None:
        """Admin token API manages persisted user API keys."""
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            headers = auth_headers(data_root, role="admin")
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                headers=headers,
            ) as client:
                create_response = await client.post(
                    "/admin/v1/tokens",
                    json={
                        "tenant_id": "tenant-a",
                        "user_id": "alice",
                    },
                )
                list_response = await client.get("/admin/v1/tokens")
                created = create_response.json()
                token_prefix = sha256(created["token"].encode("utf-8")).hexdigest()[:16]
                revoke_response = await client.request(
                    "DELETE",
                    "/admin/v1/tokens",
                    json={"token_prefix": token_prefix},
                )
                after_revoke_response = await client.get("/admin/v1/tokens")

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(created["tenant_id"], "tenant-a")
        self.assertEqual(created["user_id"], "alice")
        self.assertEqual(created["role"], "user")
        self.assertIn("token", created)

        self.assertEqual(list_response.status_code, 200)
        records = list_response.json()["tokens"]
        user_records = [
            record for record in records if record["tenant_id"] == "tenant-a"
        ]
        self.assertEqual(len(user_records), 1)
        self.assertEqual(user_records[0]["token"], created["token"])
        self.assertEqual(user_records[0]["token_prefix"], token_prefix)

        self.assertEqual(revoke_response.status_code, 200)
        self.assertEqual(after_revoke_response.status_code, 200)
        self.assertFalse(
            any(
                record["tenant_id"] == "tenant-a"
                for record in after_revoke_response.json()["tokens"]
            )
        )

    async def test_non_admin_cannot_manage_tokens(self) -> None:
        """User tokens cannot access admin token management."""
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            headers = auth_headers(data_root)
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                headers=headers,
            ) as client:
                response = await client.get("/admin/v1/tokens")

        self.assertEqual(response.status_code, 403)

    async def test_configured_admin_token_is_registered(self) -> None:
        """A configured admin JWT bootstraps admin API access."""
        with TemporaryDirectory() as data_root:
            secret = ensure_secret(data_root)
            token = generate_token(
                "tenant-admin",
                "root",
                secret,
                role="admin",
            )
            app = create_app(
                settings=app_settings(data_root).model_copy(
                    update={"admin_api_token": token}
                )
            )
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                headers={"authorization": f"Bearer {token}"},
            ) as client:
                me_response = await client.get("/api/v1/auth/me")
                tokens_response = await client.get("/admin/v1/tokens")

        self.assertEqual(me_response.status_code, 200)
        self.assertEqual(me_response.json()["role"], "admin")
        self.assertEqual(me_response.json()["tenant_id"], "tenant-admin")
        self.assertEqual(tokens_response.status_code, 200)
        self.assertEqual(tokens_response.json()["tokens"][0]["role"], "admin")

    async def test_default_admin_token_is_created_once(self) -> None:
        """A default admin token is created only when no admin record exists."""
        with TemporaryDirectory() as data_root:
            create_app(settings=app_settings(data_root))
            records = load_token_records(data_root)
            admin_records = [
                record for record in records if record.get("role") == "admin"
            ]

            create_app(settings=app_settings(data_root))
            after_second_start = load_token_records(data_root)
            second_admin_records = [
                record for record in after_second_start if record.get("role") == "admin"
            ]

        self.assertEqual(len(admin_records), 1)
        self.assertEqual(admin_records[0]["tenant_id"], "_system")
        self.assertEqual(admin_records[0]["user_id"], "_admin")
        self.assertEqual(second_admin_records, admin_records)

    async def test_placeholder_admin_token_uses_default_bootstrap(self) -> None:
        """Placeholder admin token values are treated as unset."""
        with TemporaryDirectory() as data_root:
            create_app(
                settings=app_settings(data_root).model_copy(
                    update={"admin_api_token": "<admin-jwt>"}
                )
            )
            records = load_token_records(data_root)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tenant_id"], "_system")
        self.assertEqual(records[0]["user_id"], "_admin")
        self.assertEqual(records[0]["role"], "admin")

    async def test_default_admin_token_does_not_replace_existing_admin(self) -> None:
        """Existing admin records suppress default admin token bootstrap."""
        with TemporaryDirectory() as data_root:
            secret = ensure_secret(data_root)
            token = generate_token(
                "tenant-admin",
                "root",
                secret,
                role="admin",
            )
            save_token_record(
                data_root,
                token,
                "tenant-admin",
                "root",
                role="admin",
            )

            create_app(settings=app_settings(data_root))
            records = load_token_records(data_root)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tenant_id"], "tenant-admin")
        self.assertEqual(records[0]["user_id"], "root")

    async def test_mcp_initialize_and_tools_list(self) -> None:
        """MCP exposes initialize and tools/list over Streamable HTTP."""
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        init_response = await client.post(
                            "/mcp",
                            headers=MCP_HEADERS,
                            json={
                                "jsonrpc": "2.0",
                                "id": "init-1",
                                "method": "initialize",
                                "params": {
                                    "protocolVersion": "2025-06-18",
                                    "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "0"},
                                },
                            },
                        )
                        tools_response = await client.post(
                            "/mcp",
                            headers=MCP_HEADERS,
                            json={
                                "jsonrpc": "2.0",
                                "id": "tools-1",
                                "method": "tools/list",
                            },
                        )
                        prompts_response = await client.post(
                            "/mcp",
                            headers=MCP_HEADERS,
                            json={
                                "jsonrpc": "2.0",
                                "id": "prompts-1",
                                "method": "prompts/list",
                            },
                        )
                        prompt_get_response = await client.post(
                            "/mcp",
                            headers=MCP_HEADERS,
                            json={
                                "jsonrpc": "2.0",
                                "id": "prompt-get-1",
                                "method": "prompts/get",
                                "params": {
                                    "name": "opencortex-memory-rules",
                                    "arguments": {},
                                },
                            },
                        )

        self.assertEqual(init_response.status_code, 200)
        init_result = init_response.json()["result"]
        self.assertEqual(init_result["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init_result["capabilities"])
        self.assertIn("prompts", init_result["capabilities"])
        self.assertIn("opencortex.search", init_result["instructions"])

        self.assertEqual(tools_response.status_code, 200)
        tool_names = {tool["name"] for tool in tools_response.json()["result"]["tools"]}
        self.assertIn("opencortex.search", tool_names)
        self.assertIn("opencortex.store_memory", tool_names)
        self.assertIn("opencortex.store_resource", tool_names)
        search_tool = next(
            tool
            for tool in tools_response.json()["result"]["tools"]
            if tool["name"] == "opencortex.search"
        )
        self.assertIn("inputSchema", search_tool)

        self.assertEqual(prompts_response.status_code, 200)
        prompts = prompts_response.json()["result"]["prompts"]
        self.assertEqual(prompts[0]["name"], "opencortex-memory-rules")

        self.assertEqual(prompt_get_response.status_code, 200)
        prompt_text = prompt_get_response.json()["result"]["messages"][0]["content"][
            "text"
        ]
        self.assertIn("Before answering", prompt_text)
        self.assertIn("opencortex.session_message", prompt_text)

    async def test_mcp_store_and_search_tools_use_new_chain(self) -> None:
        """MCP tools/call writes and recalls through the current memory chain."""
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=keyword_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=keyword_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        store_response = await client.post(
                            "/mcp",
                            headers=MCP_HEADERS,
                            json={
                                "jsonrpc": "2.0",
                                "id": "store-1",
                                "method": "tools/call",
                                "params": {
                                    "name": "opencortex.store_memory",
                                    "arguments": {
                                        "content": (
                                            "Zephyr uses a blue notebook for planning."
                                        ),
                                        "category": "semantic",
                                        "metadata": {"entities": ["Zephyr"]},
                                        "source": {"kind": "manual"},
                                    },
                                },
                            },
                        )
                        await app.state.store_event_worker.wait_idle()
                        search_response = await client.post(
                            "/mcp",
                            headers=MCP_HEADERS,
                            json={
                                "jsonrpc": "2.0",
                                "id": "search-1",
                                "method": "tools/call",
                                "params": {
                                    "name": "opencortex.search",
                                    "arguments": {
                                        "query": "Zephyr notebook planning",
                                        "limit": 3,
                                    },
                                },
                            },
                        )

        self.assertEqual(store_response.status_code, 200)
        store_result = store_response.json()["result"]
        self.assertFalse(store_result["isError"])
        stored_uri = store_result["structuredContent"]["uri"]
        self.assertEqual(store_result["structuredContent"]["context_type"], "memory")

        self.assertEqual(search_response.status_code, 200)
        search_result = search_response.json()["result"]
        self.assertFalse(search_result["isError"])
        results = search_result["structuredContent"]["results"]
        self.assertTrue(any(result["uri"] == stored_uri for result in results))

    async def test_lifespan_initializes_store_dependencies(self) -> None:
        """Lifespan initializes state used by dependency injection."""
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            async with app.router.lifespan_context(app):
                self.assertIsNotNone(app.state.runtime)
                self.assertIsNotNone(app.state.vector_store)
                self.assertIsNotNone(app.state.session_buffer)
                self.assertIsNotNone(app.state.store_event_worker)
            self.assertIsNone(app.state.runtime)
            self.assertIsNone(app.state.session_buffer)

    async def test_memory_store_uses_new_request_schema(self) -> None:
        """POST /memory/store accepts the new store contract."""
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        response = await client.post(
                            "/api/v1/memory/store",
                            json={
                                "type": "memory",
                                "content": "User prefers dark theme.",
                                "category": "semantic",
                                "metadata": {},
                                "source": {"kind": "manual"},
                            },
                        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["context_type"], "memory")
        self.assertEqual(data["category"], "semantic")

    async def test_memory_store_enqueues_check_update_event(self) -> None:
        """Every primary write also creates a check-update event."""
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    seen_events: list[str] = []

                    def capture_event(event: object) -> None:
                        seen_events.append(str(event.name))
                        app.state.store_event_queue.enqueue(
                            "store_events",
                            app.state.store_event_worker.event_payload(event),
                            max_attempts=app.state.store_event_worker.max_attempts,
                        )

                    app.state.runtime.memory_events.subscribers = {
                        name: [capture_event]
                        for name in app.state.runtime.memory_events.subscribers
                    }
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        response = await client.post(
                            "/api/v1/memory/store",
                            json={
                                "type": "memory",
                                "content": "Check update event should be queued.",
                                "category": "semantic",
                                "metadata": {},
                                "source": {"kind": "manual"},
                            },
                        )
                    await app.state.store_event_worker.wait_idle()
                    status = app.state.store_event_queue.status("store_events")

        self.assertEqual(response.status_code, 200)
        self.assertIn("memory_stored", seen_events)
        self.assertIn("check_update", seen_events)
        self.assertEqual(status.failed, 0)

    async def test_memory_forget_deletes_top_semantic_match(self) -> None:
        """Semantic forget recalls top1 and removes FS plus vector projections."""
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=keyword_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=keyword_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        store_response = await client.post(
                            "/api/v1/memory/store",
                            json={
                                "type": "memory",
                                "content": "Zephyr uses a blue notebook for planning.",
                                "category": "semantic",
                                "metadata": {"entities": ["Zephyr"]},
                                "source": {"kind": "manual"},
                            },
                        )
                        await app.state.store_event_worker.wait_idle()
                        target_uri = store_response.json()["uri"]
                        before_forget = await client.post(
                            "/api/v1/memory/search",
                            json={
                                "query": "Zephyr notebook planning",
                                "limit": 3,
                            },
                        )
                        forget_response = await client.post(
                            "/api/v1/memory/forget",
                            json={"query": "Zephyr notebook planning"},
                        )
                        after_forget = await client.post(
                            "/api/v1/memory/search",
                            json={
                                "query": "Zephyr notebook planning",
                                "limit": 3,
                            },
                        )
                    records = await app.state.vector_store.filter("context", None)
                    fs_missing = False
                    try:
                        await app.state.runtime.cortex_storage.abstract(target_uri)
                    except FileNotFoundError:
                        fs_missing = True

        self.assertEqual(store_response.status_code, 200)
        self.assertEqual(before_forget.status_code, 200)
        self.assertEqual(forget_response.status_code, 200)
        self.assertTrue(
            any(
                result["uri"] == target_uri
                for result in before_forget.json()["data"]["results"]
            )
        )
        forget_data = forget_response.json()["data"]
        self.assertEqual(forget_data["uri"], target_uri)
        self.assertEqual(forget_data["matched_by"], "query")
        self.assertEqual(forget_data["forgotten"], 1)
        self.assertTrue(forget_data["qdrant_removed"])
        self.assertTrue(forget_data["fs_removed"])
        self.assertFalse(
            any(
                record.get("uri") == target_uri
                or str(record.get("uri", "")).startswith(f"{target_uri}/")
                or record.get("source_uri") == target_uri
                for record in records
            )
        )
        self.assertEqual(after_forget.json()["data"]["total"], 0)
        self.assertTrue(fs_missing)

    async def test_console_memory_management_uses_separate_routes(self) -> None:
        """Console routes list, hydrate, search, and delete memories."""
        content = "Zephyr uses a blue notebook for planning."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=keyword_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=keyword_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        store_response = await client.post(
                            "/api/v1/memory/store",
                            json={
                                "type": "memory",
                                "content": content,
                                "category": "semantic",
                                "metadata": {"entities": ["Zephyr"]},
                                "source": {"kind": "manual"},
                            },
                        )
                        await app.state.store_event_worker.wait_idle()
                        self.assertEqual(store_response.status_code, 200)
                        uri = store_response.json()["uri"]

                        stats_response = await client.get("/console/v1/stats")
                        list_response = await client.get("/console/v1/memories")
                        content_response = await client.get(
                            "/console/v1/memories/content",
                            params={"uri": uri},
                        )
                        search_response = await client.post(
                            "/console/v1/memories/search",
                            json={
                                "query": "Zephyr notebook planning",
                                "limit": 3,
                            },
                        )
                        delete_response = await client.request(
                            "DELETE",
                            "/console/v1/memories",
                            json={"uri": uri},
                        )

        self.assertEqual(store_response.status_code, 200)
        self.assertEqual(stats_response.status_code, 200)
        self.assertGreaterEqual(stats_response.json()["primary_records"], 1)
        self.assertEqual(list_response.status_code, 200)
        self.assertTrue(
            any(item["uri"] == uri for item in list_response.json()["results"])
        )
        self.assertEqual(content_response.status_code, 200)
        self.assertEqual(content_response.json()["content"], content)
        self.assertEqual(search_response.status_code, 200)
        self.assertTrue(
            any(item["uri"] == uri for item in search_response.json()["results"])
        )
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_response.json()["uri"], uri)

    async def test_console_memory_management_enforces_visibility(self) -> None:
        """Console content and URI delete reject another user's memory URI."""
        content = "Private Orion runbook."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            owner_headers = auth_headers(
                data_root,
                tenant_id="tenant-a",
                user_id="owner",
            )
            other_headers = auth_headers(
                data_root,
                tenant_id="tenant-a",
                user_id="other",
            )
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=keyword_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=keyword_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                    ) as client:
                        store_response = await client.post(
                            "/api/v1/memory/store",
                            headers=owner_headers,
                            json={
                                "type": "memory",
                                "content": content,
                                "category": "semantic",
                                "metadata": {},
                                "source": {"kind": "manual"},
                            },
                        )
                        await app.state.store_event_worker.wait_idle()
                        self.assertEqual(store_response.status_code, 200)
                        uri = store_response.json()["uri"]

                        content_response = await client.get(
                            "/console/v1/memories/content",
                            headers=other_headers,
                            params={"uri": uri},
                        )
                        delete_response = await client.request(
                            "DELETE",
                            "/console/v1/memories",
                            headers=other_headers,
                            json={"uri": uri},
                        )
                        owner_content_response = await client.get(
                            "/console/v1/memories/content",
                            headers=owner_headers,
                            params={"uri": uri},
                        )

        self.assertEqual(content_response.status_code, 404)
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_response.json()["forgotten"], 0)
        self.assertEqual(owner_content_response.status_code, 200)
        self.assertEqual(owner_content_response.json()["content"], content)

    async def test_memory_store_uses_llm_derived_layers(self) -> None:
        """Store writes use LLM-derived abstract and overview layers."""
        content = "0123456789" * 150
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        response = await client.post(
                            "/api/v1/memory/store",
                            json={
                                "type": "memory",
                                "content": content,
                                "category": "semantic",
                                "metadata": {},
                                "source": {"kind": "manual"},
                            },
                        )
                    await app.state.store_event_worker.wait_idle()
                    uri = response.json()["uri"]
                    record = next(
                        item
                        for item in await app.state.vector_store.filter("context", None)
                        if item.get("uri") == uri
                    )
                    abstract = await app.state.runtime.cortex_storage.abstract(uri)
                    overview = await app.state.runtime.cortex_storage.overview(uri)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(record["abstract"], f"derived abstract: {content}")
        self.assertEqual(record["overview"], f"derived overview: {content}")
        self.assertEqual(abstract, f"derived abstract: {content}")
        self.assertEqual(overview, f"derived overview: {content}")

    async def test_resource_store_writes_document_parser_tree(self) -> None:
        """Structured resource content writes parsed child records."""
        content = "# Atlas Setup\n\nVector recall setup.\n\n## Qdrant\n\nUse Qdrant."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        response = await client.post(
                            "/api/v1/memory/store",
                            json={
                                "type": "resource",
                                "content": content,
                                "category": "semantic",
                                "metadata": {},
                                "source": {
                                    "kind": "document",
                                    "path": "/docs/atlas.md",
                                    "title": "Atlas",
                                },
                            },
                        )
                    await app.state.store_event_worker.wait_idle()
                    root_uri = response.json()["uri"]
                    records = await app.state.vector_store.filter("context", None)
                    root = next(
                        record for record in records if record["uri"] == root_uri
                    )
                    children = [
                        record
                        for record in records
                        if record.get("meta", {}).get("tree_root_uri") == root_uri
                        and record.get("uri") != root_uri
                        and record.get("retrieval_surface") == "l0_object"
                    ]
                    reason_tree_nodes = [
                        record
                        for record in records
                        if record.get("source_uri") == root_uri
                        and record.get("retrieval_surface") == "reason_tree_index"
                        and record.get("meta", {}).get("index_name")
                        == "ReasonTreeBuild"
                    ]
                    child_content = await app.state.runtime.cortex_storage.read_file(
                        f"{children[0]['uri']}/content.md",
                    )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(root["is_leaf"])
        self.assertEqual(root["meta"]["chunk_role"], "document_root")
        self.assertGreaterEqual(len(children), 1)
        self.assertEqual(children[0]["parent_uri"], root_uri)
        self.assertEqual(children[0]["meta"]["source_section_path"], "Atlas Setup")
        self.assertEqual(children[0]["source_doc_title"], "Atlas")
        self.assertIn("Atlas Setup", child_content)
        self.assertEqual(len(reason_tree_nodes), 1)
        self.assertTrue(reason_tree_nodes[0]["uri"].startswith(f"{root_uri}/"))
        self.assertEqual(reason_tree_nodes[0]["reason_role"], "resource_tree_node")

    async def test_document_parser_dispatches_resource_source_formats(self) -> None:
        """Resource parser accepts explicit document formats, MIME types, and paths."""
        parser = DocumentParser()

        pdf_chunks = await parser.parse(
            content="## Page 1\n\nAtlas PDF content.",
            source_path="",
            source_format="application/pdf",
        )
        docx_chunks = await parser.parse(
            content="# Plan\n\nAtlas Word content.",
            source_path="/docs/plan.docx",
            source_format="",
        )
        xlsx_chunks = await parser.parse(
            content="## Sheet1\n\n| Name | Value |\n| --- | --- |\n| Atlas | 1 |",
            source_path="",
            source_format="xlsx",
        )

        self.assertEqual(pdf_chunks[0].source_format, "pdf")
        self.assertEqual(docx_chunks[0].source_format, "docx")
        self.assertEqual(xlsx_chunks[0].source_format, "xlsx")

    async def test_session_message_runs_full_worker_write_chain(self) -> None:
        """Session writes produce primary, FS layers, cleanup, and retrieval indexes."""
        content = "assistant: Alice uses Python in Hangzhou."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        response = await client.post(
                            "/api/v1/session/message",
                            json={
                                "session_id": "session-e2e",
                                "turn_id": "turn-1",
                                "messages": [
                                    {
                                        "role": "assistant",
                                        "content": content,
                                        "meta": {
                                            "entities": ["Alice"],
                                            "topics": ["Python"],
                                        },
                                    }
                                ],
                                "tool_calls": [{"name": "Search"}],
                            },
                        )
                    await app.state.store_event_worker.wait_idle()
                    records = await app.state.vector_store.filter("context", None)
                    status = app.state.store_event_queue.status("store_events")

                    immediate_records = [
                        record
                        for record in records
                        if record.get("session_id") == "session-e2e"
                        and record.get("retrieval_surface") == "l0_object"
                        and record.get("meta", {}).get("layer") == "immediate"
                    ]
                    merged_records = [
                        record
                        for record in records
                        if record.get("session_id") == "session-e2e"
                        and record.get("meta", {}).get("layer") == "merged"
                        and record.get("retrieval_surface") == "l0_object"
                    ]
                    retrieval_surfaces = {
                        record.get("retrieval_surface")
                        for record in records
                        if record.get("session_id") == "session-e2e"
                    }
                    merged_uri = merged_records[0]["uri"]
                    abstract = await app.state.runtime.cortex_storage.abstract(
                        merged_uri,
                    )
                    overview = await app.state.runtime.cortex_storage.overview(
                        merged_uri,
                    )
                    content_file = await app.state.runtime.cortex_storage.read_file(
                        f"{merged_uri}/content.md",
                    )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["merge_requested"])
        self.assertEqual(status.pending, 0)
        self.assertEqual(status.processing, 0)
        self.assertEqual(status.failed, 0)
        self.assertEqual(immediate_records, [])
        self.assertEqual(len(merged_records), 1)
        self.assertEqual(merged_records[0]["abstract"], f"derived abstract: {content}")
        self.assertEqual(merged_records[0]["overview"], f"derived overview: {content}")
        self.assertIn("anchor_index", retrieval_surfaces)
        self.assertIn("fact_index", retrieval_surfaces)
        self.assertIn("entity_index", retrieval_surfaces)
        self.assertIn("reason_tree_index", retrieval_surfaces)
        self.assertEqual(abstract, f"derived abstract: {content}")
        self.assertEqual(overview, f"derived overview: {content}")
        self.assertEqual(content_file, content)

    async def test_session_end_writes_structured_final_tree(self) -> None:
        """Structured session final content is parsed into child records."""
        content = "# Decision\n\nUse CFS.\n\n## Follow Up\n\nWire parser."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        await client.post(
                            "/api/v1/session/message",
                            json={
                                "session_id": "session-final-tree",
                                "turn_id": "turn-1",
                                "messages": [
                                    {
                                        "role": "assistant",
                                        "content": content,
                                        "meta": {"entities": ["CFS"]},
                                    }
                                ],
                            },
                        )
                        await app.state.store_event_worker.wait_idle()
                        response = await client.post(
                            "/api/v1/session/end",
                            json={"session_id": "session-final-tree"},
                        )
                        await app.state.store_event_worker.wait_idle()
                    final_uri = response.json()["data"]["final_uri"]
                    records = await app.state.vector_store.filter("context", None)
                    children = [
                        record
                        for record in records
                        if record.get("meta", {}).get("tree_root_uri") == final_uri
                        and record.get("uri") != final_uri
                        and record.get("retrieval_surface") == "l0_object"
                    ]

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(children), 1)
        self.assertEqual(children[0]["parent_uri"], final_uri)
        self.assertEqual(children[0]["meta"]["chunk_role"], "session_final_section")
        self.assertEqual(children[0]["meta"]["source_section_path"], "Decision")

    async def test_session_end_final_uris_only_include_merged_primary_records(
        self,
    ) -> None:
        """Session final aggregation ignores secondary index projections."""
        content = "assistant: Alice uses Python in Hangzhou."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        await client.post(
                            "/api/v1/session/message",
                            json={
                                "session_id": "session-final-primary-only",
                                "turn_id": "turn-1",
                                "messages": [
                                    {
                                        "role": "assistant",
                                        "content": content,
                                        "meta": {"entities": ["Alice"]},
                                    }
                                ],
                            },
                        )
                        await app.state.store_event_worker.wait_idle()
                        response = await client.post(
                            "/api/v1/session/end",
                            json={"session_id": "session-final-primary-only"},
                        )
                        await app.state.store_event_worker.wait_idle()

        merged_uris = response.json()["data"]["merged_uris"]
        self.assertEqual(response.status_code, 200)
        self.assertTrue(merged_uris)
        self.assertTrue(all("/merged/" in uri for uri in merged_uris))
        self.assertFalse(any("_indexes/" in uri for uri in merged_uris))

    async def test_memory_search_recalls_written_indexes(self) -> None:
        """Search recalls primary records through secondary retrieval surfaces."""
        content = "assistant: Alice uses Python in Hangzhou."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        await client.post(
                            "/api/v1/session/message",
                            json={
                                "session_id": "session-recall",
                                "turn_id": "turn-1",
                                "messages": [
                                    {
                                        "role": "assistant",
                                        "content": content,
                                        "meta": {"entities": ["Alice"]},
                                    }
                                ],
                            },
                        )
                        await app.state.store_event_worker.wait_idle()
                        response = await client.post(
                            "/api/v1/memory/search",
                            json={
                                "query": "Alice Python",
                                "limit": 3,
                            },
                        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertNotIn("plan", data)
        result = data["results"][0]
        self.assertEqual(result["session_id"], "session-recall")
        self.assertEqual(result["type"], "memory")
        self.assertEqual(result["content"], content)
        self.assertEqual(result["source"]["session_id"], "session-recall")
        self.assertNotIn("retrieval_surfaces", result)
        self.assertNotIn("match_reason", result)
        self.assertTrue(
            {
                "memory",
                "topic",
                "fact",
                "entity",
                "summary",
            }.intersection({item["kind"] for item in result["evidence"]})
        )

    async def test_write_to_recall_hit_rate_for_memory_resource_session(
        self,
    ) -> None:
        """Memory, resource, and session writes are each recalled by target query."""
        targets: dict[str, str] = {}
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=keyword_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=keyword_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        memory_response = await client.post(
                            "/api/v1/memory/store",
                            json={
                                "type": "memory",
                                "content": "Zephyr uses a blue notebook for planning.",
                                "category": "semantic",
                                "metadata": {"entities": ["Zephyr"]},
                                "source": {"kind": "manual"},
                            },
                        )
                        resource_response = await client.post(
                            "/api/v1/memory/store",
                            json={
                                "type": "resource",
                                "content": "Atlas guide documents vector recall setup.",
                                "category": "semantic",
                                "metadata": {
                                    "entities": ["Atlas"],
                                    "source_path": "/docs/atlas-guide.md",
                                },
                                "source": {
                                    "kind": "document",
                                    "path": "/docs/atlas-guide.md",
                                    "title": "Atlas Guide",
                                },
                            },
                        )
                        await client.post(
                            "/api/v1/session/message",
                            json={
                                "session_id": "session-orion-recall",
                                "turn_id": "turn-1",
                                "messages": [
                                    {
                                        "role": "assistant",
                                        "content": (
                                            "Orion deployment requires canary checks."
                                        ),
                                        "meta": {"entities": ["Orion"]},
                                    }
                                ],
                            },
                        )
                        await app.state.store_event_worker.wait_idle()
                        records = await app.state.vector_store.filter("context", None)
                        targets["memory"] = memory_response.json()["uri"]
                        targets["resource"] = resource_response.json()["uri"]
                        targets["session"] = next(
                            record["uri"]
                            for record in records
                            if record.get("session_id") == "session-orion-recall"
                            and record.get("retrieval_surface") == "l0_object"
                        )

                        queries = {
                            "memory": "Zephyr notebook planning",
                            "resource": "Atlas vector recall setup",
                            "session": "Orion canary deployment",
                        }
                        search_results = {
                            kind: (
                                await client.post(
                                    "/api/v1/memory/search",
                                    json={"query": query, "limit": 3},
                                )
                            ).json()["data"]["results"]
                            for kind, query in queries.items()
                        }

        hits = {
            kind: any(result["uri"] == targets[kind] for result in results)
            for kind, results in search_results.items()
        }
        hit_rate = sum(1 for hit in hits.values() if hit) / len(hits)
        self.assertEqual(hits, {"memory": True, "resource": True, "session": True})
        self.assertEqual(hit_rate, 1.0)

    async def test_large_memory_search_decomposes_query_inside_probe(self) -> None:
        """Large search queries are split before probe vector search."""
        content = "assistant: Alice uses Python in Hangzhou."
        captured_system_prompts.clear()
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=capture_system_prompt_llm_completion),
                ) as llm_mock,
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        await client.post(
                            "/api/v1/session/message",
                            json={
                                "session_id": "session-large-recall",
                                "turn_id": "turn-1",
                                "messages": [
                                    {
                                        "role": "assistant",
                                        "content": content,
                                        "meta": {"entities": ["Alice"]},
                                    }
                                ],
                            },
                        )
                        await app.state.store_event_worker.wait_idle()
                        response = await client.post(
                            "/api/v1/memory/search",
                            json={
                                "query": (
                                    "总结一下这个 session 里关于写入链路、召回链路"
                                    "和 Qdrant 的所有讨论，按阶段列出结论"
                                ),
                                "limit": 3,
                            },
                        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertNotIn("plan", data)
        self.assertTrue(data["results"])
        prompts = [call.args[0] for call in llm_mock.call_args_list]
        self.assertTrue(
            any(
                "Split this user query into short retrieval queries" in p
                for p in prompts
            )
        )
        self.assertTrue(
            any("Select the best reason-tree entry URIs" in p for p in prompts)
        )
        self.assertIn(LAYER_DERIVATION_SYSTEM_PROMPT, captured_system_prompts)
        self.assertIn(QUERY_DECOMPOSITION_SYSTEM_PROMPT, captured_system_prompts)
        self.assertIn(REASON_TREE_SELECTION_SYSTEM_PROMPT, captured_system_prompts)
        self.assertIn(
            "summary",
            {item["kind"] for item in data["results"][0]["evidence"]},
        )

    async def test_memory_search_reranks_multiple_fused_candidates(self) -> None:
        """Medium recall reranks fused candidates without exposing internals."""
        captured_system_prompts.clear()
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=capture_system_prompt_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=keyword_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=keyword_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        first = await client.post(
                            "/api/v1/memory/store",
                            json={
                                "type": "memory",
                                "content": "Zephyr notebook planning uses Python.",
                                "category": "semantic",
                                "metadata": {},
                                "source": {"kind": "manual"},
                            },
                        )
                        second = await client.post(
                            "/api/v1/memory/store",
                            json={
                                "type": "memory",
                                "content": "Zephyr notebook planning uses Qdrant.",
                                "category": "semantic",
                                "metadata": {},
                                "source": {"kind": "manual"},
                            },
                        )
                        await app.state.store_event_worker.wait_idle()
                        response = await client.post(
                            "/api/v1/memory/search",
                            json={
                                "query": "Zephyr notebook planning",
                                "limit": 2,
                            },
                        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertIn(RECALL_RERANK_SYSTEM_PROMPT, captured_system_prompts)
        self.assertNotIn("plan", data)
        self.assertTrue(data["results"])
        self.assertNotIn("rerank", data["results"][0])

    async def test_memory_search_supports_litellm_rerank_provider(self) -> None:
        """LiteLLM rerank provider uses the API provider interface."""
        captured: dict[str, object] = {}

        async def fake_arerank(**kwargs: object) -> object:
            captured.update(kwargs)
            return type(
                "RerankResponse",
                (),
                {
                    "results": [
                        type(
                            "RerankItem",
                            (),
                            {"index": 0, "relevance_score": 0.2},
                        )(),
                        type(
                            "RerankItem",
                            (),
                            {"index": 1, "relevance_score": 0.9},
                        )(),
                    ]
                },
            )()

        client = LiteLLMRerankClient(
            model="cohere/rerank-v3.5",
            api_base="https://rerank.example/v1",
            api_key="test-rerank-key",
        )
        with patch.dict(
            sys.modules, {"litellm": SimpleNamespace(arerank=fake_arerank)}
        ):
            scores = await client.rerank_batch("Zephyr notebook", ["first", "second"])

        self.assertEqual(scores, [0.2, 0.9])
        self.assertEqual(captured["model"], "cohere/rerank-v3.5")
        self.assertEqual(captured["api_base"], "https://rerank.example/v1")
        self.assertEqual(captured["api_key"], "test-rerank-key")
        self.assertEqual(captured["query"], "Zephyr notebook")
        self.assertEqual(captured["documents"], ["first", "second"])
        self.assertFalse(captured["return_documents"])

    async def test_memory_search_supports_hosted_vllm_rerank_provider(self) -> None:
        """Hosted vLLM rerank provider uses LiteLLM's hosted_vllm adapter."""
        captured: dict[str, object] = {}

        async def fake_arerank(**kwargs: object) -> object:
            captured.update(kwargs)
            return type(
                "RerankResponse",
                (),
                {
                    "results": [
                        {"index": 0, "relevance_score": 0.3},
                        {"index": 1, "relevance_score": 0.8},
                    ]
                },
            )()

        client = HostedVLLMRerankClient(
            model="qwen3-reranker-0.6b",
            api_base="http://vllm-reranker:8000/v1",
            api_key="local-key",
        )
        with patch.dict(
            sys.modules, {"litellm": SimpleNamespace(arerank=fake_arerank)}
        ):
            scores = await client.rerank_batch("Zephyr notebook", ["first", "second"])

        self.assertEqual(scores, [0.3, 0.8])
        self.assertEqual(captured["model"], "hosted_vllm/qwen3-reranker-0.6b")
        self.assertEqual(captured["custom_llm_provider"], "hosted_vllm")
        self.assertEqual(captured["api_base"], "http://vllm-reranker:8000/v1")
        self.assertEqual(captured["top_n"], 2)

    async def test_memory_search_supports_openai_rerank_provider(self) -> None:
        """OpenAI-compatible rerank provider calls /rerank directly."""
        captured: dict[str, object] = {}

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {
                    "results": [
                        {"index": 0, "relevance_score": 0.4},
                        {"index": 1, "relevance_score": 0.7},
                    ]
                }

        class FakeClient:
            def __init__(self, **kwargs: object) -> None:
                captured["client_kwargs"] = kwargs

            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def post(self, url: str, **kwargs: object) -> FakeResponse:
                captured["url"] = url
                captured.update(kwargs)
                return FakeResponse()

        client = OpenAIRerankClient(
            model="qwen3-reranker-0.6b",
            api_base="http://vllm-reranker:8000/v1",
            api_key="local-key",
        )
        with patch("httpx.AsyncClient", FakeClient):
            scores = await client.rerank_batch("Zephyr notebook", ["first", "second"])

        self.assertEqual(scores, [0.4, 0.7])
        self.assertEqual(captured["url"], "http://vllm-reranker:8000/v1/rerank")
        self.assertEqual(
            captured["json"],
            {
                "model": "qwen3-reranker-0.6b",
                "query": "Zephyr notebook",
                "documents": ["first", "second"],
                "top_n": 2,
                "return_documents": False,
            },
        )

    async def test_large_memory_search_rejects_invalid_reason_tree_uri(self) -> None:
        """ReasonTree selection fails when the LLM returns only invalid URIs."""
        content = "assistant: Alice uses Python in Hangzhou."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=invalid_reason_tree_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        await client.post(
                            "/api/v1/session/message",
                            json={
                                "session_id": "session-invalid-reason-tree",
                                "turn_id": "turn-1",
                                "messages": [
                                    {
                                        "role": "assistant",
                                        "content": content,
                                        "meta": {"entities": ["Alice"]},
                                    }
                                ],
                            },
                        )
                        await app.state.store_event_worker.wait_idle()
                        with self.assertRaisesRegex(
                            ValueError,
                            "Reason tree selected no valid candidate URIs",
                        ):
                            await client.post(
                                "/api/v1/memory/search",
                                json={
                                    "query": (
                                        "总结一下这个 session 里关于写入链路、召回链路"
                                        "和 Qdrant 的所有讨论，按阶段列出结论"
                                    ),
                                    "limit": 3,
                                },
                            )

    async def test_reason_tree_node_selection_hydrates_source_primary(self) -> None:
        """ReasonTree node selection returns the source primary payload."""
        content = "# Atlas Ops\n\nAtlas configures Qdrant HNSW."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=reason_tree_node_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=keyword_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=keyword_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        store_response = await client.post(
                            "/api/v1/memory/store",
                            json={
                                "type": "resource",
                                "content": content,
                                "category": "semantic",
                                "metadata": {},
                                "source": {
                                    "kind": "document",
                                    "path": "/docs/atlas.md",
                                    "title": "Atlas",
                                },
                            },
                        )
                        await app.state.store_event_worker.wait_idle()
                        response = await client.post(
                            "/api/v1/memory/search",
                            json={
                                "query": (
                                    "总结一下所有关于 Atlas Qdrant HNSW 配置的信息，"
                                    "按阶段完整列出"
                                ),
                                "limit": 3,
                            },
                        )

        self.assertEqual(store_response.status_code, 200)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        reason_hit = next(
            result
            for result in data["results"]
            if any(item["kind"] == "summary" for item in result["evidence"])
        )
        self.assertEqual(reason_hit["uri"], store_response.json()["uri"])
        self.assertNotIn("/reason_tree/", reason_hit["uri"])
        self.assertIn("Atlas configures Qdrant HNSW", reason_hit["content"])

    async def test_large_memory_search_rejects_oversized_llm_query(self) -> None:
        """Probe does not truncate oversized LLM retrieval queries."""
        content = "assistant: Alice uses Python in Hangzhou."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=oversized_query_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        await client.post(
                            "/api/v1/session/message",
                            json={
                                "session_id": "session-oversized-recall",
                                "turn_id": "turn-1",
                                "messages": [
                                    {
                                        "role": "assistant",
                                        "content": content,
                                        "meta": {"entities": ["Alice"]},
                                    }
                                ],
                            },
                        )
                        await app.state.store_event_worker.wait_idle()
                        with self.assertRaisesRegex(
                            ValueError,
                            "oversized retrieval query",
                        ):
                            await client.post(
                                "/api/v1/memory/search",
                                json={
                                    "query": (
                                        "总结一下这个 session 里关于写入链路、召回链路"
                                        "和 Qdrant 的所有讨论，按阶段列出结论"
                                    ),
                                    "limit": 3,
                                },
                            )

    async def test_memory_search_does_not_expose_execution_internals(self) -> None:
        """Public search response exposes business evidence, not execution internals."""
        content = "assistant: Alice uses Python in Hangzhou."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            headers = auth_headers(data_root)
            with (
                patch(
                    "opencortex.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers=headers,
                    ) as client:
                        await client.post(
                            "/api/v1/session/message",
                            json={
                                "session_id": "session-global-recall",
                                "turn_id": "turn-1",
                                "messages": [
                                    {
                                        "role": "assistant",
                                        "content": content,
                                        "meta": {"entities": ["Alice"]},
                                    }
                                ],
                            },
                        )
                        await app.state.store_event_worker.wait_idle()
                        response = await client.post(
                            "/api/v1/memory/search",
                            json={
                                "query": "Alice Python",
                                "limit": 3,
                            },
                        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertNotIn("plan", data)
        result = data["results"][0]
        self.assertIn("source", result)
        self.assertIn("evidence", result)
        self.assertNotIn("probe", result)
        self.assertNotIn("cone_expansion", result)
        self.assertNotIn("reason_tree", result)
        self.assertNotIn("rerank", result)
        self.assertNotIn("retrieval_surfaces", result)

    async def test_lifespan_requires_llm_configuration(self) -> None:
        """The app fails fast when no LLM API key is configured."""
        with TemporaryDirectory() as data_root:
            app = create_app(settings=Settings(data_root=data_root, llm_api_key=""))

            with self.assertRaises(ValueError):
                async with app.router.lifespan_context(app):
                    pass

    async def test_settings_disable_identity_context_from_environment(self) -> None:
        """Environment variables can disable identity context injection."""
        with patch.dict(
            "os.environ",
            {"OPENCORTEX_APP_IDENTITY_CONTEXT_ENABLED": "false"},
        ):
            get_settings.cache_clear()
            settings = get_settings()
            app = create_app(settings=settings)
        get_settings.cache_clear()

        self.assertFalse(settings.identity_context_enabled)
        self.assertTrue(
            any(
                middleware.cls.__name__ == "WriteRequestContextMiddleware"
                for middleware in app.user_middleware
            )
        )

    async def test_create_app_accepts_explicit_settings(self) -> None:
        """Explicit settings control FastAPI metadata."""
        app = create_app(
            settings=Settings(
                app_name="Custom App",
                app_description="Custom description",
                app_version="1.2.3",
            )
        )

        self.assertEqual(app.title, "Custom App")
        self.assertEqual(app.description, "Custom description")
        self.assertEqual(app.version, "1.2.3")
