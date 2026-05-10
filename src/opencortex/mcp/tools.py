# SPDX-License-Identifier: Apache-2.0
"""MCP tool definitions backed by OpenCortex memory flows."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from opencortex.core.identity import get_identity_profile
from opencortex.mcp.schemas import McpTool, ToolContent, ToolResult
from opencortex.store.forget import MemoryForgetter
from opencortex.store.schemas import (
    MemoryForgetRequest,
    SessionEndRequest,
    SessionTurnRequest,
    StoreRequest,
    memory_store_input_from_request,
    resource_store_input_from_request,
    session_end_input_from_request,
    session_message_input_from_request,
)
from opencortex.store.session.ender import SessionEnder
from opencortex.store.session.store import SessionStore
from opencortex.store.store import MemoryStore, ResourceStore
from opencortex.store.types import StoreRecordType
from opencortex.vector.retrieval import MemoryRetriever, RetrievalRequest


class McpToolName(StrEnum):
    """OpenCortex tools exposed through MCP."""

    SEARCH = "opencortex.search"
    STORE_MEMORY = "opencortex.store_memory"
    STORE_RESOURCE = "opencortex.store_resource"
    FORGET = "opencortex.forget"
    SESSION_MESSAGE = "opencortex.session_message"
    SESSION_END = "opencortex.session_end"


class StoreMemoryToolInput(BaseModel):
    """Input schema for storing one memory through MCP."""

    content: str = Field(..., min_length=1)
    category: str = Field(default="semantic")
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, Any] = Field(default_factory=lambda: {"kind": "api"})

    model_config = ConfigDict(extra="forbid")


class StoreResourceToolInput(StoreMemoryToolInput):
    """Input schema for storing one resource through MCP."""


class McpToolbox(BaseModel):
    """Runtime dependencies used by MCP tool dispatch."""

    retriever: MemoryRetriever
    memory_store: MemoryStore
    resource_store: ResourceStore
    forgetter: MemoryForgetter
    session_store: SessionStore
    session_ender: SessionEnder

    model_config = ConfigDict(arbitrary_types_allowed=True)

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Call one OpenCortex MCP tool."""
        tool_name = McpToolName(name)
        if tool_name == McpToolName.SEARCH:
            return await self._search(arguments)
        if tool_name == McpToolName.STORE_MEMORY:
            return await self._store_memory(arguments)
        if tool_name == McpToolName.STORE_RESOURCE:
            return await self._store_resource(arguments)
        if tool_name == McpToolName.FORGET:
            return await self._forget(arguments)
        if tool_name == McpToolName.SESSION_MESSAGE:
            return await self._session_message(arguments)
        if tool_name == McpToolName.SESSION_END:
            return await self._session_end(arguments)
        raise ValueError(f"Unsupported MCP tool: {name}")

    async def _search(self, arguments: dict[str, Any]) -> ToolResult:
        request = RetrievalRequest.model_validate(arguments)
        result = await self.retriever.search(request, profile=get_identity_profile())
        return tool_result(result.model_dump(mode="json"))

    async def _store_memory(self, arguments: dict[str, Any]) -> ToolResult:
        tool_input = StoreMemoryToolInput.model_validate(arguments)
        request = StoreRequest.model_validate(
            {
                **tool_input.model_dump(mode="json"),
                "type": StoreRecordType.MEMORY,
            }
        )
        stored = await self.memory_store.store(memory_store_input_from_request(request))
        return tool_result(stored.model_dump(mode="json"))

    async def _store_resource(self, arguments: dict[str, Any]) -> ToolResult:
        tool_input = StoreResourceToolInput.model_validate(arguments)
        request = StoreRequest.model_validate(
            {
                **tool_input.model_dump(mode="json"),
                "type": StoreRecordType.RESOURCE,
            }
        )
        stored = await self.resource_store.store(
            resource_store_input_from_request(request)
        )
        return tool_result(stored.model_dump(mode="json"))

    async def _forget(self, arguments: dict[str, Any]) -> ToolResult:
        request = MemoryForgetRequest.model_validate(arguments)
        result = await self.forgetter.forget(request, profile=get_identity_profile())
        return tool_result(result.model_dump(mode="json"))

    async def _session_message(self, arguments: dict[str, Any]) -> ToolResult:
        request = SessionTurnRequest.model_validate(arguments)
        result = await self.session_store.message(
            session_message_input_from_request(request)
        )
        return tool_result(result.model_dump(mode="json"))

    async def _session_end(self, arguments: dict[str, Any]) -> ToolResult:
        request = SessionEndRequest.model_validate(arguments)
        result = await self.session_ender.end(session_end_input_from_request(request))
        return tool_result(result.model_dump(mode="json"))


def list_tools() -> list[McpTool]:
    """Return MCP tool descriptors."""
    return [
        McpTool(
            name=McpToolName.SEARCH,
            title="Search OpenCortex Memory",
            description=(
                "Recall memory and resource records using OpenCortex retrieval."
            ),
            inputSchema=RetrievalRequest.model_json_schema(),
        ),
        McpTool(
            name=McpToolName.STORE_MEMORY,
            title="Store OpenCortex Memory",
            description="Store one semantic, episodic, or procedural memory record.",
            inputSchema=StoreMemoryToolInput.model_json_schema(),
        ),
        McpTool(
            name=McpToolName.STORE_RESOURCE,
            title="Store OpenCortex Resource",
            description="Store one resource document and enqueue its side indexes.",
            inputSchema=StoreResourceToolInput.model_json_schema(),
        ),
        McpTool(
            name=McpToolName.FORGET,
            title="Forget OpenCortex Memory",
            description="Forget the top semantic match or an explicit OpenCortex URI.",
            inputSchema=MemoryForgetRequest.model_json_schema(),
        ),
        McpTool(
            name=McpToolName.SESSION_MESSAGE,
            title="Store OpenCortex Session Message",
            description="Store one conversation turn and enqueue session side effects.",
            inputSchema=SessionTurnRequest.model_json_schema(),
        ),
        McpTool(
            name=McpToolName.SESSION_END,
            title="End OpenCortex Session",
            description="Close a conversation session and write its final memory tree.",
            inputSchema=SessionEndRequest.model_json_schema(),
        ),
    ]


def tool_result(data: dict[str, Any]) -> ToolResult:
    """Return an MCP-compatible structured result."""
    return ToolResult(
        content=[
            ToolContent(
                text=json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            )
        ],
        structuredContent=data,
    )


__all__ = ["McpToolName", "McpToolbox", "list_tools"]
