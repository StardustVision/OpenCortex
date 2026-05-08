# SPDX-License-Identifier: Apache-2.0
"""FastAPI dependencies for opencortex_app store routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from opencortex_app.storage.namespace import CortexNamespace
from opencortex_app.store.embedder import StoreEmbedder
from opencortex_app.store.event.events import StoreEvents
from opencortex_app.store.session.buffer import SessionBuffer
from opencortex_app.store.session.ender import SessionEnder
from opencortex_app.store.session.merger import SessionMerger
from opencortex_app.store.session.store import SessionStore
from opencortex_app.store.store import MemoryStore, ResourceStore
from opencortex_app.store.writer.primary_record_writer import PrimaryRecordWriter


def get_vector_store(request: Request) -> Any:
    """Return vector store for write paths."""
    vector_store = getattr(request.app.state, "vector_store", None)
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is not initialized")
    return vector_store


def get_store_config(request: Request) -> Any:
    """Return configuration for write paths."""
    config = getattr(request.app.state, "store_config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="Store config is not initialized")
    return config


def get_collection_resolver(request: Request) -> Any:
    """Return active collection resolver."""
    resolver = getattr(request.app.state, "collection_resolver", None)
    if resolver is None:
        raise HTTPException(
            status_code=503,
            detail="Collection resolver is not initialized",
        )
    return resolver


def get_ttl_resolver(request: Request) -> Any:
    """Return TTL timestamp conversion function."""
    resolver = getattr(request.app.state, "ttl_resolver", None)
    if resolver is None:
        raise HTTPException(status_code=503, detail="TTL resolver is not initialized")
    return resolver


def get_llm_completion(request: Request) -> Any:
    """Return optional LLM completion callable for derivation."""
    return getattr(request.app.state, "store_llm_completion", None)


def get_embedding_model(request: Request) -> Any:
    """Return optional embedding model."""
    return getattr(request.app.state, "store_embedder", None)


def get_memory_events(request: Request) -> Any:
    """Return write-path event manager."""
    return getattr(request.app.state, "store_memory_events", None)


def get_cortex_namespace(
    collection_resolver: Annotated[Any, Depends(get_collection_resolver)],
) -> CortexNamespace:
    """Return URI namespace resolver."""
    return CortexNamespace(
        collection_resolver=collection_resolver,
    )


def get_store_embedder(
    embedding_model: Annotated[Any, Depends(get_embedding_model)],
) -> StoreEmbedder:
    """Return store embedder."""
    return StoreEmbedder(embedding_model)


def get_primary_record_writer(
    vector_store: Annotated[Any, Depends(get_vector_store)],
    collection_resolver: Annotated[Any, Depends(get_collection_resolver)],
    namespace: Annotated[CortexNamespace, Depends(get_cortex_namespace)],
) -> PrimaryRecordWriter:
    """Return primary record writer."""
    return PrimaryRecordWriter(
        vector_store=vector_store,
        collection_resolver=collection_resolver,
        namespace=namespace,
    )


def get_store_events(
    memory_events: Annotated[Any, Depends(get_memory_events)],
) -> StoreEvents:
    """Return write event publisher."""
    return StoreEvents(memory_events)


def get_memory_store(
    namespace: Annotated[CortexNamespace, Depends(get_cortex_namespace)],
    writer: Annotated[PrimaryRecordWriter, Depends(get_primary_record_writer)],
    events: Annotated[StoreEvents, Depends(get_store_events)],
) -> MemoryStore:
    """Return memory store flow."""
    return MemoryStore(
        namespace=namespace,
        writer=writer,
        events=events,
    )


def get_resource_store(
    namespace: Annotated[CortexNamespace, Depends(get_cortex_namespace)],
    writer: Annotated[PrimaryRecordWriter, Depends(get_primary_record_writer)],
    events: Annotated[StoreEvents, Depends(get_store_events)],
) -> ResourceStore:
    """Return resource store flow."""
    return ResourceStore(
        namespace=namespace,
        writer=writer,
        events=events,
    )


def get_session_buffer(request: Request) -> SessionBuffer:
    """Return session message buffer."""
    buffer = getattr(request.app.state, "session_buffer", None)
    if buffer is None:
        raise HTTPException(status_code=503, detail="Session buffer is not initialized")
    return buffer


def get_session_store(
    buffer: Annotated[SessionBuffer, Depends(get_session_buffer)],
    namespace: Annotated[CortexNamespace, Depends(get_cortex_namespace)],
    store_embedder: Annotated[StoreEmbedder, Depends(get_store_embedder)],
    writer: Annotated[PrimaryRecordWriter, Depends(get_primary_record_writer)],
    events: Annotated[StoreEvents, Depends(get_store_events)],
    config: Annotated[Any, Depends(get_store_config)],
    ttl_resolver: Annotated[Any, Depends(get_ttl_resolver)],
) -> SessionStore:
    """Return session message store flow."""
    return SessionStore(
        buffer=buffer,
        namespace=namespace,
        embedder=store_embedder,
        writer=writer,
        events=events,
        config=config,
        ttl_from_hours=ttl_resolver,
    )


def get_session_merger(
    buffer: Annotated[SessionBuffer, Depends(get_session_buffer)],
    namespace: Annotated[CortexNamespace, Depends(get_cortex_namespace)],
    writer: Annotated[PrimaryRecordWriter, Depends(get_primary_record_writer)],
    events: Annotated[StoreEvents, Depends(get_store_events)],
    config: Annotated[Any, Depends(get_store_config)],
    ttl_resolver: Annotated[Any, Depends(get_ttl_resolver)],
) -> SessionMerger:
    """Return session merge flow."""
    return SessionMerger(
        buffer=buffer,
        namespace=namespace,
        writer=writer,
        events=events,
        config=config,
        ttl_from_hours=ttl_resolver,
    )


def get_session_ender(
    buffer: Annotated[SessionBuffer, Depends(get_session_buffer)],
    merger: Annotated[SessionMerger, Depends(get_session_merger)],
    namespace: Annotated[CortexNamespace, Depends(get_cortex_namespace)],
    writer: Annotated[PrimaryRecordWriter, Depends(get_primary_record_writer)],
    events: Annotated[StoreEvents, Depends(get_store_events)],
    vector_store: Annotated[Any, Depends(get_vector_store)],
    collection_resolver: Annotated[Any, Depends(get_collection_resolver)],
) -> SessionEnder:
    """Return session end flow."""
    return SessionEnder(
        buffer=buffer,
        merger=merger,
        namespace=namespace,
        writer=writer,
        events=events,
        vector_store=vector_store,
        collection_resolver=collection_resolver,
    )
