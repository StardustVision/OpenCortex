# SPDX-License-Identifier: Apache-2.0
"""FastAPI HTTP Server for OpenCortex.

Hosts the MemoryOrchestrator and exposes all MCP tool capabilities as REST
endpoints.  This is the primary deployment target — the MCP Server acts as
a thin client that forwards requests here.

Usage::

    python -m opencortex.http --host 127.0.0.1 --port 8921 --config server.json
"""

import logging
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, Response
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
from opencortex.http.models import (
    # Context Protocol
    ContextPrepareResponse,
    ContextRequest,
    IntentShouldRecallRequest,
    KnowledgeApproveRequest,
    KnowledgeRejectRequest,
    KnowledgeSearchRequest,
    MemoryBatchStoreRequest,
    MemoryFeedbackRequest,
    MemoryForgetRequest,
    MemorySearchRequest,
    MemorySearchResponse,
    MemoryStoreRequest,
    PromoteToSharedRequest,
    SessionBeginRequest,
    SessionEndRequest,
    SessionMessageRequest,
    # Cortex Alpha
    SessionMessagesRequest,
)
from opencortex.http.request_context import (
    reset_request_identity,
    reset_request_project_id,
    reset_request_role,
    set_collection_name,
    set_request_identity,
    set_request_project_id,
    set_request_role,
)
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.retrieve.types import ContextType

logger = logging.getLogger(__name__)

# Module-level orchestrator, initialized in lifespan
_orchestrator: Optional[MemoryOrchestrator] = None

# Module-level JWT secret, loaded once at startup
_jwt_secret: Optional[str] = None

# Paths that do NOT require authentication
_AUTH_WHITELIST = {
    "/api/v1/memory/health",
    "/docs",
    "/openapi.json",
}

_CODE_PATTERN = re.compile(
    r"^\s*(def |class |import |from |if |for |while |return |"
    r"const |let |var |function |\{|\}|//|#!)"
)


def _check_store_warnings(abstract: str) -> list:
    """Return advisory warnings for a store request. Never blocks storage."""
    warnings = []
    stripped = abstract.strip()
    if len(stripped) < 10:
        warnings.append(
            {
                "key": "abstract_too_short",
                "message": (
                    "Memory abstract should be at least 10 characters "
                    "for useful retrieval"
                ),
            }
        )
        return warnings

    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if len(lines) >= 2:
        code_lines = sum(1 for ln in lines if _CODE_PATTERN.match(ln))
        if code_lines / len(lines) > 0.8:
            warnings.append(
                {
                    "key": "code_snippet_detected",
                    "message": (
                        "Consider storing a description of the code pattern "
                        "rather than raw code"
                    ),
                }
            )
    return warnings


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
            try:
                return await call_next(request)
            finally:
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
        if collection:
            set_collection_name(collection)

        try:
            return await call_next(request)
        finally:
            reset_request_identity(id_tokens)
            reset_request_project_id(project_token)
            reset_request_role(role_token)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize and teardown the MemoryOrchestrator."""
    global _orchestrator, _jwt_secret
    config = get_config()
    _jwt_secret = ensure_secret(config.data_root)
    _orchestrator = MemoryOrchestrator(config=config)
    await _orchestrator.init()
    logger.info("[HTTP] Orchestrator initialized (data_root=%s)", config.data_root)

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

    register_admin_routes(_orchestrator, _jwt_secret)

    # Initialize insights components (optional feature)
    try:
        from opencortex.insights.agent import InsightsAgent
        from opencortex.insights.api import create_insights_router
        from opencortex.insights.collector import InsightsCollector
        from opencortex.insights.report import ReportManager
        from opencortex.models.llm_factory import create_llm_completion

        if not _orchestrator._trace_store:
            raise Exception(
                "TraceStore not initialized; enable trace_splitter in "
                "cortex_alpha config"
            )

        llm_callable = create_llm_completion(_orchestrator._config)
        if not llm_callable:
            raise Exception("LLM not configured; insights requires LLM API key")

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

        collector = InsightsCollector(_orchestrator._trace_store, _orchestrator)
        llm = LLMWrapper(llm_callable)
        agent = InsightsAgent(llm=llm, collector=collector)
        report_manager = ReportManager(_orchestrator._fs)

        _orchestrator._insights_report_manager = report_manager

        insights_router = create_insights_router(
            agent=agent,
            report_manager=report_manager,
            orchestrator=_orchestrator,
        )
        app.include_router(insights_router)
        logger.info("[HTTP] Insights components initialized and routes registered")
    except Exception as e:
        logger.warning(f"[HTTP] Insights components not available: {e}")
        _orchestrator._insights_report_manager = None

    # Skill Engine routes
    try:
        from opencortex.skill_engine.http_routes import router as skill_router

        app.include_router(skill_router)
        logger.info("[HTTP] Skill Engine routes registered")
    except Exception as e:
        logger.info("[HTTP] Skill Engine routes not available: %s", e)

    try:
        yield
    finally:
        await _orchestrator.close()
        _orchestrator = None
        logger.info("[HTTP] Orchestrator closed")


def create_app() -> FastAPI:
    """Create and return the FastAPI application."""
    app = FastAPI(
        title="OpenCortex HTTP Server",
        description="Memory and context management system for AI Agents",
        version="0.7.0",
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
    # Core Memory
    # =====================================================================

    @app.post("/api/v1/memory/store")
    async def memory_store(req: MemoryStoreRequest) -> Dict[str, Any]:
        # URI is always auto-generated by backend based on identity,
        # context_type, and category.
        # Client-provided uri is ignored to prevent malformed storage paths.
        warnings = _check_store_warnings(req.abstract)
        result = await _orchestrator.add(
            abstract=req.abstract,
            content=req.content,
            overview=req.overview,
            category=req.category,
            context_type=req.context_type,
            meta=req.meta,
            dedup=req.dedup,
            embed_text=req.embed_text,
        )
        resp: Dict[str, Any] = {
            "uri": result.uri,
            "context_type": result.context_type,
            "category": result.category,
            "abstract": result.abstract,
        }
        if result.overview:
            resp["overview"] = result.overview
        dedup_action = result.meta.get("dedup_action")
        if dedup_action:
            resp["dedup_action"] = dedup_action
        if warnings:
            resp["warnings"] = warnings
        return resp

    @app.post("/api/v1/memory/batch_store")
    async def memory_batch_store(req: MemoryBatchStoreRequest) -> Dict[str, Any]:
        return await _orchestrator.batch_add(
            items=[item.model_dump() for item in req.items],
            source_path=req.source_path,
            scan_meta=req.scan_meta,
        )

    @app.post("/api/v1/memory/promote_to_shared")
    async def memory_promote_to_shared(req: PromoteToSharedRequest) -> Dict[str, Any]:
        return await _orchestrator.promote_to_shared(
            uris=req.uris,
            project_id=req.project_id,
        )

    @app.post(
        "/api/v1/memory/search",
        response_model=MemorySearchResponse,
        response_model_exclude_none=True,
    )
    async def memory_search(
        req: MemorySearchRequest, request: Request
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

        result = await _orchestrator.search(
            query=req.query,
            limit=req.limit,
            context_type=ct,
            metadata_filter=metadata_filter,
            detail_level=req.detail_level,
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
    async def memory_feedback(req: MemoryFeedbackRequest) -> Dict[str, str]:
        await _orchestrator.feedback(uri=req.uri, reward=req.reward)
        return {"status": "ok", "uri": req.uri, "reward": str(req.reward)}

    @app.get("/api/v1/memory/list")
    async def memory_list(
        category: Optional[str] = None,
        context_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        include_payload: bool = False,
    ) -> Dict[str, Any]:
        """List user's accessible memories (private + shared)."""
        items = await _orchestrator.list_memories(
            category=category,
            context_type=context_type,
            limit=limit,
            offset=offset,
            include_payload=include_payload,
        )
        return {"results": items, "total": len(items)}

    @app.get("/api/v1/memory/index")
    async def memory_index(
        context_type: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Lightweight index of all memories, grouped by type."""
        return await _orchestrator.memory_index(
            context_type=context_type,
            limit=limit,
        )

    @app.get("/api/v1/memory/stats")
    async def memory_stats() -> Dict[str, Any]:
        return await _orchestrator.stats()

    @app.get("/api/v1/memory/derive_status")
    async def memory_derive_status(uri: str) -> Dict[str, Any]:
        """Check async derive status for a document URI."""
        return await _orchestrator.derive_status(uri)

    @app.post("/api/v1/memory/wait_derives")
    async def memory_wait_derives() -> Dict[str, Any]:
        """Wait until all in-flight deferred derives complete. Returns count of completed derives."""
        await _orchestrator.wait_deferred_derives()
        return {"status": "ok"}

    @app.post("/api/v1/memory/forget")
    async def memory_forget(req: MemoryForgetRequest) -> Dict[str, Any]:
        """Delete a memory by exact URI or semantic search query."""
        if req.uri:
            count = await _orchestrator.remove(req.uri)
            return {"status": "ok", "forgotten": count, "uri": req.uri}
        if req.query:
            results = await _orchestrator.search(query=req.query, limit=1)
            if not results:
                return {"status": "not_found", "forgotten": 0}
            uri = results[0].uri
            count = await _orchestrator.remove(uri)
            return {"status": "ok", "forgotten": count, "uri": uri}
        raise HTTPException(400, "Either uri or query is required")

    @app.post("/api/v1/memory/decay")
    async def memory_decay() -> Dict[str, Any]:
        result = await _orchestrator.decay()
        return result or {}

    @app.get("/api/v1/memory/health")
    async def memory_health() -> Dict[str, Any]:
        return await _orchestrator.health_check()

    # =====================================================================
    # Intent
    # =====================================================================

    @app.post("/api/v1/intent/should_recall")
    async def intent_should_recall(req: IntentShouldRecallRequest) -> Dict[str, Any]:
        return (await _orchestrator.probe_memory(req.query)).to_dict()

    # =====================================================================
    # Session
    # =====================================================================

    @app.post("/api/v1/session/begin")
    async def session_begin(req: SessionBeginRequest) -> Dict[str, Any]:
        return await _orchestrator.session_begin(session_id=req.session_id)

    @app.post("/api/v1/session/message")
    async def session_message(req: SessionMessageRequest) -> Dict[str, Any]:
        return await _orchestrator.session_message(
            session_id=req.session_id,
            role=req.role,
            content=req.content,
        )

    @app.post("/api/v1/session/end")
    async def session_end(req: SessionEndRequest) -> Dict[str, Any]:
        return await _orchestrator.session_end(
            session_id=req.session_id,
            quality_score=req.quality_score,
        )

    # =====================================================================
    # Cortex Alpha
    # =====================================================================

    @app.post("/api/v1/session/messages")
    async def session_messages_batch(req: SessionMessagesRequest) -> Dict[str, Any]:
        """Batch message recording (Observer debounce buffer)."""
        if _orchestrator._observer:
            from opencortex.http.request_context import get_effective_identity

            tid, uid = get_effective_identity()
            _orchestrator._observer.record_batch(
                session_id=_orchestrator._observer_session_id(
                    req.session_id,
                    tenant_id=tid,
                    user_id=uid,
                ),
                messages=req.messages,
                tenant_id=tid,
                user_id=uid,
            )
        return {"ok": True, "count": len(req.messages)}

    @app.post("/api/v1/knowledge/search")
    async def knowledge_search(req: KnowledgeSearchRequest) -> Dict[str, Any]:
        if not _orchestrator._config.cortex_alpha.archivist_enabled:
            return {"error": "feature disabled"}
        return await _orchestrator.knowledge_search(
            query=req.query,
            types=req.types,
            limit=req.limit,
        )

    @app.post("/api/v1/knowledge/approve")
    async def knowledge_approve(req: KnowledgeApproveRequest) -> Dict[str, Any]:
        if not _orchestrator._config.cortex_alpha.archivist_enabled:
            return {"error": "feature disabled"}
        return await _orchestrator.knowledge_approve(req.knowledge_id)

    @app.post("/api/v1/knowledge/reject")
    async def knowledge_reject(req: KnowledgeRejectRequest) -> Dict[str, Any]:
        if not _orchestrator._config.cortex_alpha.archivist_enabled:
            return {"error": "feature disabled"}
        return await _orchestrator.knowledge_reject(req.knowledge_id)

    @app.get("/api/v1/knowledge/candidates")
    async def knowledge_candidates() -> Dict[str, Any]:
        if not _orchestrator._config.cortex_alpha.archivist_enabled:
            return {"error": "feature disabled"}
        return await _orchestrator.knowledge_list_candidates()

    @app.post("/api/v1/archivist/trigger")
    async def archivist_trigger() -> Dict[str, Any]:
        if not _orchestrator._config.cortex_alpha.archivist_enabled:
            return {"error": "feature disabled"}
        return await _orchestrator.archivist_trigger()

    @app.get("/api/v1/archivist/status")
    async def archivist_status() -> Dict[str, Any]:
        if not _orchestrator._config.cortex_alpha.archivist_enabled:
            return {"error": "feature disabled"}
        return await _orchestrator.archivist_status()

    # =====================================================================
    # Context Protocol
    # =====================================================================

    @app.post("/api/v1/context")
    async def context_handler(req: ContextRequest) -> Dict[str, Any]:
        """Unified memory_context lifecycle: prepare / commit / end."""
        from opencortex.http.request_context import get_effective_identity

        tid, uid = get_effective_identity()
        response = await _orchestrator._context_manager.handle(
            session_id=req.session_id,
            phase=req.phase,
            tenant_id=tid,
            user_id=uid,
            turn_id=req.turn_id,
            messages=[m.model_dump() for m in req.messages] if req.messages else None,
            cited_uris=req.cited_uris,
            config=req.config.model_dump() if req.config else None,
            tool_calls=[t.model_dump() for t in req.tool_calls]
            if req.tool_calls
            else None,
        )
        if req.phase == "prepare":
            return ContextPrepareResponse.model_validate(response).model_dump(
                exclude_none=True
            )
        return response

    # =====================================================================
    # System Status
    # =====================================================================

    @app.get("/api/v1/system/status")
    async def system_status(type: str = "doctor") -> Dict[str, Any]:
        return await _orchestrator.system_status(status_type=type)

    # =====================================================================
    # Content (L0/L1/L2 on-demand loading)
    # =====================================================================

    @app.get("/api/v1/content/abstract")
    async def content_abstract(uri: str) -> Dict[str, Any]:
        """Read L0 abstract from CortexFS."""
        text = await _orchestrator._fs.abstract(uri)
        return {"status": "ok", "result": text}

    @app.get("/api/v1/content/overview")
    async def content_overview(uri: str) -> Dict[str, Any]:
        """Read L1 overview from CortexFS."""
        text = await _orchestrator._fs.overview(uri)
        return {"status": "ok", "result": text}

    @app.get("/api/v1/content/read")
    async def content_read(
        uri: str,
        offset: int = 0,
        limit: int = -1,
    ) -> Dict[str, Any]:
        """Read L2 content from CortexFS."""
        raw = await _orchestrator._fs.read(
            uri + "/content.md",
            offset=offset,
            size=limit,
        )
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return {"status": "ok", "result": text}
