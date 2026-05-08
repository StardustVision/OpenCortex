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


def fake_embedding(_text: str) -> object:
    """Return one deterministic embedding."""
    return type(
        "Embedding",
        (),
        {"dense_vector": [0.1] * 1024, "sparse_vector": None},
    )()


def fake_embedding_batch(texts: list[str]) -> list[object]:
    """Return one deterministic embedding per text."""
    return [fake_embedding(text) for text in texts]


class TestOpenCortexApp(unittest.IsolatedAsyncioTestCase):
    """Verify the standalone opencortex_app FastAPI surface."""

    async def test_create_app_exposes_only_write_routes(self) -> None:
        """The app exposes the three primary write endpoints."""
        app = create_app()

        paths = {route.path for route in app.routes}
        self.assertIn("/api/v1/memory/store", paths)
        self.assertIn("/api/v1/session/message", paths)
        self.assertIn("/api/v1/session/end", paths)
        self.assertNotIn("/api/v1/memory/search", paths)

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
                    new=AsyncMock(
                        return_value=derived_json("User prefers dark theme.")
                    ),
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
                    new=AsyncMock(return_value=derived_json(content)),
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
                    new=AsyncMock(return_value=derived_json(content)),
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
