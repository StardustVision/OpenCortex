# SPDX-License-Identifier: Apache-2.0
"""FastAPI routes for opencortex store flows."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query

from opencortex.core.identity import get_identity_profile
from opencortex.store.dependencies import (
    get_memory_forgetter,
    get_memory_retriever,
    get_memory_store,
    get_resource_store,
    get_session_ender,
    get_session_store,
    get_store_event_worker,
    get_store_wait_tracker,
    get_temp_upload_store,
)
from opencortex.store.event.wait import StoreWaitTracker
from opencortex.store.forget import MemoryForgetter
from opencortex.store.schemas import (
    MemoryForgetRequest,
    ResourceImportRequest,
    SessionEndRequest,
    SessionTurnRequest,
    StoreRequest,
    memory_store_input_from_request,
    resource_store_input_from_request,
    resource_store_input_from_upload,
    session_end_input_from_request,
    session_message_input_from_request,
)
from opencortex.store.session.ender import SessionEnder
from opencortex.store.session.store import SessionStore
from opencortex.store.store import MemoryStore, ResourceStore
from opencortex.store.types import StoreRecordType
from opencortex.store.upload import INLINE_STORE_MAX_BYTES, TempUploadStore
from opencortex.vector.retrieval import (
    MemoryRetriever,
    RetrievalRequest,
)

router = APIRouter(prefix="/api/v1")
MAX_STORE_WAIT_SECONDS = 55.0


@router.post("/memory/store")
async def memory_store(
    req: StoreRequest,
    memory_store_flow: Annotated[MemoryStore, Depends(get_memory_store)],
    resource_store_flow: Annotated[ResourceStore, Depends(get_resource_store)],
    wait_tracker: Annotated[StoreWaitTracker, Depends(get_store_wait_tracker)],
    event_worker: Annotated[Any, Depends(get_store_event_worker)],
) -> dict[str, Any]:
    """Store one memory or resource primary record."""
    if len(req.content.encode("utf-8")) > INLINE_STORE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                "Inline content exceeds 5MB. Use /api/v1/resources/temp_upload "
                "then /api/v1/resources/import."
            ),
        )
    request_id = uuid4().hex if req.wait else ""
    if request_id:
        await wait_tracker.register_request(request_id)

    with wait_tracker.scope(request_id):
        if req.type == StoreRecordType.MEMORY:
            stored = await memory_store_flow.store(memory_store_input_from_request(req))
        elif req.type == StoreRecordType.RESOURCE:
            stored = await resource_store_flow.store(
                resource_store_input_from_request(req)
            )
        else:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported store type: {req.type}",
            )

    response = _stored_response(stored)
    if not request_id:
        return response

    await _wait_for_store_request(
        request_id=request_id,
        wait_tracker=wait_tracker,
        event_worker=event_worker,
        requested_timeout=req.timeout,
        response=response,
    )
    return response


@router.post("/resources/temp_upload")
async def resource_temp_upload(
    body: Annotated[bytes, Body(media_type="application/octet-stream")],
    upload_store: Annotated[TempUploadStore, Depends(get_temp_upload_store)],
    filename: Annotated[str, Query()] = "",
    source_format: Annotated[str, Query()] = "",
    content_type: Annotated[str, Header(alias="content-type")] = "",
) -> dict[str, Any]:
    """Upload large resource bytes before importing them into memory store."""
    try:
        result = upload_store.save(
            body,
            filename=filename,
            content_type=content_type,
            source_format=source_format,
        )
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return _response(result)


@router.post("/resources/import")
async def resource_import(
    req: ResourceImportRequest,
    resource_store_flow: Annotated[ResourceStore, Depends(get_resource_store)],
    upload_store: Annotated[TempUploadStore, Depends(get_temp_upload_store)],
    wait_tracker: Annotated[StoreWaitTracker, Depends(get_store_wait_tracker)],
    event_worker: Annotated[Any, Depends(get_store_event_worker)],
) -> dict[str, Any]:
    """Import a previously uploaded resource and trigger retrieval indexes."""
    try:
        upload = upload_store.consume(req.upload_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="upload_id not found") from exc

    request_id = uuid4().hex if req.wait else ""
    if request_id:
        await wait_tracker.register_request(request_id)

    with wait_tracker.scope(request_id):
        stored = await resource_store_flow.store(
            resource_store_input_from_upload(req, upload)
        )

    response = _stored_response(stored)
    response["upload_id"] = upload.upload_id
    if isinstance(response.get("data"), dict):
        response["data"]["upload_id"] = upload.upload_id
    if not request_id:
        return response

    await _wait_for_store_request(
        request_id=request_id,
        wait_tracker=wait_tracker,
        event_worker=event_worker,
        requested_timeout=req.timeout,
        response=response,
    )
    return response


@router.get("/memory/store/status/{request_id}")
async def memory_store_status(
    request_id: str,
    wait_tracker: Annotated[StoreWaitTracker, Depends(get_store_wait_tracker)],
) -> dict[str, Any]:
    """Return queue status for a wait-enabled store request."""
    return _response(await wait_tracker.status(request_id))


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


@router.post("/memory/forget")
async def memory_forget(
    req: MemoryForgetRequest,
    forgetter: Annotated[MemoryForgetter, Depends(get_memory_forgetter)],
) -> dict[str, Any]:
    """Forget the top semantic match, or an explicit URI."""
    profile = get_identity_profile()
    try:
        result = await forgetter.forget(req, profile=profile)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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


async def _wait_for_store_request(
    *,
    request_id: str,
    wait_tracker: StoreWaitTracker,
    event_worker: Any,
    requested_timeout: float | None,
    response: dict[str, Any],
) -> None:
    """Wait for request-scoped indexes within the gateway-safe budget."""
    timeout = MAX_STORE_WAIT_SECONDS
    if requested_timeout is not None:
        timeout = min(timeout, max(0.0, float(requested_timeout)))

    publish_completed = True
    try:
        if event_worker is not None:
            deadline = asyncio.get_running_loop().time() + timeout
            await event_worker.wait_publish_tasks(deadline=deadline)
            timeout = max(0.0, deadline - asyncio.get_running_loop().time())
    except TimeoutError:
        publish_completed = False
    if publish_completed:
        with contextlib.suppress(TimeoutError):
            await wait_tracker.wait_for_request(request_id, timeout_seconds=timeout)
    status = await wait_tracker.status(request_id)
    if not publish_completed and status["index_status"] == "ready":
        status["index_status"] = "processing"
        status["queue_status"]["pending"] = max(1, status["queue_status"]["pending"])
    response.update(status)
    data = response.setdefault("data", {})
    if isinstance(data, dict):
        data.update(status)


def _response(data: dict[str, Any] | list[Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "code": 0,
        "message": "ok",
        "data": data,
    }
    if isinstance(data, dict):
        response.update(data)
    return response
