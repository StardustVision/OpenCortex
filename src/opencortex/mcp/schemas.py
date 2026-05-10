# SPDX-License-Identifier: Apache-2.0
"""Schemas for OpenCortex MCP Streamable HTTP endpoints."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MCP_PROTOCOL_VERSION = "2025-06-18"


class McpMethod(StrEnum):
    """JSON-RPC methods implemented by the OpenCortex MCP endpoint."""

    INITIALIZE = "initialize"
    INITIALIZED = "notifications/initialized"
    PING = "ping"
    TOOLS_LIST = "tools/list"
    TOOLS_CALL = "tools/call"


class JsonRpcErrorCode(StrEnum):
    """JSON-RPC error codes used by the MCP endpoint."""

    PARSE_ERROR = "-32700"
    INVALID_REQUEST = "-32600"
    METHOD_NOT_FOUND = "-32601"
    INVALID_PARAMS = "-32602"
    INTERNAL_ERROR = "-32603"

    @property
    def integer(self) -> int:
        """Return the JSON-RPC integer value."""
        return int(self.value)


class JsonRpcRequest(BaseModel):
    """One JSON-RPC request or notification."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


class JsonRpcError(BaseModel):
    """JSON-RPC error object."""

    code: int
    message: str
    data: dict[str, Any] | None = None


class JsonRpcResponse(BaseModel):
    """JSON-RPC response object."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    result: Any | None = None
    error: JsonRpcError | None = None

    @model_validator(mode="after")
    def validate_result_or_error(self) -> "JsonRpcResponse":
        """Require exactly one response payload shape."""
        if (self.result is None) == (self.error is None):
            raise ValueError("JSON-RPC response requires result or error")
        return self


class McpTool(BaseModel):
    """MCP tool descriptor."""

    name: str
    title: str
    description: str
    input_schema: dict[str, Any] = Field(
        default_factory=dict,
        serialization_alias="inputSchema",
        validation_alias="inputSchema",
    )


class ToolCallParams(BaseModel):
    """Params for the MCP tools/call method."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class ToolContent(BaseModel):
    """One MCP tool result content item."""

    type: Literal["text"] = "text"
    text: str


class ToolResult(BaseModel):
    """MCP tool call result."""

    content: list[ToolContent]
    structured_content: dict[str, Any] = Field(
        default_factory=dict,
        serialization_alias="structuredContent",
        validation_alias="structuredContent",
    )
    is_error: bool = Field(
        default=False,
        serialization_alias="isError",
        validation_alias="isError",
    )


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "JsonRpcError",
    "JsonRpcErrorCode",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "McpMethod",
    "McpTool",
    "ToolCallParams",
    "ToolContent",
    "ToolResult",
]
