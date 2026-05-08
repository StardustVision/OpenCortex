# SPDX-License-Identifier: Apache-2.0
"""FastAPI routes for opencortex_app store flows."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from opencortex_app.core.identity import get_identity_profile
from opencortex_app.store.dependencies import (
    get_memory_retriever,
    get_memory_store,
    get_resource_store,
    get_session_ender,
    get_session_store,
)
from opencortex_app.store.schemas import (
    SessionEndRequest,
    SessionTurnRequest,
    StoreRequest,
    memory_store_input_from_request,
    resource_store_input_from_request,
    session_end_input_from_request,
    session_message_input_from_request,
)
from opencortex_app.store.session.ender import SessionEnder
from opencortex_app.store.session.store import SessionStore
from opencortex_app.store.store import MemoryStore, ResourceStore
from opencortex_app.store.types import StoreRecordType
from opencortex_app.vector.retrieval import (
    MemoryRetriever,
    RetrievalRequest,
)

router = APIRouter(prefix="/api/v1")


@router.post("/memory/store")
async def memory_store(
    req: StoreRequest,
    memory_store_flow: Annotated[MemoryStore, Depends(get_memory_store)],
    resource_store_flow: Annotated[ResourceStore, Depends(get_resource_store)],
) -> dict[str, Any]:
    """Store one memory or resource primary record."""
    if req.type == StoreRecordType.MEMORY:
        stored = await memory_store_flow.store(memory_store_input_from_request(req))
        return _stored_response(stored)

    if req.type == StoreRecordType.RESOURCE:
        stored = await resource_store_flow.store(resource_store_input_from_request(req))
        return _stored_response(stored)

    raise HTTPException(
        status_code=422,
        detail=f"Unsupported store type: {req.type}",
    )


@router.post("/memory/search")
async def memory_search(
    req: RetrievalRequest,
    retriever: Annotated[
        MemoryRetriever,
        Depends(get_memory_retriever),
    ],
) -> dict[str, Any]:
    """Search retrieval-ready memory records."""
    profile = get_identity_profile()
    result = await retriever.search(req, profile=profile)
    return _response(result.model_dump(mode="json"))


@router.post("/session/message")
async def session_message(
    req: SessionTurnRequest,
    session_store: Annotated[SessionStore, Depends(get_session_store)],
) -> dict[str, Any]:
    """Store one conversation turn."""
    result = await session_store.message(session_message_input_from_request(req))
    return _response(result.model_dump())


@router.post("/session/end")
async def session_end(
    req: SessionEndRequest,
    session_ender: Annotated[SessionEnder, Depends(get_session_ender)],
) -> dict[str, Any]:
    """Close one conversation session."""
    result = await session_ender.end(session_end_input_from_request(req))
    return _response(result.model_dump())


def _stored_response(stored: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "uri": stored.uri,
        "context_type": stored.context_type,
        "category": stored.category,
        "abstract": stored.abstract,
    }
    if stored.meta.get("dedup_action"):
        data["dedup_action"] = stored.meta["dedup_action"]
    return _response(data)


def _response(data: dict[str, Any] | list[Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "code": 0,
        "message": "ok",
        "data": data,
    }
    if isinstance(data, dict):
        response.update(data)
    return response
