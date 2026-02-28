# SPDX-License-Identifier: Apache-2.0
"""
FastAPI HTTP Server for OpenCortex.

Hosts the MemoryOrchestrator and exposes all MCP tool capabilities as REST
endpoints.  This is the primary deployment target — the MCP Server acts as
a thin client that forwards requests here.

Usage::

    python -m opencortex.http --host 127.0.0.1 --port 8921 --config opencortex.json
"""

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from opencortex.config import get_config
from opencortex.http.request_context import reset_request_identity, set_request_identity
from opencortex.http.models import (
    ErrorRecordRequest,
    ErrorSuggestRequest,
    HooksExportRequest,
    HooksInitRequest,
    HooksLearnRequest,
    HooksPretrainRequest,
    HooksRecallRequest,
    HooksRememberRequest,
    HooksRouteRequest,
    MemoryFeedbackRequest,
    MemorySearchRequest,
    MemoryStoreRequest,
    SessionBeginRequest,
    SessionEndRequest,
    SessionMessageRequest,
    TrajectoryBeginRequest,
    TrajectoryEndRequest,
    TrajectoryStepRequest,
)
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.retrieve.types import ContextType

logger = logging.getLogger(__name__)

# Module-level orchestrator, initialized in lifespan
_orchestrator: Optional[MemoryOrchestrator] = None


# ---------------------------------------------------------------------------
# Tenant Identity Middleware
# ---------------------------------------------------------------------------

class TenantIdentityMiddleware(BaseHTTPMiddleware):
    """Extract per-request tenant/user identity from HTTP headers.

    Headers:
        X-Tenant-ID — overrides config tenant_id for this request
        X-User-ID   — overrides config user_id for this request

    Falls back to CortexConfig defaults when headers are absent.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        config = get_config()
        tenant_id = request.headers.get("x-tenant-id", config.tenant_id)
        user_id = request.headers.get("x-user-id", config.user_id)
        tokens = set_request_identity(tenant_id, user_id)
        try:
            return await call_next(request)
        finally:
            reset_request_identity(tokens)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Initialize and teardown the MemoryOrchestrator."""
    global _orchestrator
    config = get_config()
    _orchestrator = MemoryOrchestrator(config=config)
    await _orchestrator.init()
    logger.info(
        "[HTTP] Orchestrator initialized (tenant=%s, user=%s)",
        config.tenant_id,
        config.user_id,
    )
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
        version="0.2.0",
        lifespan=_lifespan,
    )
    app.add_middleware(TenantIdentityMiddleware)
    _register_routes(app)
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
        result = await _orchestrator.add(
            abstract=req.abstract,
            content=req.content,
            overview=req.overview,
            category=req.category,
            context_type=req.context_type,
            uri=req.uri,
            meta=req.meta,
        )
        resp: Dict[str, Any] = {
            "uri": result.uri,
            "context_type": result.context_type,
            "category": result.category,
            "abstract": result.abstract,
        }
        if result.overview:
            resp["overview"] = result.overview
        return resp

    @app.post("/api/v1/memory/search")
    async def memory_search(req: MemorySearchRequest) -> Dict[str, Any]:
        ct = ContextType(req.context_type) if req.context_type else None
        metadata_filter = None
        if req.category:
            metadata_filter = {"op": "must", "field": "category", "conds": [req.category]}

        result = await _orchestrator.search(
            query=req.query,
            limit=req.limit,
            context_type=ct,
            metadata_filter=metadata_filter,
            detail_level=req.detail_level,
        )
        items = []
        for matched in result:
            item: Dict[str, Any] = {
                "uri": matched.uri,
                "abstract": matched.abstract,
                "context_type": str(matched.context_type),
                "score": getattr(matched, "score", None),
            }
            if matched.overview is not None:
                item["overview"] = matched.overview
            if matched.content is not None:
                item["content"] = matched.content
            items.append(item)
        resp: Dict[str, Any] = {"results": items, "total": result.total}
        if result.search_intent:
            resp["search_intent"] = {
                "intent_type": result.search_intent.intent_type,
                "top_k": result.search_intent.top_k,
                "detail_level": result.search_intent.detail_level.value,
                "time_scope": result.search_intent.time_scope,
                "should_recall": result.search_intent.should_recall,
            }
        return resp

    @app.post("/api/v1/memory/feedback")
    async def memory_feedback(req: MemoryFeedbackRequest) -> Dict[str, str]:
        await _orchestrator.feedback(uri=req.uri, reward=req.reward)
        return {"status": "ok", "uri": req.uri, "reward": str(req.reward)}

    @app.get("/api/v1/memory/stats")
    async def memory_stats() -> Dict[str, Any]:
        return await _orchestrator.stats()

    @app.post("/api/v1/memory/decay")
    async def memory_decay() -> Dict[str, Any]:
        result = await _orchestrator.decay()
        return result or {}

    @app.get("/api/v1/memory/health")
    async def memory_health() -> Dict[str, Any]:
        return await _orchestrator.health_check()

    # =====================================================================
    # Hooks Learn
    # =====================================================================

    @app.post("/api/v1/hooks/learn")
    async def hooks_learn(req: HooksLearnRequest) -> Dict[str, Any]:
        actions = req.available_actions.split(",") if req.available_actions else None
        return await _orchestrator.hooks_learn(
            state=req.state,
            action=req.action,
            reward=req.reward,
            available_actions=actions,
        )

    @app.post("/api/v1/hooks/remember")
    async def hooks_remember(req: HooksRememberRequest) -> Dict[str, Any]:
        return await _orchestrator.hooks_remember(
            content=req.content,
            memory_type=req.memory_type,
        )

    @app.post("/api/v1/hooks/recall")
    async def hooks_recall(req: HooksRecallRequest) -> List[Dict[str, Any]]:
        return await _orchestrator.hooks_recall(
            query=req.query,
            limit=req.limit,
        )

    @app.get("/api/v1/hooks/stats")
    async def hooks_stats() -> Dict[str, Any]:
        return await _orchestrator.hooks_stats()

    # =====================================================================
    # Trajectory
    # =====================================================================

    @app.post("/api/v1/hooks/trajectory/begin")
    async def trajectory_begin(req: TrajectoryBeginRequest) -> Dict[str, Any]:
        return await _orchestrator.hooks_trajectory_begin(
            trajectory_id=req.trajectory_id,
            initial_state=req.initial_state,
        )

    @app.post("/api/v1/hooks/trajectory/step")
    async def trajectory_step(req: TrajectoryStepRequest) -> Dict[str, Any]:
        return await _orchestrator.hooks_trajectory_step(
            trajectory_id=req.trajectory_id,
            action=req.action,
            reward=req.reward,
            next_state=req.next_state or None,
        )

    @app.post("/api/v1/hooks/trajectory/end")
    async def trajectory_end(req: TrajectoryEndRequest) -> Dict[str, Any]:
        return await _orchestrator.hooks_trajectory_end(
            trajectory_id=req.trajectory_id,
            quality_score=req.quality_score,
        )

    # =====================================================================
    # Error
    # =====================================================================

    @app.post("/api/v1/hooks/error/record")
    async def error_record(req: ErrorRecordRequest) -> Dict[str, Any]:
        return await _orchestrator.hooks_error_record(
            error=req.error,
            fix=req.fix,
            context=req.context or None,
        )

    @app.post("/api/v1/hooks/error/suggest")
    async def error_suggest(req: ErrorSuggestRequest) -> List[Dict[str, Any]]:
        return await _orchestrator.hooks_error_suggest(error=req.error)

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
    # Integration
    # =====================================================================

    @app.post("/api/v1/integration/route")
    async def integration_route(req: HooksRouteRequest) -> Dict[str, Any]:
        agent_list = (
            [a.strip() for a in req.agents.split(",") if a.strip()]
            if req.agents
            else None
        )
        return await _orchestrator.hooks_route(task=req.task, agents=agent_list)

    @app.post("/api/v1/integration/init")
    async def integration_init(req: HooksInitRequest) -> Dict[str, Any]:
        return await _orchestrator.hooks_init(project_path=req.project_path)

    @app.post("/api/v1/integration/pretrain")
    async def integration_pretrain(req: HooksPretrainRequest) -> Dict[str, Any]:
        return await _orchestrator.hooks_pretrain(repo_path=req.repo_path)

    @app.get("/api/v1/integration/verify")
    async def integration_verify() -> Dict[str, Any]:
        return await _orchestrator.hooks_verify()

    @app.get("/api/v1/integration/doctor")
    async def integration_doctor() -> Dict[str, Any]:
        return await _orchestrator.hooks_doctor()

    @app.post("/api/v1/integration/export")
    async def integration_export(req: HooksExportRequest) -> Dict[str, Any]:
        return await _orchestrator.hooks_export(format=req.format)

    @app.get("/api/v1/integration/build-agents")
    async def integration_build_agents() -> Dict[str, Any]:
        return await _orchestrator.hooks_build_agents()
