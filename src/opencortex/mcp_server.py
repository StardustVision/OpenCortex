# SPDX-License-Identifier: Apache-2.0
"""
MCP Server for OpenCortex.

Exposes MemoryOrchestrator capabilities as MCP tools for external AI agents.
Uses PrefectHQ FastMCP (v3) for tool registration with stdio/SSE/HTTP transport.

Usage::

    # stdio mode (local agent)
    python -m opencortex.mcp_server

    # SSE mode (remote agent)
    python -m opencortex.mcp_server --transport sse --port 8920

    # streamable-http mode
    python -m opencortex.mcp_server --transport streamable-http --port 8920
"""

import argparse
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastmcp import Context, FastMCP

from opencortex.config import get_config, init_config
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.retrieve.types import ContextType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan: initialize orchestrator once at startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(server: FastMCP):
    """Initialize and teardown the MemoryOrchestrator."""
    config = get_config()
    orch = MemoryOrchestrator(config=config)
    await orch.init()
    logger.info("[MCP] Orchestrator initialized (tenant=%s, user=%s)",
                config.tenant_id, config.user_id)
    try:
        yield {"orchestrator": orch}
    finally:
        await orch.close()
        logger.info("[MCP] Orchestrator closed")


# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="opencortex",
    instructions=(
        "OpenCortex Memory Server. "
        "Store, search, and manage AI agent memories with SONA reinforcement learning."
    ),
    lifespan=_lifespan,
)


def _get_orch(ctx: Context) -> MemoryOrchestrator:
    """Extract orchestrator from the MCP lifespan context."""
    return ctx.lifespan_context["orchestrator"]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="memory_store",
    description=(
        "Store a new memory, resource, or skill. "
        "Returns the URI and metadata of the stored context."
    ),
)
async def memory_store(
    abstract: str,
    ctx: Context,
    content: str = "",
    category: str = "",
    context_type: str = "memory",
    uri: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Store a new context in the memory system.

    Args:
        abstract: Short summary of the memory (used for vector search).
        content: Full content (stored on filesystem as L2).
        category: Category hint (e.g. "preferences", "entities", "patterns").
        context_type: Type of context: "memory", "resource", or "skill".
        uri: Explicit URI. Auto-generated if not provided.
        meta: Additional metadata dict.
    """
    orch = _get_orch(ctx)
    result = await orch.add(
        abstract=abstract,
        content=content,
        category=category,
        context_type=context_type,
        uri=uri,
        meta=meta,
    )
    return {
        "uri": result.uri,
        "context_type": result.context_type,
        "category": result.category,
        "abstract": result.abstract,
    }


@mcp.tool(
    name="memory_search",
    description=(
        "Semantic search across stored memories, resources, and skills. "
        "Returns ranked results with relevance scores."
    ),
)
async def memory_search(
    query: str,
    ctx: Context,
    limit: int = 5,
    context_type: Optional[str] = None,
    category: Optional[str] = None,
) -> Dict[str, Any]:
    """Search for relevant contexts.

    Args:
        query: Natural language search query.
        limit: Maximum number of results (default 5).
        context_type: Restrict to "memory", "resource", or "skill".
        category: Filter by category.
    """
    orch = _get_orch(ctx)

    ct = None
    if context_type:
        ct = ContextType(context_type)

    metadata_filter = None
    if category:
        metadata_filter = {"op": "must", "field": "category", "conds": [category]}

    result = await orch.search(
        query=query,
        limit=limit,
        context_type=ct,
        metadata_filter=metadata_filter,
    )

    items = []
    for matched in result:
        items.append({
            "uri": matched.uri,
            "abstract": matched.abstract,
            "context_type": str(matched.context_type),
            "score": getattr(matched, "score", None),
        })

    return {
        "results": items,
        "total": result.total,
    }


@mcp.tool(
    name="memory_feedback",
    description=(
        "Submit reward feedback for a memory (SONA reinforcement). "
        "Positive rewards reinforce retrieval; negative rewards penalize it."
    ),
)
async def memory_feedback(
    uri: str,
    reward: float,
    ctx: Context,
) -> Dict[str, str]:
    """Submit reward signal for a context.

    Args:
        uri: URI of the context to reward.
        reward: Scalar reward value (positive = good, negative = bad).
    """
    orch = _get_orch(ctx)
    await orch.feedback(uri=uri, reward=reward)
    return {"status": "ok", "uri": uri, "reward": str(reward)}


@mcp.tool(
    name="memory_stats",
    description="Get system statistics including storage info, tenant, and component status.",
)
async def memory_stats(ctx: Context) -> Dict[str, Any]:
    """Return orchestrator statistics."""
    orch = _get_orch(ctx)
    return await orch.stats()


@mcp.tool(
    name="memory_decay",
    description=(
        "Trigger time-decay across all stored memories (SONA). "
        "Reduces effective scores of inactive memories over time."
    ),
)
async def memory_decay(ctx: Context) -> Dict[str, Any]:
    """Trigger time-decay across all records."""
    orch = _get_orch(ctx)
    result = await orch.decay()
    return result or {}


@mcp.tool(
    name="memory_health",
    description="Check health status of all OpenCortex components.",
)
async def memory_health(ctx: Context) -> Dict[str, Any]:
    """Check health of all components."""
    orch = _get_orch(ctx)
    return await orch.health_check()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="opencortex.mcp_server",
        description="OpenCortex MCP Server",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8920,
        help="Port for SSE/HTTP transport (default: 8920)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for SSE/HTTP transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to opencortex.json config file",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Initialize config
    init_config(path=args.config)

    mcp.run(
        transport=args.transport,
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
