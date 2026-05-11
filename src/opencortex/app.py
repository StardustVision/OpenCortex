# SPDX-License-Identifier: Apache-2.0
"""FastAPI application for OpenCortex write-path APIs."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI

from opencortex.auth.routes import admin_router, auth_router
from opencortex.auth.token import ensure_secret, register_token_record
from opencortex.core.identity import (
    get_collection_name,
)
from opencortex.core.middlewares import WriteRequestContextMiddleware
from opencortex.mcp.routes import router as mcp_router
from opencortex.runtime import AppRuntime, AppRuntimeConfig
from opencortex.settings import Settings, get_settings
from opencortex.storage.namespace import CortexNamespace
from opencortex.store.document_tree import DocumentParser
from opencortex.store.event.actions import (
    CheckUpdateAction,
    CortexStorageAction,
    EntityIndexAction,
    ReasonTreeBuildAction,
    ReasonTreeIndexAction,
    SearchIndexAction,
    SemanticDeriveAction,
    SessionCleanupAction,
    SessionMergeAction,
)
from opencortex.store.event.events import StoreEvents
from opencortex.store.event.worker import EventWorker
from opencortex.store.routes import router as store_router
from opencortex.console.routes import router as console_router  # noqa: E402  load after store imports to avoid circular init
from opencortex.store.session.buffer import SessionBuffer
from opencortex.store.session.merger import SessionMerger
from opencortex.store.writer.primary_record_writer import PrimaryRecordWriter

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize and tear down runtime dependencies for write APIs."""
    settings = app.state.settings
    runtime = AppRuntime(
        config=AppRuntimeConfig(
            data_root=settings.data_root,
            vector_dimension=settings.vector_dimension,
            qdrant_url=settings.qdrant_url,
            qdrant_api_key=settings.qdrant_api_key,
            qdrant_timeout=settings.qdrant_timeout,
            embedding_api_key=settings.embedding_api_key,
            embedding_api_base=settings.embedding_api_base,
            embedding_model=settings.embedding_model,
            llm_api_key=settings.llm_api_key,
            llm_api_base=settings.llm_api_base,
            llm_model=settings.llm_model,
            llm_api_style=settings.llm_api_style,
            conversation_merge_token_budget=settings.conversation_merge_token_budget,
            session_idle_ttl=settings.session_idle_ttl,
            immediate_event_ttl_hours=settings.immediate_event_ttl_hours,
            merged_event_ttl_hours=settings.merged_event_ttl_hours,
            store_event_worker_concurrency=settings.store_event_worker_concurrency,
        )
    )
    await runtime.init()
    configure_store_state(app, runtime)
    event_worker = build_event_worker(app, runtime)
    event_worker.subscribe()
    await event_worker.start()
    app.state.store_event_worker = event_worker
    logger.info("opencortex.initialized", data_root=runtime.config.data_root)

    try:
        yield
    finally:
        await shutdown_store_state(app)
        await runtime.close()
        logger.info("opencortex.closed")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the opencortex FastAPI application."""
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        description=settings.app_description,
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.add_middleware(WriteRequestContextMiddleware)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(store_router)
    app.include_router(console_router)
    app.include_router(mcp_router)
    app.state.settings = settings
    configure_admin_token(settings)
    return app


def configure_admin_token(settings: Settings) -> None:
    """Register a configured bootstrap admin token."""
    if not settings.admin_api_token:
        return
    secret = ensure_secret(settings.data_root)
    record = register_token_record(
        settings.data_root,
        settings.admin_api_token,
        secret=secret,
    )
    if record["role"] != "admin":
        raise ValueError("Configured admin_api_token must have role=admin")


def configure_store_state(app: FastAPI, runtime: AppRuntime) -> None:
    """Attach write-path dependencies to FastAPI application state."""
    config = runtime.config
    app.state.runtime = runtime
    app.state.store_config = config
    app.state.vector_store = runtime.vector_store
    app.state.store_embedder = runtime.embedder
    app.state.store_llm_completion = runtime.llm_completion
    app.state.store_memory_events = runtime.memory_events
    app.state.store_event_queue = runtime.store_event_queue
    app.state.collection_resolver = runtime.get_collection
    app.state.ttl_resolver = runtime.ttl_from_hours
    app.state.store_document_parser = DocumentParser()
    app.state.session_buffer = SessionBuffer(
        collection_resolver=lambda: get_collection_name() or "context",
        merge_token_budget=config.conversation_merge_token_budget,
        idle_ttl_seconds=getattr(config, "session_idle_ttl", 1800.0),
    )


def build_event_worker(app: FastAPI, runtime: AppRuntime) -> EventWorker:
    """Build the background worker for write-path side effects."""
    event_namespace = CortexNamespace(
        collection_resolver=runtime.get_collection,
    )
    event_writer = PrimaryRecordWriter(
        vector_store=runtime.vector_store,
        collection_resolver=runtime.get_collection,
        namespace=event_namespace,
    )
    event_store_events = StoreEvents(runtime.memory_events)
    event_merger = SessionMerger(
        buffer=app.state.session_buffer,
        namespace=event_namespace,
        writer=event_writer,
        events=event_store_events,
        config=runtime.config,
        ttl_from_hours=runtime.ttl_from_hours,
    )
    return EventWorker(
        memory_events=runtime.memory_events,
        event_queue=runtime.store_event_queue,
        actions=[
            SemanticDeriveAction(
                vector_store=runtime.vector_store,
                collection_resolver=runtime.get_collection,
                llm_completion=runtime.llm_completion,
                embedder=runtime.embedder,
            ),
            SearchIndexAction(
                vector_store=runtime.vector_store,
                collection_resolver=runtime.get_collection,
                embedder=runtime.embedder,
            ),
            EntityIndexAction(
                vector_store=runtime.vector_store,
                collection_resolver=runtime.get_collection,
                embedder=runtime.embedder,
            ),
            CortexStorageAction(cortex_storage=runtime.cortex_storage),
            SessionMergeAction(
                buffer=app.state.session_buffer,
                merger=event_merger,
            ),
            SessionCleanupAction(
                vector_store=runtime.vector_store,
                collection_resolver=runtime.get_collection,
            ),
            ReasonTreeIndexAction(
                vector_store=runtime.vector_store,
                collection_resolver=runtime.get_collection,
                namespace=event_namespace,
                embedder=runtime.embedder,
            ),
            ReasonTreeBuildAction(
                vector_store=runtime.vector_store,
                collection_resolver=runtime.get_collection,
                llm_completion=runtime.llm_completion,
                embedder=runtime.embedder,
            ),
            CheckUpdateAction(),
        ],
        concurrency=runtime.config.store_event_worker_concurrency,
    )


async def shutdown_store_state(app: FastAPI) -> None:
    """Stop store background workers and clear application state."""
    event_worker = getattr(app.state, "store_event_worker", None)
    if event_worker is not None:
        await event_worker.close()

    session_buffer = getattr(app.state, "session_buffer", None)
    if session_buffer is not None:
        session_buffer.clear()

    for attr in store_state_attrs():
        setattr(app.state, attr, None)


def store_state_attrs() -> tuple[str, ...]:
    """Return state attributes owned by opencortex."""
    return (
        "store_event_worker",
        "session_buffer",
        "ttl_resolver",
        "collection_resolver",
        "store_memory_events",
        "store_event_queue",
        "store_document_parser",
        "store_llm_completion",
        "store_embedder",
        "vector_store",
        "store_config",
        "runtime",
    )


__all__ = ["create_app"]
