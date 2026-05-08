# SPDX-License-Identifier: Apache-2.0
"""Resource store flow."""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any
from uuid import uuid4

from opencortex.core.context import Context, Vectorize
from opencortex.core.user_id import UserIdentifier
from opencortex.http.request_context import get_identity_profile
from opencortex.prompts import build_layer_derivation_prompt
from opencortex.retrieve.types import ContextType
from opencortex.storage.cortex_namespace import CortexNamespace
from opencortex.store.common import (
    build_abstract_json,
    extract_category_from_uri,
    memory_object_payload,
    merge_unique_strings,
    split_keyword_string,
)
from opencortex.store.embedder import StoreEmbedder
from opencortex.store.schemas import (
    PrimaryRecordInput,
    ResourceStoreInput,
    StoreDerived,
    StoreDraft,
    StoreTarget,
    StoredRecord,
)
from opencortex.store.events import StoreEvents
from opencortex.utils.json_parse import parse_json_from_response
from opencortex.utils.text import smart_truncate
from opencortex.writer.primary_record_writer import PrimaryRecordWriter


class ResourceStore:
    """Store resource primary records through the explicit store flow."""

    def __init__(
        self,
        *,
        namespace: CortexNamespace,
        llm_completion: Any,
        embedder: StoreEmbedder,
        writer: PrimaryRecordWriter,
        events: StoreEvents,
    ) -> None:
        self.namespace = namespace
        self.llm_completion = llm_completion
        self.embedder = embedder
        self.writer = writer
        self.events = events

    async def store(self, input_: ResourceStoreInput) -> StoredRecord:
        """Run the resource store flow."""
        normalized = self.prepare(input_)
        target = await self.resolve(normalized)
        derived = await self.derive(normalized)
        draft = self.assemble(normalized, target, derived)
        embedding = await self.embedder.embed_context(draft.ctx)
        profile = get_identity_profile()
        record_input = PrimaryRecordInput(
            ctx=draft.ctx,
            abstract_json=draft.abstract_json,
            object_payload=draft.object_payload,
            effective_category=draft.effective_category,
            keywords=draft.keywords,
            entities=draft.entities,
            meta=draft.meta,
            context_type=ContextType.RESOURCE,
            session_id="",
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            sparse_vector=embedding.sparse_vector,
            content=normalized.content,
        )
        stored = await self.writer.write(record_input)
        self.events.resource_stored(record_input, stored)
        stored.meta["dedup_action"] = "created"
        return stored

    def prepare(self, input_: ResourceStoreInput) -> ResourceStoreInput:
        """Prepare resource metadata."""
        meta = dict(input_.meta)
        source_path = input_.source_path or str(meta.get("file_path", "") or "")
        if source_path:
            source_doc_id = hashlib.sha256(source_path.encode()).hexdigest()[:16]
        else:
            source_doc_id = uuid4().hex[:16]

        source_doc_title = str(meta.get("title", "") or "")
        if not source_doc_title and source_path:
            source_doc_title = os.path.basename(source_path)
        meta.setdefault("source_doc_id", source_doc_id)
        meta.setdefault("source_doc_title", source_doc_title)
        meta.setdefault("source_section_path", "")
        meta.setdefault("chunk_role", "resource")
        return input_.model_copy(update={"meta": meta})

    async def resolve(self, input_: ResourceStoreInput) -> StoreTarget:
        """Resolve resource target URI and metadata."""
        abstract = input_.abstract or input_.source_path or "resource"
        uri, parent_uri = await self.namespace.resolve(
            context_type=ContextType.RESOURCE,
            category=input_.category,
            abstract=abstract,
        )
        return StoreTarget(
            uri=uri,
            parent_uri=parent_uri,
            meta=dict(input_.meta),
            explicit_entities=merge_unique_strings(input_.meta.get("entities")),
            explicit_topics=merge_unique_strings(input_.meta.get("topics")),
        )

    async def derive(self, input_: ResourceStoreInput) -> StoreDerived:
        """Derive resource summary fields."""
        derive_started = asyncio.get_running_loop().time()
        layers = await self.derive_layers(
            abstract=input_.abstract,
            overview=input_.overview,
            content=input_.content,
        )
        derive_ms = int((asyncio.get_running_loop().time() - derive_started) * 1000)
        return StoreDerived(
            abstract=input_.abstract or str(layers.get("abstract", "") or ""),
            overview=input_.overview or str(layers.get("overview", "") or ""),
            layers=layers,
            derive_ms=derive_ms,
        )

    def assemble(
        self,
        input_: ResourceStoreInput,
        target: StoreTarget,
        derived: StoreDerived,
    ) -> StoreDraft:
        """Assemble a resource primary-record draft."""
        meta = dict(target.meta)
        layers = derived.layers
        derived_entities = layers.get("entities", []) if input_.content else []
        entities = merge_unique_strings(derived_entities, target.explicit_entities)
        keywords_list = merge_unique_strings(
            split_keyword_string(str(layers.get("keywords", "") or "")),
            target.explicit_topics,
        )
        if keywords_list:
            meta["topics"] = merge_unique_strings(meta.get("topics"), keywords_list)

        keywords = ", ".join(keywords_list)
        profile = get_identity_profile()
        meta["project_id"] = profile.project_id
        ctx = Context(
            uri=target.uri,
            parent_uri=target.parent_uri,
            is_leaf=True,
            abstract=derived.abstract,
            overview=derived.overview,
            context_type=ContextType.RESOURCE,
            category=input_.category,
            related_uri=[],
            meta=meta,
            session_id=None,
            user=UserIdentifier(profile.tenant_id, profile.user_id),
        )

        embed_text = input_.embed_text
        if not embed_text and meta.get("source_doc_title"):
            embed_text = f"[{meta['source_doc_title']}] {derived.abstract}"
        if embed_text:
            ctx.vectorize = Vectorize(embed_text)
        if keywords:
            ctx.vectorize = Vectorize(f"{ctx.get_vectorization_text()} {keywords}")

        effective_category = input_.category or extract_category_from_uri(target.uri)
        abstract_json = build_abstract_json(
            uri=target.uri,
            context_type=ContextType.RESOURCE,
            category=effective_category,
            abstract=derived.abstract,
            overview=derived.overview,
            content=input_.content,
            entities=entities,
            meta=meta,
            keywords=keywords_list,
            parent_uri=target.parent_uri,
            session_id="",
        )
        if input_.content:
            abstract_json["fact_points"] = layers.get("fact_points", [])
        object_payload = memory_object_payload(abstract_json, is_leaf=True)
        return StoreDraft(
            ctx=ctx,
            abstract=derived.abstract,
            overview=derived.overview,
            keywords=keywords,
            keywords_list=keywords_list,
            entities=entities,
            meta=meta,
            effective_category=effective_category,
            abstract_json=abstract_json,
            object_payload=object_payload,
        )

    async def derive_layers(
        self,
        *,
        abstract: str,
        overview: str,
        content: str,
    ) -> dict[str, Any]:
        """Derive resource fields without old write-path imports."""
        if not content:
            return {}
        if abstract and overview:
            return {
                "abstract": abstract,
                "overview": overview,
                "keywords": "",
                "entities": [],
                "anchor_handles": [],
                "fact_points": [],
            }
        if self.llm_completion is not None:
            try:
                response = await self.llm_completion(
                    build_layer_derivation_prompt(content, abstract)
                )
                parsed = parse_json_from_response(response)
                if isinstance(parsed, dict):
                    return {
                        "abstract": abstract or str(parsed.get("abstract", "") or ""),
                        "overview": overview or str(parsed.get("overview", "") or ""),
                        "keywords": parsed.get("keywords", ""),
                        "entities": parsed.get("entities", []),
                        "anchor_handles": parsed.get("anchor_handles", []),
                        "fact_points": parsed.get("fact_points", []),
                    }
            except Exception:
                pass
        fallback_overview = overview or smart_truncate(str(content or "").strip(), 1200)
        return {
            "abstract": abstract or smart_truncate(fallback_overview, 200),
            "overview": fallback_overview,
            "keywords": "",
            "entities": [],
            "anchor_handles": [],
            "fact_points": [],
        }
