# SPDX-License-Identifier: Apache-2.0
"""FastAPI HTTP server for OpenCortex.

Hosts CortexMemory and exposes memory, context, retrieval, and
administrative capabilities as REST endpoints.

Usage::

    python -m opencortex.http --host 127.0.0.1 --port 8921 --config server.json
"""

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from opencortex.auth.token import (
    decode_token,
    ensure_secret,
    generate_admin_token,
    load_token_records,
    save_token_record,
)
from opencortex.config import get_config
from opencortex.core.identity import IdentityProfile
from opencortex.cortex_memory import CortexMemory
from opencortex.http.memory_store import store_warnings
from opencortex.http.models import (
    IntentShouldRecallRequest,
    MemoryFeedbackRequest,
    MemoryForgetRequest,
    MemorySearchRequest,
    MemorySearchResponse,
    MemoryStoreRequest,
    PromoteToSharedRequest,
    SessionEndRequest,
    SessionTurnRequest,
)
from opencortex.http.request_context import (
    get_collection_name,
    reset_collection_name,
    reset_identity_profile,
    reset_request_identity,
    reset_request_project_id,
    reset_request_role,
    set_collection_name,
    set_identity_profile,
    set_request_identity,
    set_request_project_id,
    set_request_role,
)
from opencortex.retrieve.types import ContextType
from opencortex.storage.cortex_namespace import CortexNamespace
from opencortex.store.embedder import StoreEmbedder
from opencortex.store.event_actions import (
    CortexFSAction,
    EntityIndexAction,
    ReasoningTreeIndexAction,
    SearchIndexAction,
    SessionCleanupAction,
    SessionMergeAction,
)
from opencortex.store.event_worker import EventWorker
from opencortex.store.events import StoreEvents
from opencortex.store.memory_store import MemoryStore
from opencortex.store.resource_store import ResourceStore
from opencortex.store.schemas import (
    StoredRecord,
    memory_store_input_from_request,
    resource_store_input_from_request,
    session_end_input_from_request,
    session_message_input_from_request,
)
from opencortex.store.session_buffer import SessionBuffer
from opencortex.store.session_ender import SessionEnder
from opencortex.store.session_merger import SessionMerger
from opencortex.store.session_store import SessionStore
from opencortex.writer.primary_record_writer import PrimaryRecordWriter

logger = logging.getLogger(__name__)

# Module-level JWT secret, loaded once at startup
_jwt_secret: Optional[str] = None

# Paths that do NOT require authentication
_AUTH_WHITELIST = {
    "/api/v1/memory/health",
    "/docs",
    "/openapi.json",
}


def _check_store_warnings(abstract: str) -> list:
    """Return advisory warnings for a store request. Never blocks storage."""
    return store_warnings(abstract)


def store_response(
    stored: StoredRecord,
    warnings: list[Dict[str, str]],
) -> Dict[str, Any]:
    """Build the HTTP response payload for a stored record."""
    resp: Dict[str, Any] = {
        "uri": stored.uri,
        "context_type": stored.context_type,
        "category": stored.category,
        "abstract": stored.abstract,
    }
    if stored.overview:
        resp["overview"] = stored.overview
    dedup_action = stored.meta.get("dedup_action")
    if dedup_action:
        resp["dedup_action"] = dedup_action
    if warnings:
        resp["warnings"] = warnings
    return resp


def get_memory(request: Request) -> CortexMemory:
    """FastAPI dependency for the application memory facade."""
    memory = getattr(request.app.state, "memory", None)
    if memory is None:
        raise HTTPException(status_code=503, detail="OpenCortex is not initialized")
    return memory


def get_store_storage(request: Request) -> Any:
    """FastAPI dependency for vector storage used by store flows."""
    storage = getattr(request.app.state, "store_storage", None)
    if storage is None and getattr(request.app.state, "memory", None) is not None:
        storage = request.app.state.memory._storage
    if storage is None:
        raise HTTPException(status_code=503, detail="Store storage is not initialized")
    return storage


def get_store_config(request: Request) -> Any:
    """FastAPI dependency for store configuration."""
    config = getattr(request.app.state, "store_config", None)
    if config is None and getattr(request.app.state, "memory", None) is not None:
        config = request.app.state.memory._config
    if config is None:
        raise HTTPException(status_code=503, detail="Store config is not initialized")
    return config


def get_collection_resolver(request: Request) -> Any:
    """FastAPI dependency for active collection resolution."""
    resolver = getattr(request.app.state, "collection_resolver", None)
    if resolver is None and getattr(request.app.state, "memory", None) is not None:
        resolver = request.app.state.memory._get_collection
    if resolver is None:
        raise HTTPException(
            status_code=503,
            detail="Collection resolver is not initialized",
        )
    return resolver


def get_ttl_resolver(request: Request) -> Any:
    """FastAPI dependency for TTL timestamp conversion."""
    resolver = getattr(request.app.state, "ttl_resolver", None)
    if resolver is None and getattr(request.app.state, "memory", None) is not None:
        resolver = request.app.state.memory._ttl_from_hours
    if resolver is None:
        raise HTTPException(status_code=503, detail="TTL resolver is not initialized")
    return resolver


def get_llm_completion(request: Request) -> Any:
    """FastAPI dependency for optional store derivation LLM."""
    completion = getattr(request.app.state, "store_llm_completion", None)
    if completion is None and getattr(request.app.state, "memory", None) is not None:
        completion = request.app.state.memory._llm_completion
    return completion


def get_embedding_model(request: Request) -> Any:
    """FastAPI dependency for optional store embedding model."""
    embedder = getattr(request.app.state, "store_embedder", None)
    if embedder is None and getattr(request.app.state, "memory", None) is not None:
        embedder = request.app.state.memory._embedder
    return embedder


def get_memory_events(request: Request) -> Any:
    """FastAPI dependency for store lifecycle events."""
    events = getattr(request.app.state, "store_memory_events", None)
    if events is None and getattr(request.app.state, "memory", None) is not None:
        events = request.app.state.memory._memory_events
    return events


def get_cortex_namespace(
    storage: Annotated[Any, Depends(get_store_storage)],
    collection_resolver: Annotated[Any, Depends(get_collection_resolver)],
) -> CortexNamespace:
    """FastAPI dependency for URI namespace resolution."""
    return CortexNamespace(
        storage=storage,
        collection_resolver=collection_resolver,
    )


def get_store_embedder(
    embedding_model: Annotated[Any, Depends(get_embedding_model)],
) -> StoreEmbedder:
    """FastAPI dependency for store embedding."""
    return StoreEmbedder(embedding_model)


def get_primary_record_writer(
    config: Annotated[Any, Depends(get_store_config)],
    storage: Annotated[Any, Depends(get_store_storage)],
    collection_resolver: Annotated[Any, Depends(get_collection_resolver)],
    ttl_resolver: Annotated[Any, Depends(get_ttl_resolver)],
) -> PrimaryRecordWriter:
    """FastAPI dependency for primary record writes."""
    return PrimaryRecordWriter(
        config=config,
        storage=storage,
        collection_resolver=collection_resolver,
        ttl_from_hours=ttl_resolver,
    )


def get_store_events(
    memory_events: Annotated[Any, Depends(get_memory_events)],
) -> StoreEvents:
    """FastAPI dependency for store event publishing."""
    return StoreEvents(memory_events)


def get_memory_store(
    llm_completion: Annotated[Any, Depends(get_llm_completion)],
    namespace: Annotated[CortexNamespace, Depends(get_cortex_namespace)],
    embedder: Annotated[StoreEmbedder, Depends(get_store_embedder)],
    writer: Annotated[PrimaryRecordWriter, Depends(get_primary_record_writer)],
    events: Annotated[StoreEvents, Depends(get_store_events)],
) -> MemoryStore:
    """FastAPI dependency for memory store flow."""
    return MemoryStore(
        namespace=namespace,
        llm_completion=llm_completion,
        embedder=embedder,
        writer=writer,
        events=events,
    )


def get_resource_store(
    llm_completion: Annotated[Any, Depends(get_llm_completion)],
    namespace: Annotated[CortexNamespace, Depends(get_cortex_namespace)],
    embedder: Annotated[StoreEmbedder, Depends(get_store_embedder)],
    writer: Annotated[PrimaryRecordWriter, Depends(get_primary_record_writer)],
    events: Annotated[StoreEvents, Depends(get_store_events)],
) -> ResourceStore:
    """FastAPI dependency for resource store flow."""
    return ResourceStore(
        namespace=namespace,
        llm_completion=llm_completion,
        embedder=embedder,
        writer=writer,
        events=events,
    )


def get_session_buffer(
    request: Request,
) -> SessionBuffer:
    """FastAPI dependency for session message buffer state."""
    buffer = getattr(request.app.state, "session_buffer", None)
    if buffer is None and getattr(request.app.state, "memory", None) is not None:
        config = request.app.state.memory._config
        buffer = SessionBuffer(
            collection_resolver=lambda: get_collection_name() or "context",
            merge_token_budget=config.conversation_merge_token_budget,
            idle_ttl_seconds=getattr(config, "session_idle_ttl", 1800.0),
        )
        request.app.state.session_buffer = buffer
    if buffer is None:
        raise HTTPException(status_code=503, detail="Session buffer is not initialized")
    return buffer


def get_session_store(
    buffer: Annotated[SessionBuffer, Depends(get_session_buffer)],
    namespace: Annotated[CortexNamespace, Depends(get_cortex_namespace)],
    embedder: Annotated[StoreEmbedder, Depends(get_store_embedder)],
    writer: Annotated[PrimaryRecordWriter, Depends(get_primary_record_writer)],
    events: Annotated[StoreEvents, Depends(get_store_events)],
) -> SessionStore:
    """FastAPI dependency for session message store flow."""
    return SessionStore(
        buffer=buffer,
        namespace=namespace,
        embedder=embedder,
        writer=writer,
        events=events,
    )


def get_session_merger(
    buffer: Annotated[SessionBuffer, Depends(get_session_buffer)],
    namespace: Annotated[CortexNamespace, Depends(get_cortex_namespace)],
    embedder: Annotated[StoreEmbedder, Depends(get_store_embedder)],
    writer: Annotated[PrimaryRecordWriter, Depends(get_primary_record_writer)],
    events: Annotated[StoreEvents, Depends(get_store_events)],
    storage: Annotated[Any, Depends(get_store_storage)],
    collection_resolver: Annotated[Any, Depends(get_collection_resolver)],
) -> SessionMerger:
    """FastAPI dependency for session merge flow."""
    return SessionMerger(
        buffer=buffer,
        namespace=namespace,
        embedder=embedder,
        writer=writer,
        events=events,
        storage=storage,
        collection_resolver=collection_resolver,
    )


def get_session_ender(
    buffer: Annotated[SessionBuffer, Depends(get_session_buffer)],
    merger: Annotated[SessionMerger, Depends(get_session_merger)],
    namespace: Annotated[CortexNamespace, Depends(get_cortex_namespace)],
    embedder: Annotated[StoreEmbedder, Depends(get_store_embedder)],
    writer: Annotated[PrimaryRecordWriter, Depends(get_primary_record_writer)],
    events: Annotated[StoreEvents, Depends(get_store_events)],
    storage: Annotated[Any, Depends(get_store_storage)],
    collection_resolver: Annotated[Any, Depends(get_collection_resolver)],
) -> SessionEnder:
    """FastAPI dependency for session end flow."""
    return SessionEnder(
        buffer=buffer,
        merger=merger,
        namespace=namespace,
        embedder=embedder,
        writer=writer,
        events=events,
        storage=storage,
        collection_resolver=collection_resolver,
    )


# ---------------------------------------------------------------------------
# Request Context Middleware
# ---------------------------------------------------------------------------


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Authenticate requests via JWT Bearer token and set per-request identity.

    The ``Authorization: Bearer <token>`` header is required on all paths
    except those in ``_AUTH_WHITELIST``.  Identity (tenant_id, user_id) is
    extracted from the JWT claims (``tid``, ``uid``).
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Apply auth and request identity before forwarding the request."""
        path = request.url.path

        # Whitelisted paths bypass authentication
        if path in _AUTH_WHITELIST or path.startswith("/console"):
            id_tokens = set_request_identity("default", "default")
            project_id = request.headers.get("x-project-id", "public")
            project_token = set_request_project_id(project_id)
            collection = request.headers.get("x-collection") or ""
            collection_token = set_collection_name(collection) if collection else None
            profile_token = set_identity_profile(
                IdentityProfile(
                    tenant_id="default",
                    user_id="default",
                    project_id=project_id,
                    collection=collection,
                )
            )
            try:
                return await call_next(request)
            finally:
                reset_identity_profile(profile_token)
                if collection_token is not None:
                    reset_collection_name(collection_token)
                reset_request_identity(id_tokens)
                reset_request_project_id(project_token)

        # Extract and validate JWT
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid token"},
            )

        token = auth_header[7:]  # strip "Bearer "
        try:
            claims = decode_token(token, _jwt_secret)
        except Exception:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid token"},
            )

        tenant_id = claims.get("tid", "default")
        user_id = claims.get("uid", "default")
        id_tokens = set_request_identity(tenant_id, user_id)

        role = claims.get("role", "user")
        role_token = set_request_role(role)

        project_id = request.headers.get("x-project-id", "public")
        project_token = set_request_project_id(project_id)

        collection = request.headers.get("x-collection")
        collection_token = set_collection_name(collection) if collection else None
        profile_token = set_identity_profile(
            IdentityProfile(
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=project_id,
                collection=collection or "",
            )
        )

        try:
            return await call_next(request)
        finally:
            reset_identity_profile(profile_token)
            if collection_token is not None:
                reset_collection_name(collection_token)
            reset_request_identity(id_tokens)
            reset_request_project_id(project_token)
            reset_request_role(role_token)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize and teardown CortexMemory."""
    global memory, _jwt_secret
    config = get_config()
    _jwt_secret = ensure_secret(config.data_root)
    memory = CortexMemory(config=config)
    await memory.init()
    app.state.memory = memory
    app.state.store_config = config
    app.state.store_storage = memory._storage
    app.state.store_embedder = memory._embedder
    app.state.store_llm_completion = memory._llm_completion
    app.state.store_memory_events = memory._memory_events
    app.state.collection_resolver = memory._get_collection
    app.state.ttl_resolver = memory._ttl_from_hours
    app.state.session_buffer = SessionBuffer(
        collection_resolver=lambda: get_collection_name() or "context",
        merge_token_budget=config.conversation_merge_token_budget,
        idle_ttl_seconds=config.session_idle_ttl,
    )
    event_namespace = CortexNamespace(
        storage=memory._storage,
        collection_resolver=memory._get_collection,
    )
    event_embedder = StoreEmbedder(memory._embedder)
    event_writer = PrimaryRecordWriter(
        config=config,
        storage=memory._storage,
        collection_resolver=memory._get_collection,
        ttl_from_hours=memory._ttl_from_hours,
    )
    event_store_events = StoreEvents(memory._memory_events)
    event_merger = SessionMerger(
        buffer=app.state.session_buffer,
        namespace=event_namespace,
        embedder=event_embedder,
        writer=event_writer,
        events=event_store_events,
        storage=memory._storage,
        collection_resolver=memory._get_collection,
    )
    event_worker = EventWorker(
        memory_events=memory._memory_events,
        actions=[
            SearchIndexAction(
                storage=memory._storage,
                collection_resolver=memory._get_collection,
                embedder=memory._embedder,
            ),
            EntityIndexAction(
                entity_index=getattr(memory, "_entity_index", None),
                collection_resolver=memory._get_collection,
            ),
            CortexFSAction(fs=memory._fs),
            SessionMergeAction(
                buffer=app.state.session_buffer,
                merger=event_merger,
            ),
            SessionCleanupAction(
                storage=memory._storage,
                collection_resolver=memory._get_collection,
            ),
            ReasoningTreeIndexAction(),
        ],
    )
    event_worker.subscribe()
    await event_worker.start()
    app.state.store_event_worker = event_worker
    logger.info("[HTTP] CortexMemory initialized (data_root=%s)", config.data_root)

    # Auto-generate admin token on first startup
    records = load_token_records(config.data_root)
    admin_rec = next((r for r in records if r.get("role") == "admin"), None)
    if admin_rec:
        logger.info("[HTTP] Admin token (existing): %s", admin_rec["token"])
    else:
        admin_token = generate_admin_token(_jwt_secret)
        save_token_record(
            config.data_root, admin_token, "_system", "_admin", role="admin"
        )
        logger.info("[HTTP] Admin token (new): %s", admin_token)

    from opencortex.http.admin_routes import register_admin_routes

    register_admin_routes(memory, _jwt_secret)

    # Insights routes are plugin-owned and disabled by default.
    if getattr(memory._config, "insights_enabled", False):
        from opencortex.insights.agent import InsightsAgent
        from opencortex.insights.api import create_insights_router
        from opencortex.insights.collector import InsightsCollector
        from opencortex.insights.report import ReportManager
        from opencortex.models.llm_factory import create_llm_completion

        if not memory._trace_store:
            raise Exception(
                "TraceStore not initialized; enable trace_splitter in "
                "cortex_alpha config"
            )

        # REVIEW closure tracker RELY-01 (plan 009 review): the
        # InsightsAgent needs an LLMCompletion wrapper and previously
        # built a SECOND one here that was never closed — partially
        # regressing the very TCP CLOSE_WAIT leak this PR ships to
        # fix. We now hold the second wrapper on CortexMemory so
        # ``CortexMemory.close()`` releases it on shutdown.
        # Pre-existing concern (RELY-02): ``LLMWrapper.generate``
        # spawns a fresh event loop per call, which prevents httpx
        # connection-pool reuse across calls. That's out of scope for
        # this leak fix — InsightsAgent's sync→async bridge needs a
        # separate refactor. Logged as residual.
        llm_callable = create_llm_completion(memory._config)
        if not llm_callable:
            raise Exception("LLM not configured; insights requires LLM API key")
        memory._insights_llm_completion = llm_callable

        class LLMWrapper:
            def __init__(self, callable_: Any) -> None:
                self._callable = callable_

            def generate(self, prompt: str, **kwargs: Any) -> str:
                import asyncio

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    return loop.run_until_complete(self._callable(prompt))
                finally:
                    loop.close()

        collector = InsightsCollector(memory._trace_store, memory)
        llm = LLMWrapper(llm_callable)
        agent = InsightsAgent(llm=llm, collector=collector)
        report_manager = ReportManager(memory._fs)

        memory._insights_report_manager = report_manager

        insights_router = create_insights_router(
            agent=agent,
            report_manager=report_manager,
            orchestrator=memory,
        )
        app.include_router(insights_router)
        logger.info("[HTTP] Insights components initialized and routes registered")
    else:
        logger.info("[HTTP] Insights routes disabled")
        memory._insights_report_manager = None

    # Skill Engine routes are plugin-owned and disabled by default.
    if getattr(memory._config, "skill_engine_enabled", False):
        try:
            from opencortex.skill_engine.http_routes import router as skill_router

            app.include_router(skill_router)
            logger.info("[HTTP] Skill Engine routes registered")
        except Exception as e:
            logger.info("[HTTP] Skill Engine routes not available: %s", e)

    try:
        yield
    finally:
        if getattr(app.state, "store_event_worker", None) is not None:
            await app.state.store_event_worker.close()
        app.state.store_event_worker = None
        app.state.ttl_resolver = None
        app.state.collection_resolver = None
        app.state.store_memory_events = None
        app.state.store_llm_completion = None
        app.state.store_embedder = None
        app.state.store_storage = None
        app.state.store_config = None
        if getattr(app.state, "session_buffer", None) is not None:
            app.state.session_buffer.clear()
        app.state.session_buffer = None
        app.state.memory = None
        await memory.close()
        memory = None
        logger.info("[HTTP] Orchestrator closed")


def create_app() -> FastAPI:
    """Create and return the FastAPI application."""
    app = FastAPI(
        title="OpenCortex HTTP Server",
        description="Memory and context management system for AI Agents",
        version="0.8.0",
        lifespan=_lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    from opencortex.http.admin_routes import router as admin_router

    app.include_router(admin_router)
    _register_routes(app)

    # =====================================================================
    # Console UI (static files)
    # =====================================================================
    import os

    _web_dist = os.path.join(os.path.dirname(__file__), "..", "..", "..", "web", "dist")
    _web_dist = os.path.normpath(_web_dist)
    if os.path.isdir(_web_dist) and os.path.isfile(
        os.path.join(_web_dist, "index.html")
    ):
        from starlette.staticfiles import StaticFiles

        app.mount(
            "/console", StaticFiles(directory=_web_dist, html=True), name="console"
        )
        logger.info("[HTTP] Console UI mounted at /console (serving %s)", _web_dist)

    return app


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def _register_routes(app: FastAPI) -> None:
    """Register all REST endpoints on *app*."""
    # =====================================================================
    # Deprecation shims
    # =====================================================================

    @app.api_route(
        "/api/v1/benchmark/conversation_ingest",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def _legacy_benchmark_conversation_ingest_gone() -> Dict[str, Any]:
        """Return 410 Gone for the pre-admin-gate URL.

        The benchmark ingest endpoint moved under the admin namespace in
        v0.7.x to add ``_require_admin()`` enforcement (see
        ``CHANGELOG.md``). This shim makes the move discoverable for any
        out-of-tree caller still pointing at the old path; FastAPI's
        default 404 carries no migration breadcrumb. Drop after one or
        two releases of grace.
        """
        raise HTTPException(
            status_code=410,
            detail={
                "reason": "moved",
                "new_url": "/api/v1/admin/benchmark/conversation_ingest",
                "removed_in": "0.8.0",
                "note": (
                    "Endpoint relocated under the admin namespace and now "
                    "requires admin role. Use the new URL with an admin "
                    "Bearer token."
                ),
            },
        )

    # =====================================================================
    # Core Memory
    # =====================================================================

    @app.post("/api/v1/memory/store")
    async def memory_store(
        req: MemoryStoreRequest,
        memory_store: Annotated[MemoryStore, Depends(get_memory_store)],
        resource_store: Annotated[ResourceStore, Depends(get_resource_store)],
    ) -> Dict[str, Any]:
        warnings = _check_store_warnings(req.abstract)
        if req.context_type == ContextType.MEMORY:
            stored = await memory_store.store(memory_store_input_from_request(req))
            return store_response(stored, warnings)
        if req.context_type == ContextType.RESOURCE:
            stored = await resource_store.store(resource_store_input_from_request(req))
            return store_response(stored, warnings)
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported store context_type: {req.context_type}",
        )

    @app.post("/api/v1/memory/promote_to_shared")
    async def memory_promote_to_shared(
        req: PromoteToSharedRequest,
        memory: Annotated[CortexMemory, Depends(get_memory)],
    ) -> Dict[str, Any]:
        return await memory.promote_to_shared(
            uris=req.uris,
            project_id=req.project_id,
        )

    @app.post(
        "/api/v1/memory/search",
        response_model=MemorySearchResponse,
        response_model_exclude_none=True,
    )
    async def memory_search(
        req: MemorySearchRequest,
        request: Request,
        memory: Annotated[CortexMemory, Depends(get_memory)],
    ) -> MemorySearchResponse:
        ct = ContextType(req.context_type) if req.context_type else None
        metadata_filter = None
        if req.category:
            metadata_filter = {
                "op": "must",
                "field": "category",
                "conds": [req.category],
            }
        if req.metadata_filter:
            metadata_filter = (
                {"op": "and", "conds": [metadata_filter, req.metadata_filter]}
                if metadata_filter
                else req.metadata_filter
            )

        result = await memory.search(
            query=req.query,
            limit=req.limit,
            context_type=ct,
            target_uri=req.target_uri,
            score_threshold=req.score_threshold,
            metadata_filter=metadata_filter,
            detail_level=req.detail_level,
            meta={"target_doc_id": req.target_doc_id} if req.target_doc_id else None,
            session_context=req.session_context,
        )
        response_payload = result.to_memory_search_response()
        # v0.6: explain query param support
        explain_mode = request.query_params.get("explain")
        if (
            explain_mode
            and hasattr(result, "explain_summary")
            and result.explain_summary
        ):
            from dataclasses import asdict

            response_payload["explain_summary"] = asdict(result.explain_summary)
        if (
            explain_mode == "detail"
            and hasattr(result, "query_results")
            and result.query_results
        ):
            from dataclasses import asdict

            response_payload["explain_detail"] = [
                asdict(qr.explain) for qr in result.query_results if qr.explain
            ]
        return MemorySearchResponse.model_validate(response_payload)

    @app.post("/api/v1/memory/feedback")
    async def memory_feedback(
        req: MemoryFeedbackRequest,
        memory: Annotated[CortexMemory, Depends(get_memory)],
    ) -> Dict[str, str]:
        await memory.feedback(uri=req.uri, reward=req.reward)
        return {"status": "ok", "uri": req.uri, "reward": str(req.reward)}

    @app.get("/api/v1/memory/list")
    async def memory_list(
        memory: Annotated[CortexMemory, Depends(get_memory)],
        category: Optional[str] = None,
        context_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        include_payload: bool = False,
    ) -> Dict[str, Any]:
        """List user's accessible memories (private + shared)."""
        items = await memory.list_memories(
            category=category,
            context_type=context_type,
            limit=limit,
            offset=offset,
            include_payload=include_payload,
        )
        return {"results": items, "total": len(items)}

    @app.get("/api/v1/memory/index")
    async def memory_index(
        memory: Annotated[CortexMemory, Depends(get_memory)],
        context_type: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Lightweight index of all memories, grouped by type."""
        return await memory.memory_index(
            context_type=context_type,
            limit=limit,
        )

    @app.get("/api/v1/memory/stats")
    async def memory_stats(
        memory: Annotated[CortexMemory, Depends(get_memory)],
    ) -> Dict[str, Any]:
        return await memory.stats()

    @app.get("/api/v1/memory/derive_status")
    async def memory_derive_status(
        uri: str,
        memory: Annotated[CortexMemory, Depends(get_memory)],
    ) -> Dict[str, Any]:
        """Check async derive status for a document URI."""
        return await memory.derive_status(uri)

    @app.post("/api/v1/memory/wait_derives")
    async def memory_wait_derives(
        memory: Annotated[CortexMemory, Depends(get_memory)],
    ) -> Dict[str, Any]:
        """Wait until all in-flight deferred derives complete."""
        await memory.wait_deferred_derives()
        return {"status": "ok"}

    @app.post("/api/v1/memory/forget")
    async def memory_forget(
        req: MemoryForgetRequest,
        memory: Annotated[CortexMemory, Depends(get_memory)],
    ) -> Dict[str, Any]:
        """Delete a memory by exact URI or semantic search query."""
        if req.uri:
            count = await memory.remove(req.uri)
            return {"status": "ok", "forgotten": count, "uri": req.uri}
        if req.query:
            results = await memory.search(query=req.query, limit=1)
            if not results:
                return {"status": "not_found", "forgotten": 0}
            uri = results[0].uri
            count = await memory.remove(uri)
            return {"status": "ok", "forgotten": count, "uri": uri}
        raise HTTPException(400, "Either uri or query is required")

    @app.post("/api/v1/memory/decay")
    async def memory_decay(
        memory: Annotated[CortexMemory, Depends(get_memory)],
    ) -> Dict[str, Any]:
        result = await memory.decay()
        return result or {}

    @app.get("/api/v1/memory/health")
    async def memory_health(
        memory: Annotated[CortexMemory, Depends(get_memory)],
    ) -> Dict[str, Any]:
        return await memory.health_check()

    # =====================================================================
    # Intent
    # =====================================================================

    @app.post("/api/v1/intent/should_recall")
    async def intent_should_recall(
        req: IntentShouldRecallRequest,
        memory: Annotated[CortexMemory, Depends(get_memory)],
    ) -> Dict[str, Any]:
        return (await memory.probe_memory(req.query)).to_dict()

    # =====================================================================
    # Session
    # =====================================================================

    @app.post("/api/v1/session/message")
    async def session_message(
        req: SessionTurnRequest,
        session_store: Annotated[SessionStore, Depends(get_session_store)],
    ) -> Dict[str, Any]:
        return (
            await session_store.message(session_message_input_from_request(req))
        ).model_dump()

    @app.post("/api/v1/session/end")
    async def session_end(
        req: SessionEndRequest,
        session_ender: Annotated[SessionEnder, Depends(get_session_ender)],
    ) -> Dict[str, Any]:
        return (
            await session_ender.end(session_end_input_from_request(req))
        ).model_dump()

    # =====================================================================
    # System Status
    # =====================================================================

    @app.get("/api/v1/system/status")
    async def system_status(
        memory: Annotated[CortexMemory, Depends(get_memory)],
        type: str = "doctor",
    ) -> Dict[str, Any]:
        return await memory.system_status(status_type=type)

    # =====================================================================
    # Content (L0/L1/L2 on-demand loading)
    # =====================================================================

    @app.get("/api/v1/content/abstract")
    async def content_abstract(
        uri: str,
        memory: Annotated[CortexMemory, Depends(get_memory)],
    ) -> Dict[str, Any]:
        """Read L0 abstract from CortexFS."""
        text = await memory._fs.abstract(uri)
        return {"status": "ok", "result": text}

    @app.get("/api/v1/content/overview")
    async def content_overview(
        uri: str,
        memory: Annotated[CortexMemory, Depends(get_memory)],
    ) -> Dict[str, Any]:
        """Read L1 overview from CortexFS."""
        text = await memory._fs.overview(uri)
        return {"status": "ok", "result": text}

    @app.get("/api/v1/content/read")
    async def content_read(
        uri: str,
        memory: Annotated[CortexMemory, Depends(get_memory)],
        offset: int = 0,
        limit: int = -1,
    ) -> Dict[str, Any]:
        """Read L2 content from CortexFS."""
        raw = await memory._fs.read(
            uri + "/content.md",
            offset=offset,
            size=limit,
        )
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return {"status": "ok", "result": text}
