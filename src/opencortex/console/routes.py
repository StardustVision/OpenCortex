# SPDX-License-Identifier: Apache-2.0
"""Routes used by the OpenCortex web console."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import models

from opencortex.core.identity import IdentityProfile, get_identity_profile
from opencortex.store.dependencies import (
    get_collection_resolver,
    get_cortex_storage,
    get_vector_store,
)
from opencortex.store.forget import MemoryForgetter
from opencortex.store.schemas import MemoryForgetRequest
from opencortex.store.types import ContextType
from opencortex.vector.payloads import VectorPayloadSurface
from opencortex.vector.retrieval import MemoryRetriever, RetrievalRequest
from opencortex.vector.retrieval.filters import field_match

router = APIRouter(prefix="/console/v1")


class ConsoleMemoryRecord(BaseModel):
    """Memory record summary shown in the web console."""

    uri: str
    abstract: str = ""
    overview: str = ""
    content: str = ""
    category: str = ""
    context_type: str = ""
    scope: str = ""
    project_id: str = ""
    session_id: str = ""
    source_tenant_id: str = ""
    source_user_id: str = ""
    updated_at: str = ""
    created_at: str = ""
    score: float | None = None
    retrieval_surfaces: list[str] = Field(default_factory=list)
    keywords: str = ""
    entities: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class ConsoleMemoryListResponse(BaseModel):
    """Console memory list response."""

    results: list[ConsoleMemoryRecord]
    total: int


class ConsoleContentResponse(BaseModel):
    """Console memory content response."""

    uri: str
    abstract: str = ""
    overview: str = ""
    content: str = ""


class ConsoleStatsResponse(BaseModel):
    """Console storage summary."""

    tenant_id: str
    user_id: str
    project_id: str
    role: str
    total_records: int = 0
    primary_records: int = 0
    by_context_type: dict[str, int] = Field(default_factory=dict)
    by_surface: dict[str, int] = Field(default_factory=dict)


class ConsoleSearchRequest(BaseModel):
    """Console search request."""

    query: str = Field(..., min_length=1)
    limit: int = Field(default=20, ge=1, le=50)

    model_config = ConfigDict(extra="forbid")


class ConsoleForgetRequest(BaseModel):
    """Console forget request."""

    query: str = ""
    uri: str = ""
    tenant_id: str = ""
    user_id: str = ""
    project_id: str = "public"

    model_config = ConfigDict(extra="forbid")


class ConsoleMemoryQuery(BaseModel):
    """Normalized console list query."""

    tenant_id: str = ""
    user_id: str = ""
    project_id: str = ""
    context_type: str = ""
    category: str = ""
    limit: int
    offset: int


@router.get("/memories", response_model=ConsoleMemoryListResponse)
async def console_list_memories(
    vector_store: Annotated[Any, Depends(get_vector_store)],
    collection_resolver: Annotated[Any, Depends(get_collection_resolver)],
    tenant_id: str = "",
    user_id: str = "",
    project_id: str = "",
    context_type: str = "",
    category: str = "",
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ConsoleMemoryListResponse:
    """List primary records visible to the web console."""
    profile = console_profile(
        tenant_id=tenant_id, user_id=user_id, project_id=project_id
    )
    records, total = await load_console_memories(
        vector_store,
        collection_resolver(),
        profile=profile,
        query=ConsoleMemoryQuery(
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
            context_type=context_type,
            category=category,
            limit=limit,
            offset=offset,
        ),
    )
    return ConsoleMemoryListResponse(
        results=[memory_record(record) for record in records],
        total=total,
    )


@router.post("/memories/search", response_model=ConsoleMemoryListResponse)
async def console_search_memories(
    req: ConsoleSearchRequest,
    retriever: Annotated[MemoryRetriever, Depends(console_memory_retriever)],
    tenant_id: str = "",
    user_id: str = "",
    project_id: str = "",
) -> ConsoleMemoryListResponse:
    """Search records visible to the web console."""
    profile = console_profile(
        tenant_id=tenant_id,
        user_id=user_id,
        project_id=project_id,
    )
    result = await retriever.search(
        RetrievalRequest(query=req.query, limit=req.limit),
        profile=profile,
    )
    return ConsoleMemoryListResponse(
        results=[
            memory_record_from_search(item.model_dump(mode="json"))
            for item in result.results
        ],
        total=result.total,
    )


@router.get("/memories/content", response_model=ConsoleContentResponse)
async def console_memory_content(
    cortex_storage: Annotated[Any, Depends(get_cortex_storage)],
    vector_store: Annotated[Any, Depends(get_vector_store)],
    collection_resolver: Annotated[Any, Depends(get_collection_resolver)],
    uri: str,
) -> ConsoleContentResponse:
    """Return all display layers for one memory URI."""
    await require_visible_memory(
        vector_store,
        collection_resolver(),
        uri,
        profile=console_profile(),
    )
    abstract, overview, content = await asyncio.gather(
        read_optional(cortex_storage.abstract(uri)),
        read_optional(cortex_storage.overview(uri)),
        read_optional(cortex_storage.read_file(f"{uri}/content.md")),
    )
    return ConsoleContentResponse(
        uri=uri,
        abstract=abstract,
        overview=overview,
        content=content,
    )


@router.delete("/memories", response_model=dict[str, Any])
async def console_forget_memory(
    req: ConsoleForgetRequest,
    forgetter: Annotated[MemoryForgetter, Depends(console_memory_forgetter)],
) -> dict[str, Any]:
    """Forget one visible memory from the web console."""
    profile = console_profile(
        tenant_id=req.tenant_id,
        user_id=req.user_id,
        project_id=req.project_id,
    )
    try:
        result = await forgetter.forget(
            MemoryForgetRequest(query=req.query, uri=req.uri),
            profile=profile,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result.model_dump(mode="json")


@router.get("/stats", response_model=ConsoleStatsResponse)
async def console_stats(
    vector_store: Annotated[Any, Depends(get_vector_store)],
    collection_resolver: Annotated[Any, Depends(get_collection_resolver)],
    tenant_id: str = "",
    user_id: str = "",
    project_id: str = "",
) -> ConsoleStatsResponse:
    """Return storage counts for the active console scope."""
    profile = console_profile(
        tenant_id=tenant_id, user_id=user_id, project_id=project_id
    )
    collection = collection_resolver()
    base_filter = console_filter(
        profile,
        tenant_id=tenant_id,
        user_id=user_id,
        project_id=project_id,
    )
    surfaces = await vector_store.facet(collection, "retrieval_surface", base_filter)
    context_types = await vector_store.facet(collection, "context_type", base_filter)
    primary_filter = console_filter(
        profile,
        tenant_id=tenant_id,
        user_id=user_id,
        project_id=project_id,
        primary_only=True,
    )
    return ConsoleStatsResponse(
        tenant_id=profile.tenant_id,
        user_id=profile.user_id,
        project_id=profile.project_id,
        role=get_identity_profile().role,
        total_records=await vector_store.count(collection, base_filter),
        primary_records=await vector_store.count(collection, primary_filter),
        by_context_type=dict(context_types),
        by_surface=dict(surfaces),
    )


def console_profile(
    *,
    tenant_id: str = "",
    user_id: str = "",
    project_id: str = "",
) -> IdentityProfile:
    """Return the identity scope requested by the console."""
    current = get_identity_profile()
    if current.role != "admin":
        return current
    return current.model_copy(
        update={
            "tenant_id": tenant_id.strip() or current.tenant_id,
            "user_id": user_id.strip() or current.user_id,
            "project_id": project_id.strip() or current.project_id,
        }
    )


def console_filter(
    profile: IdentityProfile,
    *,
    tenant_id: str = "",
    user_id: str = "",
    project_id: str = "",
    context_type: str = "",
    category: str = "",
    uri: str = "",
    primary_only: bool = False,
) -> models.Filter:
    """Build a Qdrant filter for console-visible records."""
    current = get_identity_profile()
    must: list[models.Condition] = []
    if current.role == "admin":
        if tenant_id:
            must.append(field_match("tenant_id", tenant_id))
        if user_id:
            must.append(field_match("user_id", user_id))
        if project_id:
            must.append(field_match("project_id", project_id))
    else:
        must.extend(
            [
                field_match("tenant_id", profile.tenant_id),
                field_match("user_id", profile.user_id),
                field_match("project_id", profile.project_id),
            ]
        )
    if context_type:
        must.append(field_match("context_type", context_type))
    if category:
        must.append(field_match("category", category))
    if uri:
        must.append(field_match("uri", uri))
    if primary_only:
        must.append(
            field_match("retrieval_surface", str(VectorPayloadSurface.L0_OBJECT))
        )
        must.append(
            models.FieldCondition(
                key="context_type",
                match=models.MatchAny(
                    any=[str(ContextType.MEMORY), str(ContextType.RESOURCE)]
                ),
            )
        )
    return models.Filter(must=must)


async def require_visible_memory(
    vector_store: Any,
    collection: str,
    uri: str,
    *,
    profile: IdentityProfile,
) -> dict[str, Any]:
    """Return a visible primary record or reject access."""
    target_uri = uri.strip()
    if not target_uri:
        raise HTTPException(status_code=422, detail="uri is required")
    records = await vector_store.filter(
        collection,
        console_filter(profile, uri=target_uri, primary_only=True),
        limit=1,
    )
    if not records:
        raise HTTPException(status_code=404, detail="Memory not found")
    return dict(records[0])


def console_memory_retriever(
    vector_store: Annotated[Any, Depends(get_vector_store)],
    collection_resolver: Annotated[Any, Depends(get_collection_resolver)],
    cortex_storage: Annotated[Any, Depends(get_cortex_storage)],
    request: Request,
) -> MemoryRetriever:
    """Return memory retriever for console search."""
    return MemoryRetriever(
        vector_store=vector_store,
        collection_resolver=collection_resolver,
        embedder=getattr(request.app.state, "store_embedder", None),
        cortex_storage=cortex_storage,
        llm_completion=getattr(request.app.state, "store_llm_completion", None),
    )


def console_memory_forgetter(
    vector_store: Annotated[Any, Depends(get_vector_store)],
    collection_resolver: Annotated[Any, Depends(get_collection_resolver)],
    cortex_storage: Annotated[Any, Depends(get_cortex_storage)],
    retriever: Annotated[MemoryRetriever, Depends(console_memory_retriever)],
) -> MemoryForgetter:
    """Return forget flow for console deletion."""
    return MemoryForgetter(
        vector_store=vector_store,
        collection_resolver=collection_resolver,
        cortex_storage=cortex_storage,
        retriever=retriever,
    )


async def load_console_memories(
    vector_store: Any,
    collection: str,
    *,
    profile: IdentityProfile,
    query: ConsoleMemoryQuery,
) -> tuple[list[dict[str, Any]], int]:
    """Load a console page, preferring Qdrant pagination when possible."""
    filters = console_filter(
        profile,
        tenant_id=query.tenant_id,
        user_id=query.user_id,
        project_id=query.project_id,
        context_type=query.context_type,
        category=query.category,
        primary_only=True,
    )
    total = await vector_store.count(collection, filters)
    if query.offset == 0:
        page = await vector_store.scroll(collection, filters, limit=query.limit)
        return page.records, total
    records = await vector_store.filter(
        collection,
        filters,
        limit=query.offset + query.limit,
    )
    return sorted(records, key=record_sort_key, reverse=True)[
        query.offset : query.offset + query.limit
    ], total


def memory_record(record: dict[str, Any]) -> ConsoleMemoryRecord:
    """Convert a vector payload into a console memory record."""
    return ConsoleMemoryRecord(
        uri=str(record.get("uri", "") or ""),
        abstract=str(record.get("abstract", "") or ""),
        overview=str(record.get("overview", "") or ""),
        content=str(record.get("content", "") or ""),
        category=str(record.get("category", "") or ""),
        context_type=str(record.get("context_type", "") or ""),
        scope=str(record.get("scope", "") or ""),
        project_id=str(record.get("project_id", "") or ""),
        session_id=str(record.get("session_id", "") or ""),
        source_tenant_id=str(record.get("source_tenant_id", "") or ""),
        source_user_id=str(record.get("source_user_id", "") or ""),
        updated_at=record_timestamp(record),
        created_at=record_timestamp(record),
        retrieval_surfaces=[str(record.get("retrieval_surface", "") or "")],
        keywords=str(record.get("keywords", "") or ""),
        entities=list(record.get("entities") or []),
        meta=dict(record.get("meta") or {}),
    )


def memory_record_from_search(record: dict[str, Any]) -> ConsoleMemoryRecord:
    """Convert a public recall result into a console memory record."""
    source = dict(record.get("source") or {})
    return ConsoleMemoryRecord(
        uri=str(record.get("uri", "") or ""),
        abstract=str(record.get("abstract", "") or ""),
        overview=str(record.get("overview", "") or ""),
        content=str(record.get("content", "") or ""),
        category=str(record.get("category", "") or ""),
        context_type=str(
            record.get("type", "") or record.get("context_type", "") or ""
        ),
        project_id=str(record.get("project_id", "") or ""),
        session_id=str(
            record.get("session_id", "") or source.get("session_id", "") or ""
        ),
        updated_at=record_timestamp(record),
        created_at=record_timestamp(record),
        keywords=str(record.get("keywords", "") or ""),
        entities=list(record.get("entities") or []),
        meta=dict(record.get("meta") or {}),
        score=record.get("score"),
    )


def record_timestamp(record: dict[str, Any]) -> str:
    """Return the best available display timestamp."""
    meta = dict(record.get("meta") or {})
    return str(
        meta.get("updated_at") or meta.get("created_at") or meta.get("timestamp") or ""
    )


def record_sort_key(record: dict[str, Any]) -> tuple[str, str]:
    """Return a deterministic list sort key."""
    return (record_timestamp(record), str(record.get("uri", "") or ""))


async def read_optional(awaitable: Any) -> str:
    """Read an optional CFS layer."""
    try:
        content = await awaitable
    except FileNotFoundError:
        return ""
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return str(content or "")


__all__ = ["router"]
