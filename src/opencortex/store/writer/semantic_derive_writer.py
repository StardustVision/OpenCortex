# SPDX-License-Identifier: Apache-2.0
"""Worker writer that completes raw primary records with LLM semantics."""

from __future__ import annotations

from typing import Any

import structlog

from opencortex.store.common import (
    build_abstract_json,
    memory_object_payload,
    merge_unique_strings,
    split_keyword_string,
)
from opencortex.store.derive import derive_layers
from opencortex.store.event.events import MemoryEvent
from opencortex.store.writer.event_payload import event_content, primary_record
from opencortex.utils.facts import (
    extract_time_refs,
    merge_preserved_fact_points,
    normalize_date_ref,
    overview_with_fact_section,
    preserve_summary_fidelity,
    temporal_payload_fields,
)

logger = structlog.get_logger(__name__)


class SemanticDeriveWriter:
    """Complete raw primary records after the synchronous write succeeds."""

    def __init__(
        self,
        *,
        vector_store: Any,
        collection_resolver: Any,
        llm_completion: Any,
        embedder: Any = None,
    ) -> None:
        self.vector_store = vector_store
        self.collection_resolver = collection_resolver
        self.llm_completion = llm_completion
        self.embedder = embedder

    async def write(self, event: MemoryEvent) -> dict[str, Any]:
        """Derive semantic fields, update primary Qdrant record, and return it."""
        record = primary_record(event)
        if not record or record.get("derive_status") == "ready":
            return record
        content = str(record.get("content") or event_content(event) or "")
        if not content:
            return record

        layers = await derive_layers(
            llm_completion=self.llm_completion,
            content=content,
            record_kind=str(record.get("context_type") or "memory"),
            uri=str(record.get("uri", "") or ""),
            parent_uri=str(record.get("parent_uri", "") or ""),
        )
        ready_record = self.ready_record(record, layers, content)
        self.embed_record(ready_record)
        await self.vector_store.upsert(self.collection_resolver(), ready_record)
        return ready_record

    def ready_record(
        self,
        record: dict[str, Any],
        layers: dict[str, Any],
        content: str,
    ) -> dict[str, Any]:
        """Build the ready primary record payload from raw payload and LLM output."""
        ready = dict(record)
        meta = dict(ready.get("meta") or {})
        time_refs = merge_unique_strings(
            meta.get("time_refs"),
            ready.get("event_date"),
            meta.get("event_date"),
            extract_time_refs(content),
        )
        if time_refs:
            meta["time_refs"] = time_refs
        derived_entities = layers.get("entities", [])
        explicit_entities = meta.get("entities", [])
        entities = merge_unique_strings(derived_entities, explicit_entities)
        keywords_list = merge_unique_strings(
            layers.get("keywords", []),
            split_keyword_string(str(meta.get("topics", "") or "")),
            meta.get("topics", []),
        )
        if keywords_list:
            meta["topics"] = merge_unique_strings(meta.get("topics"), keywords_list)

        anchor_handles = merge_unique_strings(
            meta.get("anchor_handles"),
            layers.get("anchor_handles", []),
        )
        if anchor_handles:
            meta["anchor_handles"] = anchor_handles

        fact_points: list[str] = []
        if bool(ready.get("is_leaf", False)):
            fact_points = merge_preserved_fact_points(
                layers.get("fact_points", []),
                content=content,
                max_points=24,
            )
        abstract = preserve_summary_fidelity(
            str(layers.get("abstract", "") or ""), content=content
        )
        overview = overview_with_fact_section(
            preserve_summary_fidelity(
                str(layers.get("overview", "") or ""),
                content=content,
                max_length=1000,
            ),
            fact_points,
        )
        keywords = ", ".join(keywords_list)
        abstract_json = build_abstract_json(
            uri=str(ready.get("uri", "") or ""),
            context_type=str(ready.get("context_type", "") or ""),
            category=str(ready.get("category", "") or ""),
            abstract=abstract,
            overview=overview,
            content=content,
            entities=entities,
            meta=meta,
            keywords=keywords_list,
            parent_uri=str(ready.get("parent_uri", "") or ""),
            session_id=str(ready.get("session_id", "") or ""),
        )
        if bool(ready.get("is_leaf", False)):
            abstract_json["fact_points"] = fact_points

        temporal_fields = temporal_payload_fields(
            ready.get("event_date"),
            meta.get("event_date"),
            meta.get("time_refs"),
            content,
        )
        event_ts = normalize_date_ref(ready.get("event_date") or meta.get("event_date"))
        utterance_ts = normalize_date_ref(
            meta.get("utterance_ts") or meta.get("timestamp")
        )
        if event_ts:
            temporal_fields["event_ts"] = event_ts
        if utterance_ts:
            temporal_fields["utterance_ts"] = utterance_ts

        ready.update(
            {
                "abstract": abstract,
                "overview": overview,
                "entities": entities,
                "keywords": keywords,
                "meta": meta,
                "abstract_json": abstract_json,
                "derive_status": "ready",
                "retrieval_ready": True,
                **temporal_fields,
            }
        )
        ready.update(
            memory_object_payload(
                abstract_json,
                is_leaf=bool(ready["is_leaf"]),
            )
        )
        if bool(ready.get("is_leaf", False)) and not ready.get("retrieval_surface"):
            ready["retrieval_surface"] = "primary"
        return ready

    def embed_record(self, record: dict[str, Any]) -> None:
        """Attach required vectors for a retrieval-ready primary record."""
        if self.embedder is None:
            raise RuntimeError("SemanticDeriveWriter requires an embedder")
        text = self.embedding_text(record)
        result = self.embedder.embed(text)
        if getattr(result, "dense_vector", None):
            record["vector"] = result.dense_vector
        else:
            raise ValueError("Semantic derivation embedding returned no dense vector")
        if getattr(result, "sparse_vector", None):
            record["sparse_vector"] = result.sparse_vector

    @staticmethod
    def embedding_text(record: dict[str, Any]) -> str:
        """Return embedding text for a ready primary record."""
        meta = dict(record.get("meta") or {})
        prefix = ""
        if meta.get("source_doc_title"):
            prefix = f"[{meta['source_doc_title']}] "
        keywords = str(record.get("keywords", "") or "")
        abstract_json = record.get("abstract_json")
        fact_points = []
        if isinstance(abstract_json, dict):
            fact_points = [
                str(fact)
                for fact in (abstract_json.get("fact_points") or [])[:4]
                if str(fact).strip()
            ]
        base = str(record.get("overview") or record.get("abstract") or "")
        facts = " ".join(fact_points)
        return f"{prefix}{base} {facts} {keywords}".strip()
