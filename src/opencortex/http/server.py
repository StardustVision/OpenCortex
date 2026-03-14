# SPDX-License-Identifier: Apache-2.0
"""
FastAPI HTTP Server for OpenCortex.

Hosts the MemoryOrchestrator and exposes all MCP tool capabilities as REST
endpoints.  This is the primary deployment target — the MCP Server acts as
a thin client that forwards requests here.

Usage::

    python -m opencortex.http --host 127.0.0.1 --port 8921 --config server.json
"""

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from opencortex.auth.token import decode_token, ensure_secret
from opencortex.config import get_config
from opencortex.http.request_context import (
    reset_request_identity,
    reset_request_project_id,
    set_request_identity,
    set_request_project_id,
)
from opencortex.http.models import (
    IntentShouldRecallRequest,
    MemoryBatchStoreRequest,
    MemoryFeedbackRequest,
    MemorySearchRequest,
    MemoryStoreRequest,
    PromoteToSharedRequest,
    SessionBeginRequest,
    SessionEndRequest,
    SessionMessageRequest,
    # Cortex Alpha
    SessionMessagesRequest,
    KnowledgeSearchRequest,
    KnowledgeApproveRequest,
    KnowledgeRejectRequest,
    # Context Protocol
    ContextRequest,
)
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.retrieve.intent_router import IntentRouter
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


# ---------------------------------------------------------------------------
# Request Context Middleware
# ---------------------------------------------------------------------------

class RequestContextMiddleware(BaseHTTPMiddleware):
    """Authenticate requests via JWT Bearer token and set per-request identity.

    The ``Authorization: Bearer <token>`` header is required on all paths
    except those in ``_AUTH_WHITELIST``.  Identity (tenant_id, user_id) is
    extracted from the JWT claims (``tid``, ``uid``).
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Whitelisted paths bypass authentication
        if path in _AUTH_WHITELIST:
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

        project_id = request.headers.get("x-project-id", "public")
        project_token = set_request_project_id(project_id)

        try:
            return await call_next(request)
        finally:
            reset_request_identity(id_tokens)
            reset_request_project_id(project_token)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Initialize and teardown the MemoryOrchestrator."""
    global _orchestrator, _jwt_secret
    config = get_config()
    _jwt_secret = ensure_secret(config.data_root)
    _orchestrator = MemoryOrchestrator(config=config)
    await _orchestrator.init()
    logger.info("[HTTP] Orchestrator initialized (data_root=%s)", config.data_root)
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
        version="0.4.2",
        lifespan=_lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
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
        # URI is always auto-generated by backend based on identity + context_type + category.
        # Client-provided uri is ignored to prevent malformed storage paths.
        result = await _orchestrator.add(
            abstract=req.abstract,
            content=req.content,
            overview=req.overview,
            category=req.category,
            context_type=req.context_type,
            meta=req.meta,
            dedup=req.dedup,
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
            if matched.keywords:
                item["keywords"] = matched.keywords
            items.append(item)
        resp: Dict[str, Any] = {"results": items, "total": result.total}
        if result.search_intent:
            resp["search_intent"] = {
                "intent_type": result.search_intent.intent_type,
                "top_k": result.search_intent.top_k,
                "detail_level": result.search_intent.detail_level.value,
                "time_scope": result.search_intent.time_scope,
                "should_recall": result.search_intent.should_recall,
                "lexical_boost": result.search_intent.lexical_boost,
            }
        return resp

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
    ) -> Dict[str, Any]:
        """List user's accessible memories (private + shared)."""
        items = await _orchestrator.list_memories(
            category=category,
            context_type=context_type,
            limit=limit,
            offset=offset,
        )
        return {"results": items, "total": len(items)}

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
    # Intent
    # =====================================================================

    @app.post("/api/v1/intent/should_recall")
    async def intent_should_recall(req: IntentShouldRecallRequest) -> Dict[str, Any]:
        router = IntentRouter(llm_completion=_orchestrator._llm_completion)
        intent = await router.route(req.query)
        return {
            "should_recall": intent.should_recall,
            "intent_type": intent.intent_type,
        }

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
                session_id=req.session_id,
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
            query=req.query, types=req.types, limit=req.limit,
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
        return await _orchestrator._context_manager.handle(
            session_id=req.session_id,
            phase=req.phase,
            tenant_id=tid,
            user_id=uid,
            turn_id=req.turn_id,
            messages=[m.model_dump() for m in req.messages] if req.messages else None,
            cited_uris=req.cited_uris,
            config=req.config.model_dump() if req.config else None,
        )

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
        uri: str, offset: int = 0, limit: int = -1,
    ) -> Dict[str, Any]:
        """Read L2 content from CortexFS."""
        raw = await _orchestrator._fs.read(
            uri + "/content.md", offset=offset, size=limit,
        )
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return {"status": "ok", "result": text}

    # =====================================================================
    # Admin
    # =====================================================================

    @app.post("/api/v1/admin/reembed")
    async def admin_reembed() -> Dict[str, Any]:
        """Re-embed all records with the current embedding model."""
        count = await _orchestrator.reembed_all()
        return {"status": "ok", "updated": count}

    @app.post("/api/v1/admin/search_debug")
    async def admin_search_debug(req: MemorySearchRequest) -> Dict[str, Any]:
        """Diagnostic: show raw vector scores, rerank scores, and fused scores."""
        import asyncio

        storage = _orchestrator._storage
        embedder = _orchestrator._embedder
        retriever = _orchestrator._retriever

        # 1. Raw vector search scores from Qdrant
        loop = asyncio.get_running_loop()
        embed_result = await asyncio.wait_for(
            loop.run_in_executor(None, embedder.embed_query, req.query),
            timeout=2.0,
        )
        raw_results = await storage.search(
            "context",
            query_vector=embed_result.dense_vector,
            sparse_query_vector=embed_result.sparse_vector,
            limit=req.limit or 10,
        )

        # 2. Rerank scores (if available)
        rerank_scores = None
        if retriever._rerank_client:
            docs = [r.get("abstract", "") for r in raw_results]
            rerank_scores = await retriever._rerank_client.rerank(req.query, docs)

        # 3. Build comparison
        rows = []
        beta = retriever._fusion_beta
        for i, r in enumerate(raw_results):
            raw_score = r.get("_score", 0.0)
            rr_score = rerank_scores[i] if rerank_scores else None
            fused = (
                beta * rr_score + (1 - beta) * raw_score
                if rr_score is not None
                else raw_score
            )
            rows.append({
                "rank": i + 1,
                "abstract": r.get("abstract", "")[:80],
                "raw_vector_score": round(raw_score, 5),
                "rerank_score": round(rr_score, 5) if rr_score is not None else None,
                "fused_score": round(fused, 5),
                "uri": r.get("uri", ""),
            })

        return {
            "query": req.query,
            "fusion_beta": beta,
            "rerank_mode": retriever._rerank_client.mode if retriever._rerank_client else "disabled",
            "results": rows,
        }

    # =====================================================================
    # Migration
    # =====================================================================

    @app.post("/api/v1/migration/overview-first")
    async def migration_overview_first(
        dry_run: bool = False,
        batch: int = 50,
    ) -> Dict[str, Any]:
        """Run v0.3.2 overview-first migration (re-generate L0/L1 from L2)."""
        from opencortex.migration.v032_overview_first import migrate_overview_first
        return await migrate_overview_first(
            _orchestrator, dry_run=dry_run, batch_size=batch,
        )
