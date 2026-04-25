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

from opencortex.auth.token import (
    generate_token, load_token_records, revoke_token, save_token_record,
)
from opencortex.context.manager import SourceConflictError
from opencortex.context.session_records import SessionRecordOverflowError
from opencortex.http.models import (
    BenchmarkConversationIngestRequest, BenchmarkConversationIngestResponse,
    CreateTokenRequest, MemorySearchRequest, RevokeTokenRequest,
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
    # Plan 009 — use the orchestrator's RerankClient singleton instead
    # of constructing a fresh client per request. The previous shape
    # leaked one TCP socket per admin_search_debug call (no aclose path
    # on RerankClient before this PR). Lazy-built so non-admin code
    # paths don't pay the ``_init_local_reranker`` cold-start cost.
    rerank_cfg = _orchestrator._build_rerank_config()
    if rerank_cfg.is_available():
        rerank_client = _orchestrator._get_or_create_rerank_client()
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
        # REVIEW api-contract-004: standardize on FastAPI's
        # ``{"detail": ...}`` envelope across every admin route. The
        # prior ``JSONResponse({"error": ...})`` was the only third
        # style in this file.
        raise HTTPException(
            status_code=400,
            detail="Collection name must start with bench_",
        )
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
        raise HTTPException(
            status_code=400,
            detail="Can only delete bench_ collections",
        )
    await _orchestrator._storage.drop_collection(name)
    return {"status": "deleted", "collection": name}


# =========================================================================
# Admin — Benchmark (admin-only, benchmark infrastructure)
# =========================================================================

@router.post(
    "/api/v1/admin/benchmark/conversation_ingest",
    response_model=BenchmarkConversationIngestResponse,
)
async def admin_benchmark_conversation_ingest(
    req: BenchmarkConversationIngestRequest,
) -> BenchmarkConversationIngestResponse:
    """Benchmark-only offline conversation ingest.

    This is benchmark infrastructure: it triggers per-leaf embeds, full-session
    recomposition, and (optionally) a session summary LLM call. A single
    request can fan out to dozens of LLM calls and run for many seconds, so
    it is admin-gated and wrapped in a server-side timeout that sits ~10%
    under the client timeout to ensure the in-process cleanup tracker runs
    before the client disconnects.

    §25 Phase 6 / U5: response is validated through
    ``BenchmarkConversationIngestResponse`` so any drift in the dict
    shape returned by ``BenchmarkConversationIngestService`` surfaces
    here rather than at adapter parse time.
    """
    _require_admin()
    tid, uid = get_effective_identity()
    try:
        result = await asyncio.wait_for(
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
        return BenchmarkConversationIngestResponse.model_validate(result)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(
                "Benchmark ingest exceeded server timeout "
                f"({_BENCHMARK_INGEST_TIMEOUT_SECONDS:.0f}s); "
                "in-process cleanup ran"
            ),
        )
    except SourceConflictError as exc:
        # Same session_id, different transcript. Caller must rotate
        # session_id intentionally rather than silently overwrite a prior
        # run's source — see U5 in REVIEW Section 26.
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "transcript_hash_mismatch",
                "session_id": req.session_id,
                "existing_hash": exc.existing_hash,
                "supplied_hash": exc.supplied_hash,
            },
        )
    except SessionRecordOverflowError as exc:
        # SessionRecordsRepository safety stop fired — almost certainly a
        # session_id payload anomaly or cross-tenant collision in
        # storage. Surface 507 with the cursor + count so an operator
        # can resume the scroll manually if they actually need the full
        # set. (REVIEW closure tracker U2.)
        raise HTTPException(
            status_code=507,
            detail={
                "reason": "session_record_overflow",
                "session_id": exc.session_id,
                "method": exc.method,
                "count_at_stop": exc.count_at_stop,
                "next_cursor": exc.next_cursor,
                "hint": (
                    "Rotate session_id, audit the storage payload for "
                    "cross-tenant collision, or page manually from "
                    "next_cursor."
                ),
            },
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


# =========================================================================
# Health (admin only) — connection pool visibility
# =========================================================================

# Threshold (open/max ratio) above which a single client's pool is
# considered "approaching the cap" and the top-level status drops to
# "degraded". 0.8 = WARN at 80% utilization. Tunable, but matches the
# sweeper's WARNING threshold (U5) so operator alerts stay consistent.
_POOL_DEGRADED_THRESHOLD = 0.8


def _extract_pool_stats(client: Any) -> Dict[str, Any]:
    """Best-effort read of httpx pool internals.

    httpx exposes pool counts via ``client._transport._pool``, which is
    a private API. Wrap in try/except so a future httpx version bump
    does not crash the health endpoint — operators still see "this
    client exists with these limits" even when live counts are
    unavailable.

    Returns a dict shaped like::

        {
            "stats_source": "transport_pool" | "unavailable",
            "open_connections": int | None,
            "keepalive_connections": int | None,
            "limits": {"max_connections": int, "max_keepalive_connections": int} | None,
            "reason": str  # only when stats_source == "unavailable"
        }
    """
    out: Dict[str, Any] = {
        "stats_source": "unavailable",
        "open_connections": None,
        "keepalive_connections": None,
        "limits": None,
    }
    if client is None:
        out["reason"] = "client is None"
        return out
    # Limits are a public-ish init kwarg held internally as ``_limits``
    # — not strictly public but stable across httpx 0.27.x. Read first
    # because it's cheaper than the pool walk.
    try:
        limits = getattr(client, "_limits", None)
        if limits is not None:
            out["limits"] = {
                "max_connections": getattr(limits, "max_connections", None),
                "max_keepalive_connections": getattr(
                    limits, "max_keepalive_connections", None,
                ),
            }
    except Exception:
        pass
    try:
        transport = getattr(client, "_transport", None)
        pool = getattr(transport, "_pool", None) if transport is not None else None
        if pool is None:
            out["reason"] = "transport pool not exposed"
            return out
        # httpx 0.27+ ``ConnectionPool`` exposes ``connections`` (a list
        # of HTTPConnection-like objects, each with ``is_idle()`` /
        # ``is_available()``). Earlier shapes vary — this is the
        # private boundary the docstring warns about.
        connections = list(getattr(pool, "connections", []) or [])
        out["open_connections"] = len(connections)
        # Best-effort keepalive count: connections that report idle.
        keepalive = 0
        for conn in connections:
            try:
                if hasattr(conn, "is_idle") and conn.is_idle():
                    keepalive += 1
            except Exception:
                pass
        out["keepalive_connections"] = keepalive
        out["stats_source"] = "transport_pool"
    except Exception as exc:
        out["reason"] = f"pool read failed: {exc}"
    return out


def _classify_pool_status(stats: Dict[str, Any]) -> str:
    """Return one of ``"healthy"`` / ``"degraded"`` / ``"unavailable"``.

    Single-client classification — the top-level endpoint folds the
    per-client values into the worst case across all clients.
    """
    if stats.get("stats_source") != "transport_pool":
        return "unavailable"
    open_count = stats.get("open_connections")
    limits = stats.get("limits") or {}
    max_conn = limits.get("max_connections")
    if not isinstance(open_count, int) or not isinstance(max_conn, int) or max_conn <= 0:
        return "unavailable"
    if open_count > _POOL_DEGRADED_THRESHOLD * max_conn:
        return "degraded"
    return "healthy"


@router.get("/api/v1/admin/health/connections")
async def admin_health_connections() -> Dict[str, Any]:
    """Report pooled-httpx-client state for ops visibility (plan 009 / R4).

    The previous incident's first warning sign would have been a steady
    rise in ``open_connections`` for one of the LLM clients. This
    endpoint gives operators a poll target so they see the rise before
    the event loop blocks. Also exposes the configured limits and the
    sweeper's last-run timestamp (when U5 lands).

    Auth: admin only.

    Best-effort: ``stats_source`` indicates whether live pool counts
    were readable. The endpoint never raises 5xx for a stat-extraction
    failure — it returns ``"unavailable"`` with a reason instead.
    """
    _require_admin()

    clients_report: Dict[str, Dict[str, Any]] = {}

    # LLM completion: orchestrator holds an LLMCompletion wrapper that
    # exposes ``.client`` (the underlying httpx.AsyncClient). Bare
    # callables (legacy / test injections) have no ``.client`` so we
    # report "uninitialized" rather than crash.
    llm_completion = getattr(_orchestrator, "_llm_completion", None)
    llm_client = getattr(llm_completion, "client", None) if llm_completion else None
    if llm_client is None:
        clients_report["llm_completion"] = {
            "stats_source": "uninitialized",
            "reason": "no LLM completion wrapper held by orchestrator",
        }
    else:
        clients_report["llm_completion"] = _extract_pool_stats(llm_client)
        clients_report["llm_completion"]["backend"] = getattr(
            llm_completion, "backend", "unknown",
        )

    # Rerank: lazy singleton; ``_http_client`` is None until first call.
    rerank_client_singleton = getattr(_orchestrator, "_rerank_client", None)
    if rerank_client_singleton is None:
        clients_report["rerank"] = {
            "stats_source": "uninitialized",
            "reason": "RerankClient singleton not built yet (no rerank request "
            "has fired since process start)",
        }
    else:
        rerank_inner = getattr(rerank_client_singleton, "_http_client", None)
        if rerank_inner is None:
            clients_report["rerank"] = {
                "stats_source": "uninitialized",
                "reason": "RerankClient exists but no API call has built the "
                "lazy http client yet",
            }
        else:
            clients_report["rerank"] = _extract_pool_stats(rerank_inner)

    # Top-level status: worst case across clients. "uninitialized" does
    # not lower the status (no pool to leak) — only "degraded" matters.
    statuses = {_classify_pool_status(s) for s in clients_report.values()}
    if "degraded" in statuses:
        top_status = "degraded"
    elif "healthy" in statuses:
        top_status = "healthy"
    else:
        top_status = "unavailable"

    # Sweeper status — populated by U5. Until then, report "not_started".
    sweeper = {
        "last_sweep_at": getattr(_orchestrator, "_last_connection_sweep_at", None),
        "last_sweep_status": getattr(
            _orchestrator, "_last_connection_sweep_status", "not_started",
        ),
        "interval_seconds": getattr(
            getattr(_orchestrator, "_config", None),
            "connection_sweep_interval_seconds",
            None,
        ),
    }
    # ISO-format the timestamp if datetime; leave others as-is.
    if hasattr(sweeper["last_sweep_at"], "isoformat"):
        sweeper["last_sweep_at"] = sweeper["last_sweep_at"].isoformat()

    return {
        "status": top_status,
        "clients": clients_report,
        "sweeper": sweeper,
    }
