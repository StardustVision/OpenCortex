# SPDX-License-Identifier: Apache-2.0
"""Tests for the opencortex_app FastAPI application."""

from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx
from httpx import ASGITransport

from opencortex_app.app import create_app
from opencortex_app.settings import Settings, get_settings


def app_settings(data_root: str) -> Settings:
    """Return settings with required LLM config for app lifespan tests."""
    return Settings(data_root=data_root, llm_api_key="test-key")


def fast_merge_settings(data_root: str) -> Settings:
    """Return settings that make one session turn trigger merge."""
    settings = app_settings(data_root)
    settings.conversation_merge_token_budget = 1
    return settings


def derived_json(content: str) -> str:
    """Return fake LLM derivation JSON."""
    return (
        "{"
        f'"abstract":"derived abstract: {content}",'
        f'"overview":"derived overview: {content}",'
        '"keywords":["python"],'
        '"entities":["Alice"],'
        '"anchor_handles":["Alice"],'
        '"fact_points":["Alice uses Python"]'
        "}"
    )


async def fake_llm_completion(prompt: str, *, temperature: float = 0.0) -> str:
    """Return fake LLM JSON for layer derivation and query planning."""
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
    return derived_json(extract_prompt_content(prompt))


async def oversized_query_llm_completion(
    prompt: str,
    *,
    temperature: float = 0.0,
) -> str:
    """Return an oversized retrieval query for error-path tests."""
    if "Split this user query into short retrieval queries" in prompt:
        return '{"retrieval_queries":["' + ("x" * 120) + '"]}'
    return await fake_llm_completion(prompt, temperature=temperature)


async def invalid_reason_tree_llm_completion(
    prompt: str,
    *,
    temperature: float = 0.0,
) -> str:
    """Return an invalid reason-tree URI for error-path tests."""
    if "Select the best reason-tree entry URIs" in prompt:
        return '{"selected_uris":["opencortex://invalid/reason-tree"]}'
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
    """Verify the standalone opencortex_app FastAPI surface."""

    async def test_create_app_exposes_write_and_search_routes(self) -> None:
        """The app exposes write endpoints and memory search."""
        app = create_app()

        paths = {route.path for route in app.routes}
        self.assertIn("/api/v1/memory/store", paths)
        self.assertIn("/api/v1/memory/search", paths)
        self.assertIn("/api/v1/session/message", paths)
        self.assertIn("/api/v1/session/end", paths)

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
            with (
                patch(
                    "opencortex_app.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
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

    async def test_memory_store_uses_llm_derived_layers(self) -> None:
        """Store writes use LLM-derived abstract and overview layers."""
        content = "0123456789" * 150
        with TemporaryDirectory() as data_root:
            app = create_app(settings=app_settings(data_root))
            with (
                patch(
                    "opencortex_app.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
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

    async def test_session_message_runs_full_worker_write_chain(self) -> None:
        """Session writes produce primary, FS layers, cleanup, and retrieval indexes."""
        content = "assistant: Alice uses Python in Hangzhou."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            with (
                patch(
                    "opencortex_app.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
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

    async def test_memory_search_recalls_written_indexes(self) -> None:
        """Search recalls primary records through secondary retrieval surfaces."""
        content = "assistant: Alice uses Python in Hangzhou."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            with (
                patch(
                    "opencortex_app.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
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
        result = data["results"][0]
        self.assertEqual(result["session_id"], "session-recall")
        self.assertEqual(result["content"], content)
        self.assertIn("l0_object", result["retrieval_surfaces"])
        probe = data["plan"]["probe"]
        self.assertEqual(probe["evidence"]["object_candidate_count"], 1)
        self.assertGreaterEqual(probe["evidence"]["locator_candidate_count"], 1)
        self.assertTrue(probe["starting_uris"])
        self.assertNotIn("range_source", probe)
        self.assertNotIn("hard_range", probe)
        self.assertNotIn("range_miss", probe)
        self.assertNotIn("direct_hits", probe)
        self.assertNotIn("locator_hits", probe)
        self.assertNotIn("retrieval_queries", probe)
        self.assertNotIn("query_vector", probe)
        self.assertNotIn("search_vectors", probe)
        self.assertNotIn("scope_filter", data["plan"])
        self.assertNotIn("session_scope", data["plan"])
        self.assertNotIn("target_uri", data["plan"])
        self.assertNotIn("search_vectors", data["plan"])
        self.assertEqual(data["plan"]["starting_uris"], probe["starting_uris"])
        self.assertEqual(data["plan"]["decision"], "focused")
        self.assertEqual(data["plan"]["depth"], "l2")
        self.assertFalse(data["plan"]["reason_tree"]["enabled"])
        self.assertFalse(data["plan"]["cone_expansion"]["enabled"])
        self.assertGreater(data["plan"]["surface_limits"]["l0_object"], 0)
        self.assertGreater(data["plan"]["surface_weights"]["fact_index"], 0)
        self.assertTrue(
            {
                "anchor_index",
                "fact_index",
                "entity_index",
                "reason_tree_index",
            }.intersection(result["retrieval_surfaces"])
        )

    async def test_write_to_recall_hit_rate_for_memory_resource_session(
        self,
    ) -> None:
        """Memory, resource, and session writes are each recalled by target query."""
        targets: dict[str, str] = {}
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            with (
                patch(
                    "opencortex_app.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=keyword_embedding,
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=keyword_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
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
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            with (
                patch(
                    "opencortex_app.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ) as llm_mock,
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
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
        plan = response.json()["data"]["plan"]
        probe = plan["probe"]
        self.assertGreaterEqual(probe["evidence"]["candidate_count"], 1)
        self.assertNotIn("retrieval_queries", probe)
        self.assertNotIn("direct_hits", probe)
        self.assertNotIn("locator_hits", probe)
        self.assertTrue(plan["reason_tree"]["enabled"])
        self.assertTrue(plan["reason_tree"]["use_llm"])
        self.assertTrue(plan["cone_expansion"]["enabled"])
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
        self.assertIn("reason_tree_index", plan["surface_limits"])
        self.assertIn(
            "reason_tree_index",
            response.json()["data"]["results"][0]["retrieval_surfaces"],
        )

    async def test_large_memory_search_rejects_invalid_reason_tree_uri(self) -> None:
        """ReasonTree selection fails when the LLM returns only invalid URIs."""
        content = "assistant: Alice uses Python in Hangzhou."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            with (
                patch(
                    "opencortex_app.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=invalid_reason_tree_llm_completion),
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
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

    async def test_large_memory_search_rejects_oversized_llm_query(self) -> None:
        """Probe does not truncate oversized LLM retrieval queries."""
        content = "assistant: Alice uses Python in Hangzhou."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            with (
                patch(
                    "opencortex_app.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=oversized_query_llm_completion),
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
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

    async def test_memory_search_does_not_expose_probe_range_fields(self) -> None:
        """Probe output should only expose evidence used by the planner."""
        content = "assistant: Alice uses Python in Hangzhou."
        with TemporaryDirectory() as data_root:
            app = create_app(settings=fast_merge_settings(data_root))
            with (
                patch(
                    "opencortex_app.llm.client.LLMCompletion.complete",
                    new=AsyncMock(side_effect=fake_llm_completion),
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed",
                    side_effect=fake_embedding,
                ),
                patch(
                    "opencortex_app.vector.embedder.OpenAIEmbeddingClient.embed_batch",
                    side_effect=fake_embedding_batch,
                ),
            ):
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
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
        probe = response.json()["data"]["plan"]["probe"]
        self.assertNotIn("range_source", probe)
        self.assertNotIn("hard_range", probe)
        self.assertNotIn("range_miss", probe)
        self.assertNotIn("session_scope", probe)
        self.assertNotIn("target_uri", probe)

    async def test_lifespan_requires_llm_configuration(self) -> None:
        """The app fails fast when no LLM API key is configured."""
        with TemporaryDirectory() as data_root:
            app = create_app(settings=Settings(data_root=data_root, llm_api_key=""))

            with self.assertRaises(ValueError):
                async with app.router.lifespan_context(app):
                    pass

    async def test_settings_disable_identity_context_from_environment(self) -> None:
        """Environment variables can disable identity context middleware."""
        with patch.dict(
            "os.environ",
            {"OPENCORTEX_APP_IDENTITY_CONTEXT_ENABLED": "false"},
        ):
            get_settings.cache_clear()
            settings = get_settings()
            app = create_app(settings=settings)
        get_settings.cache_clear()

        self.assertFalse(settings.identity_context_enabled)
        self.assertFalse(
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
