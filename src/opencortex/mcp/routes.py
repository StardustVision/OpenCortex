# SPDX-License-Identifier: Apache-2.0
"""Streamable HTTP MCP routes for OpenCortex."""

from __future__ import annotations

from contextlib import suppress
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import ValidationError
from starlette import status
from starlette.requests import ClientDisconnect

from opencortex.mcp.schemas import (
    MCP_PROTOCOL_VERSION,
    JsonRpcError,
    JsonRpcErrorCode,
    JsonRpcRequest,
    JsonRpcResponse,
    McpMethod,
    ToolCallParams,
)
from opencortex.mcp.tools import McpToolbox, list_tools
from opencortex.store.dependencies import (
    get_memory_forgetter,
    get_memory_retriever,
    get_memory_store,
    get_resource_store,
    get_session_ender,
    get_session_store,
)
from opencortex.store.forget import MemoryForgetter
from opencortex.store.session.ender import SessionEnder
from opencortex.store.session.store import SessionStore
from opencortex.store.store import MemoryStore, ResourceStore
from opencortex.vector.retrieval import MemoryRetriever

router = APIRouter()


@router.get("/mcp")
async def mcp_get() -> Response:
    """Reject Streamable HTTP SSE sessions until streaming is implemented."""
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED)


@router.delete("/mcp")
async def mcp_delete() -> Response:
    """Acknowledge session termination for stateless MCP operation."""
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED)


@router.post("/mcp", response_model=None)
async def mcp_post(
    request: Request,
    _transport: Annotated[None, Depends(validate_mcp_transport)],
    toolbox: Annotated[McpToolbox, Depends(get_mcp_toolbox)],
) -> Response | dict[str, Any] | list[dict[str, Any]]:
    """Handle MCP JSON-RPC messages over Streamable HTTP."""
    try:
        payload = await request.json()
    except (ClientDisconnect, ValueError) as exc:
        return json_rpc_error_response(
            request_id=None,
            code=JsonRpcErrorCode.PARSE_ERROR,
            message="Invalid JSON body",
            data={"error": str(exc)},
        ).model_dump(mode="json", exclude_none=True, by_alias=True)

    if isinstance(payload, list):
        if not payload:
            return json_rpc_error_response(
                request_id=None,
                code=JsonRpcErrorCode.INVALID_REQUEST,
                message="Batch request must not be empty",
            ).model_dump(mode="json", exclude_none=True, by_alias=True)
        responses = [
            response.model_dump(mode="json", exclude_none=True, by_alias=True)
            for item in payload
            if (response := await handle_json_rpc_message(item, toolbox)) is not None
        ]
        if not responses:
            return Response(status_code=status.HTTP_202_ACCEPTED)
        return responses

    response = await handle_json_rpc_message(payload, toolbox)
    if response is None:
        return Response(status_code=status.HTTP_202_ACCEPTED)
    return response.model_dump(mode="json", exclude_none=True, by_alias=True)


def get_mcp_toolbox(
    retriever: Annotated[MemoryRetriever, Depends(get_memory_retriever)],
    memory_store: Annotated[MemoryStore, Depends(get_memory_store)],
    resource_store: Annotated[ResourceStore, Depends(get_resource_store)],
    forgetter: Annotated[MemoryForgetter, Depends(get_memory_forgetter)],
    session_store: Annotated[SessionStore, Depends(get_session_store)],
    session_ender: Annotated[SessionEnder, Depends(get_session_ender)],
) -> McpToolbox:
    """Return MCP tool dispatcher backed by app runtime dependencies."""
    return McpToolbox(
        retriever=retriever,
        memory_store=memory_store,
        resource_store=resource_store,
        forgetter=forgetter,
        session_store=session_store,
        session_ender=session_ender,
    )


def validate_mcp_transport(
    accept: Annotated[str, Header()] = "",
    content_type: Annotated[str, Header()] = "",
) -> None:
    """Validate Streamable HTTP transport headers before runtime dependencies."""
    validate_streamable_http_headers(accept=accept, content_type=content_type)


async def handle_json_rpc_message(
    payload: Any,
    toolbox: McpToolbox,
) -> JsonRpcResponse | None:
    """Handle one JSON-RPC request or notification."""
    try:
        rpc_request = JsonRpcRequest.model_validate(payload)
    except ValidationError as exc:
        return json_rpc_error_response(
            request_id=payload.get("id") if isinstance(payload, dict) else None,
            code=JsonRpcErrorCode.INVALID_REQUEST,
            message="Invalid JSON-RPC request",
            data={"errors": exc.errors()},
        )

    if rpc_request.id is None:
        with suppress(McpJsonRpcError):
            await handle_notification(rpc_request)
        return None

    try:
        result = await dispatch_request(rpc_request, toolbox)
    except McpJsonRpcError as exc:
        return json_rpc_error_response(
            request_id=rpc_request.id,
            code=exc.code,
            message=exc.message,
        )
    except ValidationError as exc:
        return json_rpc_error_response(
            request_id=rpc_request.id,
            code=JsonRpcErrorCode.INVALID_PARAMS,
            message="Invalid method params",
            data={"errors": exc.errors()},
        )
    except ValueError as exc:
        return json_rpc_error_response(
            request_id=rpc_request.id,
            code=JsonRpcErrorCode.INVALID_PARAMS,
            message=str(exc),
        )
    return JsonRpcResponse(id=rpc_request.id, result=result)


async def handle_notification(rpc_request: JsonRpcRequest) -> None:
    """Handle supported JSON-RPC notifications."""
    if rpc_request.method == McpMethod.INITIALIZED:
        return
    if rpc_request.method.startswith("notifications/"):
        return
    raise ValueError(f"Unsupported JSON-RPC notification: {rpc_request.method}")


async def dispatch_request(
    rpc_request: JsonRpcRequest,
    toolbox: McpToolbox,
) -> dict[str, Any]:
    """Dispatch one request-bearing MCP method."""
    method = rpc_request.method
    params = rpc_request.params or {}
    if method == McpMethod.INITIALIZE:
        return initialize_result()
    if method == McpMethod.PING:
        return {}
    if method == McpMethod.TOOLS_LIST:
        return {
            "tools": [
                tool.model_dump(mode="json", by_alias=True) for tool in list_tools()
            ]
        }
    if method == McpMethod.TOOLS_CALL:
        tool_call = ToolCallParams.model_validate(params)
        result = await toolbox.call(tool_call.name, tool_call.arguments)
        return result.model_dump(mode="json", by_alias=True)
    raise McpJsonRpcError(
        code=JsonRpcErrorCode.METHOD_NOT_FOUND,
        message=f"Method not found: {method}",
    )


def initialize_result() -> dict[str, Any]:
    """Return MCP initialize capabilities."""
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "opencortex", "version": "0.8.0"},
    }


def validate_streamable_http_headers(accept: str, content_type: str) -> None:
    """Validate Streamable HTTP POST headers."""
    if "application/json" not in content_type.lower():
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="MCP POST requires Content-Type: application/json",
        )
    accept_header = accept.lower()
    if "application/json" not in accept_header or "text/event-stream" not in (
        accept_header
    ):
        raise HTTPException(
            status_code=status.HTTP_406_NOT_ACCEPTABLE,
            detail=(
                "MCP POST requires Accept containing application/json and "
                "text/event-stream"
            ),
        )


def json_rpc_error_response(
    request_id: str | int | None,
    code: JsonRpcErrorCode,
    message: str,
    data: dict[str, Any] | None = None,
) -> JsonRpcResponse:
    """Return a JSON-RPC error response."""
    return JsonRpcResponse(
        id=request_id,
        error=JsonRpcError(code=code.integer, message=message, data=data),
    )


class McpJsonRpcError(Exception):
    """Exception carrying a JSON-RPC error code."""

    def __init__(self, code: JsonRpcErrorCode, message: str) -> None:
        """Initialize a JSON-RPC exception."""
        super().__init__(message)
        self.code = code
        self.message = message


__all__ = ["router"]
