# SPDX-License-Identifier: Apache-2.0
"""Memory store flow."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

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
    MemoryStoreInput,
    PrimaryRecordInput,
    StoreDerived,
    StoreDraft,
    StoreTarget,
    StoredRecord,
)
from opencortex.store.events import StoreEvents
from opencortex.utils.json_parse import parse_json_from_response
from opencortex.utils.text import smart_truncate
from opencortex.writer.primary_record_writer import PrimaryRecordWriter

logger = logging.getLogger(__name__)


class MemoryStore:
    """Store memory records through the explicit store flow."""

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

    async def store(self, input_: MemoryStoreInput) -> StoredRecord:
        """Validate-derived memory store flow."""
        started = asyncio.get_running_loop().time()
        target = await self.resolve(input_)
        derived = await self.derive(input_)
        draft = self.assemble(input_, target, derived)
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
            context_type=ContextType.MEMORY,
            session_id="",
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            sparse_vector=embedding.sparse_vector,
            content=input_.content,
        )
        stored = await self.writer.write(record_input)
        self.events.memory_stored(record_input, stored)
        stored.meta["dedup_action"] = "created"
        logger.info(
            "[MemoryStore] store tenant=%s user=%s uri=%s timing_ms(total=%d "
            "derive_layers=%d embed=%d upsert=%d)",
            profile.tenant_id,
            profile.user_id,
            stored.uri,
            int((asyncio.get_running_loop().time() - started) * 1000),
            derived.derive_ms,
            embedding.embed_ms,
            stored.upsert_ms,
        )
        return stored

    async def resolve(self, input_: MemoryStoreInput) -> StoreTarget:
        """Resolve memory target URI and metadata."""
        uri, parent_uri = await self.namespace.resolve(
            context_type=ContextType.MEMORY,
            category=input_.category,
            abstract=input_.abstract,
        )
        return StoreTarget(
            uri=uri,
            parent_uri=parent_uri,
            meta=dict(input_.meta),
            explicit_entities=merge_unique_strings(input_.meta.get("entities")),
            explicit_topics=merge_unique_strings(input_.meta.get("topics")),
        )

    async def derive(self, input_: MemoryStoreInput) -> StoreDerived:
        """Derive memory fields."""
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
        input_: MemoryStoreInput,
        target: StoreTarget,
        derived: StoreDerived,
    ) -> StoreDraft:
        """Assemble a memory primary-record draft."""
        return self.assemble_primary_draft(
            input_=input_,
            target=target,
            derived=derived,
            context_type=ContextType.MEMORY,
            is_leaf=True,
        )

    async def derive_layers(
        self,
        *,
        abstract: str,
        overview: str,
        content: str,
    ) -> dict[str, Any]:
        """Derive memory fields without importing the old write path."""
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
                logger.warning("[MemoryStore] derive failed; using fallback")

        fallback_overview = overview or smart_truncate(str(content or "").strip(), 1200)
        return {
            "abstract": abstract
            or self.abstract_from_overview(fallback_overview, content),
            "overview": fallback_overview,
            "keywords": "",
            "entities": [],
            "anchor_handles": [],
            "fact_points": [],
        }

    def assemble_primary_draft(
        self,
        *,
        input_: MemoryStoreInput,
        target: StoreTarget,
        derived: StoreDerived,
        context_type: ContextType,
        is_leaf: bool,
    ) -> StoreDraft:
        """Assemble primary-record draft without old context builder."""
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

        anchor_handles = merge_unique_strings(
            meta.get("anchor_handles"),
            layers.get("anchor_handles", []) if input_.content else [],
        )
        if anchor_handles:
            meta["anchor_handles"] = anchor_handles
        keywords = ", ".join(keywords_list)

        profile = get_identity_profile()
        meta["project_id"] = profile.project_id
        ctx = Context(
            uri=target.uri,
            parent_uri=target.parent_uri,
            is_leaf=is_leaf,
            abstract=derived.abstract,
            overview=derived.overview,
            context_type=context_type,
            category=input_.category,
            related_uri=[],
            meta=meta,
            session_id=None,
            user=UserIdentifier(profile.tenant_id, profile.user_id),
        )

        base_text = input_.embed_text or derived.abstract
        if keywords:
            ctx.vectorize = Vectorize(f"{base_text} {keywords}")
        elif input_.embed_text:
            ctx.vectorize = Vectorize(input_.embed_text)

        effective_category = input_.category or extract_category_from_uri(target.uri)
        abstract_json = build_abstract_json(
            uri=target.uri,
            context_type=context_type,
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
        if input_.content and is_leaf:
            abstract_json["fact_points"] = layers.get("fact_points", [])
        object_payload = memory_object_payload(abstract_json, is_leaf=is_leaf)
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

    @staticmethod
    def abstract_from_overview(overview: str, content: str) -> str:
        """Return a compact abstract fallback."""
        overview_text = str(overview or "").strip()
        if overview_text:
            return smart_truncate(overview_text.splitlines()[0].strip(), 200)
        return smart_truncate(str(content or "").strip(), 200)
