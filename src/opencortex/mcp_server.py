# SPDX-License-Identifier: Apache-2.0
"""
MCP Server for OpenCortex.

Exposes MemoryOrchestrator capabilities as MCP tools for external AI agents.
Uses PrefectHQ FastMCP (v3) for tool registration with stdio/SSE/HTTP transport.

Two operational modes (controlled by ``mcp_mode`` in config):

* **remote** (default) — Thin client that forwards all requests to the
  OpenCortex HTTP Server (FastAPI) via :class:`OpenCortexClient`.
* **local** — Embeds the MemoryOrchestrator in-process (development/testing).

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
from typing import Any, Dict, List, Optional

from fastmcp import Context, FastMCP

from opencortex.config import get_config, init_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan: dual-mode initialization
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(server: FastMCP):
    """Initialize orchestrator (local) or HTTP client (remote)."""
    config = get_config()

    if config.mcp_mode == "local":
        # Development mode: in-process orchestrator
        from opencortex.orchestrator import MemoryOrchestrator

        orch = MemoryOrchestrator(config=config)
        await orch.init()
        logger.info(
            "[MCP] Local mode — orchestrator initialized (tenant=%s, user=%s)",
            config.tenant_id,
            config.user_id,
        )
        try:
            yield {"orchestrator": orch, "client": None}
        finally:
            await orch.close()
            logger.info("[MCP] Orchestrator closed")
    else:
        # Production mode: thin HTTP client
        from opencortex.http.client import OpenCortexClient

        url = f"http://{config.http_server_host}:{config.http_server_port}"
        client = OpenCortexClient(base_url=url)
        await client.connect()
        logger.info("[MCP] Remote mode — connected to %s", url)
        try:
            yield {"orchestrator": None, "client": client}
        finally:
            await client.close()
            logger.info("[MCP] HTTP client closed")


# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="opencortex",
    instructions=(
        "OpenCortex Memory Server. "
        "Store, search, and manage AI agent memories with reinforcement learning."
    ),
    lifespan=_lifespan,
)


def _get_client(ctx: Context):
    """Extract the HTTP client from lifespan context."""
    client = ctx.lifespan_context.get("client")
    if client is None:
        raise RuntimeError("No HTTP client available — is mcp_mode set to 'remote'?")
    return client


def _get_orch(ctx: Context):
    """Extract the orchestrator from lifespan context (local mode only)."""
    return ctx.lifespan_context["orchestrator"]


def _is_local(ctx: Context) -> bool:
    """Return True if running in local mode."""
    return ctx.lifespan_context.get("orchestrator") is not None


# ---------------------------------------------------------------------------
# Tools — Core Memory
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
    """Store a new context in the memory system."""
    if _is_local(ctx):
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
    return await _get_client(ctx).memory_store(
        abstract=abstract, content=content, category=category,
        context_type=context_type, uri=uri, meta=meta,
    )


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
    """Search for relevant contexts."""
    if _is_local(ctx):
        from opencortex.retrieve.types import ContextType

        orch = _get_orch(ctx)
        ct = ContextType(context_type) if context_type else None
        metadata_filter = None
        if category:
            metadata_filter = {"op": "must", "field": "category", "conds": [category]}
        result = await orch.search(
            query=query, limit=limit, context_type=ct,
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
        return {"results": items, "total": result.total}
    return await _get_client(ctx).memory_search(
        query=query, limit=limit, context_type=context_type, category=category,
    )


@mcp.tool(
    name="memory_feedback",
    description=(
        "Submit reward feedback for a memory (reinforcement learning). "
        "Positive rewards reinforce retrieval; negative rewards penalize it."
    ),
)
async def memory_feedback(
    uri: str,
    reward: float,
    ctx: Context,
) -> Dict[str, str]:
    """Submit reward signal for a context."""
    if _is_local(ctx):
        orch = _get_orch(ctx)
        await orch.feedback(uri=uri, reward=reward)
        return {"status": "ok", "uri": uri, "reward": str(reward)}
    return await _get_client(ctx).memory_feedback(uri=uri, reward=reward)


@mcp.tool(
    name="memory_stats",
    description="Get system statistics including storage info, tenant, and component status.",
)
async def memory_stats(ctx: Context) -> Dict[str, Any]:
    """Return orchestrator statistics."""
    if _is_local(ctx):
        return await _get_orch(ctx).stats()
    return await _get_client(ctx).memory_stats()


@mcp.tool(
    name="memory_decay",
    description=(
        "Trigger time-decay across all stored memories. "
        "Reduces effective scores of inactive memories over time."
    ),
)
async def memory_decay(ctx: Context) -> Dict[str, Any]:
    """Trigger time-decay across all records."""
    if _is_local(ctx):
        result = await _get_orch(ctx).decay()
        return result or {}
    return await _get_client(ctx).memory_decay()


@mcp.tool(
    name="memory_health",
    description="Check health status of all OpenCortex components.",
)
async def memory_health(ctx: Context) -> Dict[str, Any]:
    """Check health of all components."""
    if _is_local(ctx):
        return await _get_orch(ctx).health_check()
    return await _get_client(ctx).memory_health()


# ---------------------------------------------------------------------------
# Tools — Hooks Learn
# ---------------------------------------------------------------------------

@mcp.tool(
    name="memory_hooks_learn",
    description=(
        "Record a learning outcome using native Q-learning. "
        "Maps OpenCortex concepts to hooks: state=URI, action=context_type, reward=feedback. "
        "Returns best action recommendation based on learned patterns."
    ),
)
async def memory_hooks_learn(
    state: str,
    action: str,
    reward: float,
    available_actions: str = "",
    ctx: Context = None,
) -> Dict[str, Any]:
    """Record a learning outcome via hooks Q-learning."""
    if _is_local(ctx):
        orch = _get_orch(ctx)
        actions = available_actions.split(",") if available_actions else None
        return await orch.hooks_learn(
            state=state, action=action, reward=reward,
            available_actions=actions,
        )
    return await _get_client(ctx).hooks_learn(
        state=state, action=action, reward=reward,
        available_actions=available_actions,
    )


@mcp.tool(
    name="memory_hooks_remember",
    description=(
        "Store content in semantic memory. "
        "Useful for remembering important context that should persist beyond session."
    ),
)
async def memory_hooks_remember(
    content: str,
    memory_type: str = "general",
    ctx: Context = None,
) -> Dict[str, Any]:
    """Store content in semantic memory."""
    if _is_local(ctx):
        return await _get_orch(ctx).hooks_remember(content=content, memory_type=memory_type)
    return await _get_client(ctx).hooks_remember(content=content, memory_type=memory_type)


@mcp.tool(
    name="memory_hooks_recall",
    description=(
        "Search semantic memory for relevant content. "
        "Different from vector search - searches learned patterns and memories."
    ),
)
async def memory_hooks_recall(
    query: str,
    limit: int = 5,
    ctx: Context = None,
) -> List[Dict[str, Any]]:
    """Search semantic memory."""
    if _is_local(ctx):
        return await _get_orch(ctx).hooks_recall(query=query, limit=limit)
    return await _get_client(ctx).hooks_recall(query=query, limit=limit)


@mcp.tool(
    name="memory_hooks_stats",
    description="Get hooks intelligence statistics (Q-learning patterns, memories, trajectories, errors).",
)
async def memory_hooks_stats(ctx: Context) -> Dict[str, Any]:
    """Get hooks statistics."""
    if _is_local(ctx):
        return await _get_orch(ctx).hooks_stats()
    return await _get_client(ctx).hooks_stats()


# ---------------------------------------------------------------------------
# Tools — Trajectory
# ---------------------------------------------------------------------------

@mcp.tool(
    name="memory_hooks_trajectory_begin",
    description="Begin tracking a learning trajectory for multi-step tasks.",
)
async def memory_hooks_trajectory_begin(
    trajectory_id: str,
    initial_state: str,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Begin a learning trajectory."""
    if _is_local(ctx):
        return await _get_orch(ctx).hooks_trajectory_begin(
            trajectory_id=trajectory_id, initial_state=initial_state,
        )
    return await _get_client(ctx).trajectory_begin(
        trajectory_id=trajectory_id, initial_state=initial_state,
    )


@mcp.tool(
    name="memory_hooks_trajectory_step",
    description="Add a step to an existing learning trajectory.",
)
async def memory_hooks_trajectory_step(
    trajectory_id: str,
    action: str,
    reward: float,
    next_state: str = "",
    ctx: Context = None,
) -> Dict[str, Any]:
    """Add a step to trajectory."""
    if _is_local(ctx):
        return await _get_orch(ctx).hooks_trajectory_step(
            trajectory_id=trajectory_id, action=action,
            reward=reward, next_state=next_state or None,
        )
    return await _get_client(ctx).trajectory_step(
        trajectory_id=trajectory_id, action=action,
        reward=reward, next_state=next_state,
    )


@mcp.tool(
    name="memory_hooks_trajectory_end",
    description="End a learning trajectory with a quality score.",
)
async def memory_hooks_trajectory_end(
    trajectory_id: str,
    quality_score: float,
    ctx: Context = None,
) -> Dict[str, Any]:
    """End a trajectory."""
    if _is_local(ctx):
        return await _get_orch(ctx).hooks_trajectory_end(
            trajectory_id=trajectory_id, quality_score=quality_score,
        )
    return await _get_client(ctx).trajectory_end(
        trajectory_id=trajectory_id, quality_score=quality_score,
    )


# ---------------------------------------------------------------------------
# Tools — Error
# ---------------------------------------------------------------------------

@mcp.tool(
    name="memory_hooks_error_record",
    description=(
        "Record an error and its fix for the system to learn from. "
        "Helps the system remember how to fix common errors."
    ),
)
async def memory_hooks_error_record(
    error: str,
    fix: str,
    context: str = "",
    ctx: Context = None,
) -> Dict[str, Any]:
    """Record an error and fix for learning."""
    if _is_local(ctx):
        return await _get_orch(ctx).hooks_error_record(
            error=error, fix=fix, context=context or None,
        )
    return await _get_client(ctx).error_record(
        error=error, fix=fix, context=context,
    )


@mcp.tool(
    name="memory_hooks_error_suggest",
    description=(
        "Get suggested fixes for an error based on learned patterns. "
        "The system will recommend fixes based on previously recorded errors."
    ),
)
async def memory_hooks_error_suggest(
    error: str,
    ctx: Context = None,
) -> List[Dict[str, Any]]:
    """Get suggested fixes for an error."""
    if _is_local(ctx):
        return await _get_orch(ctx).hooks_error_suggest(error=error)
    return await _get_client(ctx).error_suggest(error=error)


# ---------------------------------------------------------------------------
# Tools — Session Management
# ---------------------------------------------------------------------------

@mcp.tool(
    name="session_begin",
    description=(
        "Begin a new session for context self-iteration. "
        "The session will buffer messages and extract persistent memories on end."
    ),
)
async def session_begin(
    session_id: str,
    ctx: Context,
) -> Dict[str, Any]:
    """Begin a new session."""
    if _is_local(ctx):
        return await _get_orch(ctx).session_begin(session_id=session_id)
    return await _get_client(ctx).session_begin(session_id=session_id)


@mcp.tool(
    name="session_message",
    description=(
        "Add a message to an active session. "
        "Messages are buffered for memory extraction when the session ends."
    ),
)
async def session_message(
    session_id: str,
    role: str,
    content: str,
    ctx: Context,
) -> Dict[str, Any]:
    """Add a message to a session."""
    if _is_local(ctx):
        return await _get_orch(ctx).session_message(
            session_id=session_id, role=role, content=content,
        )
    return await _get_client(ctx).session_message(
        session_id=session_id, role=role, content=content,
    )


@mcp.tool(
    name="session_end",
    description=(
        "End a session and trigger memory extraction. "
        "The system will analyze the conversation and automatically extract "
        "persistent memories (preferences, patterns, skills, errors)."
    ),
)
async def session_end(
    session_id: str,
    quality_score: float = 0.5,
    ctx: Context = None,
) -> Dict[str, Any]:
    """End a session and extract memories."""
    if _is_local(ctx):
        return await _get_orch(ctx).session_end(
            session_id=session_id, quality_score=quality_score,
        )
    return await _get_client(ctx).session_end(
        session_id=session_id, quality_score=quality_score,
    )


# ---------------------------------------------------------------------------
# Tools — Hooks Integration
# ---------------------------------------------------------------------------

@mcp.tool(
    name="hooks_route",
    description=(
        "Route a task to the best agent based on learned patterns. "
        "Returns the recommended agent and reasoning."
    ),
)
async def hooks_route(
    task: str,
    agents: str = "",
    ctx: Context = None,
) -> Dict[str, Any]:
    """Route task to best agent."""
    if _is_local(ctx):
        orch = _get_orch(ctx)
        agent_list = [a.strip() for a in agents.split(",") if a.strip()] if agents else None
        return await orch.hooks_route(task=task, agents=agent_list)
    return await _get_client(ctx).integration_route(task=task, agents=agents)


@mcp.tool(
    name="hooks_init",
    description="Initialize OpenCortex hooks configuration for a project.",
)
async def hooks_init(
    project_path: str = ".",
    ctx: Context = None,
) -> Dict[str, Any]:
    """Initialize hooks for a project."""
    if _is_local(ctx):
        return await _get_orch(ctx).hooks_init(project_path=project_path)
    return await _get_client(ctx).integration_init(project_path=project_path)


@mcp.tool(
    name="hooks_pretrain",
    description="Pre-train OpenCortex from repository content (files, patterns, structure).",
)
async def hooks_pretrain(
    repo_path: str = ".",
    ctx: Context = None,
) -> Dict[str, Any]:
    """Pre-train from repository."""
    if _is_local(ctx):
        return await _get_orch(ctx).hooks_pretrain(repo_path=repo_path)
    return await _get_client(ctx).integration_pretrain(repo_path=repo_path)


@mcp.tool(
    name="hooks_verify",
    description="Verify OpenCortex hooks configuration is correct and functional.",
)
async def hooks_verify(ctx: Context = None) -> Dict[str, Any]:
    """Verify hooks configuration."""
    if _is_local(ctx):
        return await _get_orch(ctx).hooks_verify()
    return await _get_client(ctx).integration_verify()


@mcp.tool(
    name="hooks_doctor",
    description="Diagnose OpenCortex system health, configuration issues, and connectivity.",
)
async def hooks_doctor(ctx: Context = None) -> Dict[str, Any]:
    """Run diagnostics."""
    if _is_local(ctx):
        return await _get_orch(ctx).hooks_doctor()
    return await _get_client(ctx).integration_doctor()


@mcp.tool(
    name="hooks_export",
    description="Export OpenCortex intelligence data (learned patterns, memories, trajectories).",
)
async def hooks_export(
    format: str = "json",
    ctx: Context = None,
) -> Dict[str, Any]:
    """Export intelligence data."""
    if _is_local(ctx):
        return await _get_orch(ctx).hooks_export(format=format)
    return await _get_client(ctx).integration_export(format=format)


@mcp.tool(
    name="hooks_build_agents",
    description="Generate agent configuration based on learned patterns and project structure.",
)
async def hooks_build_agents(ctx: Context = None) -> Dict[str, Any]:
    """Generate agent configurations."""
    if _is_local(ctx):
        return await _get_orch(ctx).hooks_build_agents()
    return await _get_client(ctx).integration_build_agents()


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
        default="streamable-http",
        help="Transport mode (default: streamable-http)",
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
        "--mode",
        choices=["local", "remote"],
        default=None,
        help="Override mcp_mode from config (local=in-process, remote=HTTP client)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        default=True,
        help="Enable stateless HTTP mode (default: True)",
    )
    parser.add_argument(
        "--json-response",
        action="store_true",
        default=True,
        help="Return JSON responses instead of SSE streams (default: True)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Initialize config
    config = init_config(path=args.config)

    # CLI --mode overrides config file
    if args.mode:
        config.mcp_mode = args.mode

    run_kwargs = {"transport": args.transport}
    if args.transport != "stdio":
        run_kwargs["host"] = args.host
        run_kwargs["port"] = args.port
    if args.stateless and args.transport == "streamable-http":
        run_kwargs["stateless_http"] = True
    if args.json_response and args.transport == "streamable-http":
        run_kwargs["json_response"] = True
    mcp.run(**run_kwargs)


if __name__ == "__main__":
    main()
