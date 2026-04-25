# SPDX-License-Identifier: Apache-2.0
"""
Admin and auth API routes for OpenCortex.

Isolated from business routes (server.py). All /api/v1/admin/* endpoints
require admin role JWT. /api/v1/auth/* endpoints are open to all
authenticated users.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from opencortex.auth.token import (
    generate_token, load_token_records, revoke_token, save_token_record,
)
from opencortex.http.models import (
    BenchmarkConversationIngestRequest, CreateTokenRequest,
    MemorySearchRequest, RevokeTokenRequest,
)
from opencortex.http.request_context import (
    get_effective_identity, get_effective_role, is_admin,
)

# Server-side ceiling, ~10% under the client 600s timeout in oc_client.py.
# Cleanup tracker (U4) compensates on TimeoutError via CancelledError handler (U3).
_BENCHMARK_INGEST_TIMEOUT_SECONDS = 540.0

logger = logging.getLogger(__name__)

router = APIRouter()

# Set by register_admin_routes() — avoids circular imports
_orchestrator = None
_jwt_secret = None


def register_admin_routes(orchestrator, jwt_secret: str) -> None:
    """Bind orchestrator and secret so route handlers can use them."""
    global _orchestrator, _jwt_secret
    _orchestrator = orchestrator
    _jwt_secret = jwt_secret


def _require_admin() -> None:
    """Raise 403 if the current request is not from an admin token."""
    if not is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")


# =========================================================================
# Auth (any authenticated user)
# =========================================================================

@router.get("/api/v1/auth/me")
async def auth_me() -> Dict[str, Any]:
    """Return current user identity and role."""
    tid, uid = get_effective_identity()
    return {"tenant_id": tid, "user_id": uid, "role": get_effective_role()}


# =========================================================================
# Admin — Token Management
# =========================================================================

@router.get("/api/v1/admin/tokens")
async def admin_list_tokens() -> Dict[str, Any]:
    """List all token records (token truncated to prefix)."""
    _require_admin()
    records = load_token_records(_orchestrator.config.data_root)
    return {"tokens": [
        {
            "tenant_id": r["tenant_id"],
            "user_id": r["user_id"],
            "role": r.get("role", "user"),
            "created_at": r.get("created_at", ""),
            "token_prefix": r["token"][:20] + "...",
            "token": r["token"],
        }
        for r in records
    ]}


@router.post("/api/v1/admin/tokens")
async def admin_create_token(req: CreateTokenRequest) -> Dict[str, Any]:
    """Create a new user token."""
    _require_admin()
    token = generate_token(req.tenant_id, req.user_id, _jwt_secret)
    save_token_record(_orchestrator.config.data_root, token, req.tenant_id, req.user_id)
    return {"token": token, "tenant_id": req.tenant_id, "user_id": req.user_id, "role": "user"}


@router.delete("/api/v1/admin/tokens")
async def admin_revoke_token(req: RevokeTokenRequest) -> Dict[str, Any]:
    """Revoke a token by prefix. Cannot revoke admin tokens."""
    _require_admin()
    records = load_token_records(_orchestrator.config.data_root)
    target = next((r for r in records if r["token"].startswith(req.token_prefix)), None)
    if not target:
        raise HTTPException(status_code=404, detail="Token not found")
    if target.get("role") == "admin":
        raise HTTPException(status_code=400, detail="Cannot revoke admin token")
    removed = revoke_token(_orchestrator.config.data_root, req.token_prefix)
    return {"status": "ok", "revoked": {"tenant_id": removed["tenant_id"], "user_id": removed["user_id"]}}


# =========================================================================
# Admin — Memory Listing (cross-tenant)
# =========================================================================

@router.get("/api/v1/admin/memories")
async def admin_list_memories(
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    category: Optional[str] = None,
    context_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """List memories across all users (admin only)."""
    _require_admin()
    items = await _orchestrator.list_memories_admin(
        tenant_id=tenant_id, user_id=user_id,
        category=category, context_type=context_type,
        limit=limit, offset=offset,
    )
    return {"results": items, "total": len(items)}


# =========================================================================
# Admin — System Operations (moved from server.py)
# =========================================================================

@router.post("/api/v1/admin/reembed")
async def admin_reembed() -> Dict[str, Any]:
    """Re-embed all records with the current embedding model."""
    _require_admin()
    count = await _orchestrator.reembed_all()
    return {"status": "ok", "updated": count}


@router.post("/api/v1/admin/search_debug")
async def admin_search_debug(req: MemorySearchRequest) -> Dict[str, Any]:
    """Diagnostic: show raw vector scores, rerank scores, and fused scores."""
    _require_admin()
    storage = _orchestrator._storage
    embedder = _orchestrator._embedder

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

    rerank_scores = None
    rerank_cfg = _orchestrator._build_rerank_config()
    if rerank_cfg.is_available():
        from opencortex.retrieve.rerank_client import RerankClient

        rerank_client = RerankClient(
            rerank_cfg,
            llm_completion=_orchestrator._llm_completion,
        )
        docs = [r.get("abstract", "") for r in raw_results]
        rerank_scores = await rerank_client.rerank(req.query, docs)

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
            "abstract": r.get("abstract", ""),
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


@router.post("/api/v1/admin/collection")
async def create_bench_collection(request: Request):
    """Create a benchmark-isolated collection (name must start with bench_)."""
    _require_admin()
    body = await request.json()
    name = body.get("name", "")
    if not name.startswith("bench_"):
        return JSONResponse({"error": "Collection name must start with bench_"}, status_code=400)
    dim = _orchestrator._config.embedding_dimension
    from opencortex.storage.collection_schemas import CollectionSchemas
    schema = CollectionSchemas.context_collection(name, dim)
    await _orchestrator._storage.create_collection(name, schema)
    return {"status": "created", "collection": name}


@router.delete("/api/v1/admin/collection/{name}")
async def delete_bench_collection(name: str):
    """Delete a benchmark-isolated collection (name must start with bench_)."""
    _require_admin()
    if not name.startswith("bench_"):
        return JSONResponse({"error": "Can only delete bench_ collections"}, status_code=400)
    await _orchestrator._storage.drop_collection(name)
    return {"status": "deleted", "collection": name}


# =========================================================================
# Admin — Benchmark (admin-only, benchmark infrastructure)
# =========================================================================

@router.post("/api/v1/admin/benchmark/conversation_ingest")
async def admin_benchmark_conversation_ingest(
    req: BenchmarkConversationIngestRequest,
) -> Dict[str, Any]:
    """Benchmark-only offline conversation ingest.

    This is benchmark infrastructure: it triggers per-leaf embeds, full-session
    recomposition, and (optionally) a session summary LLM call. A single
    request can fan out to dozens of LLM calls and run for many seconds, so
    it is admin-gated and wrapped in a server-side timeout that sits ~10%
    under the client timeout to ensure the in-process cleanup tracker runs
    before the client disconnects.
    """
    _require_admin()
    tid, uid = get_effective_identity()
    try:
        return await asyncio.wait_for(
            _orchestrator.benchmark_conversation_ingest(
                session_id=req.session_id,
                tenant_id=tid,
                user_id=uid,
                segments=[
                    [message.model_dump() for message in segment.messages]
                    for segment in req.segments
                ],
                include_session_summary=req.include_session_summary,
                ingest_shape=req.ingest_shape,
            ),
            timeout=_BENCHMARK_INGEST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(
                "Benchmark ingest exceeded server timeout "
                f"({_BENCHMARK_INGEST_TIMEOUT_SECONDS:.0f}s); "
                "in-process cleanup ran"
            ),
        )


# =========================================================================
# Migration (admin only)
# =========================================================================

@router.post("/api/v1/migration/overview-first")
async def migration_overview_first(
    dry_run: bool = False,
    batch: int = 50,
) -> Dict[str, Any]:
    """Run v0.3.2 overview-first migration (re-generate L0/L1 from L2)."""
    _require_admin()
    from opencortex.migration.v032_overview_first import migrate_overview_first
    return await migrate_overview_first(
        _orchestrator, dry_run=dry_run, batch_size=batch,
    )
