# SPDX-License-Identifier: Apache-2.0
"""Queue-facing derivation coordinator for OpenCortex.

Pure LLM layer derivation lives in MemoryLayerDerivationService. This module
keeps deferred derive completion, persistence, and compatibility wrappers for
existing orchestrator callers.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opencortex.services.memory_layer_derivation_service import (
    MemoryLayerDerivationService,
)

if TYPE_CHECKING:
    from opencortex.cortex_memory import CortexMemory
    from opencortex.services.memory_record_service import MemoryRecordService

logger = logging.getLogger(__name__)


def _merge_unique_strings(*groups: Any) -> List[str]:
    """Return a stable ordered union of non-empty string values."""
    merged: List[str] = []
    for group in groups:
        if not group:
            continue
        values = [group] if isinstance(group, str) else list(group)
        for value in values:
            normalized = str(value).strip()
            if normalized and normalized not in merged:
                merged.append(normalized)
    return merged


def _split_keyword_string(raw_keywords: str) -> List[str]:
    """Split a comma-separated keyword string into normalized tokens."""
    if not raw_keywords:
        return []
    return [
        token.strip()
        for token in str(raw_keywords).split(",")
        if token and token.strip()
    ]


@dataclass
class DeriveTask:
    """Async document derive task enqueued by write path and consumed by worker."""

    parent_uri: str
    content: str
    abstract: str
    chunks: list
    category: str
    context_type: str
    meta: Dict[str, Any]
    session_id: Optional[str]
    source_path: str
    source_doc_id: str
    source_doc_title: str
    tenant_id: str
    user_id: str


class DerivationService:
    """Coordinate deferred derive persistence using orchestrator-owned subsystems."""

    def __init__(self, orchestrator: CortexMemory) -> None:
        self._orch = orchestrator

    @property
    def _deferred_derive_count(self) -> int:
        return self._orch._deferred_derive_count

    @_deferred_derive_count.setter
    def _deferred_derive_count(self, value: int) -> None:
        self._orch._deferred_derive_count = value

    @property
    def _embedder(self) -> Any:
        return self._orch._embedder

    @property
    def _storage(self) -> Any:
        return self._orch._storage

    @property
    def _fs(self) -> Any:
        return self._orch._fs

    @property
    def _layer_derivation_service(self) -> MemoryLayerDerivationService:
        """Return the pure LLM layer derivation service."""
        cached = getattr(self, "_layer_derivation_service_instance", None)
        if cached is None:
            cached = MemoryLayerDerivationService(self._orch)
            self._layer_derivation_service_instance = cached
        return cached

    def _get_collection(self) -> str:
        return self._orch._get_collection()

    @property
    def _memory_record_service(self) -> "MemoryRecordService":
        """Return the orchestrator-owned memory record/projection service."""
        return self._orch._memory_record_service

    def _extract_category_from_uri(self, uri: str) -> str:
        return self._memory_record_service._extract_category_from_uri(uri)

    def _derive_parent_uri(self, uri: str) -> str:
        return self._memory_record_service._derive_parent_uri(uri)

    async def _get_record_by_uri(self, uri: str) -> Optional[Dict[str, Any]]:
        return await self._orch._get_record_by_uri(uri)

    async def _derive_parent_summary(
        self,
        doc_title: str,
        children_abstracts: List[str],
    ) -> Dict[str, Any]:
        """Delegate parent summary derivation to MemoryLayerDerivationService."""
        return await self._layer_derivation_service._derive_parent_summary(
            doc_title=doc_title,
            children_abstracts=children_abstracts,
        )

    async def _derive_layers(
        self,
        user_abstract: str,
        content: str,
        user_overview: str = "",
    ) -> Dict[str, Any]:
        """Delegate pure layer derivation to MemoryLayerDerivationService."""
        return await self._layer_derivation_service._derive_layers(
            user_abstract=user_abstract,
            content=content,
            user_overview=user_overview,
        )

    @staticmethod
    def _coerce_derived_string(value: str) -> str:
        """Delegate derived string normalization to MemoryLayerDerivationService."""
        return MemoryLayerDerivationService._coerce_derived_string(value)

    @staticmethod
    def _coerce_derived_list(
        value: Any,
        *,
        limit: int,
        lowercase: bool = False,
    ) -> List[str]:
        """Delegate derived list normalization to MemoryLayerDerivationService."""
        return MemoryLayerDerivationService._coerce_derived_list(
            value,
            limit=limit,
            lowercase=lowercase,
        )

    async def _derive_layers_split_fields(
        self,
        *,
        user_abstract: str,
        content: str,
        user_overview: str,
    ) -> Dict[str, Any]:
        """Delegate split-field derivation to MemoryLayerDerivationService."""
        return await self._layer_derivation_service._derive_layers_split_fields(
            user_abstract=user_abstract,
            content=content,
            user_overview=user_overview,
        )

    async def _complete_deferred_derive(
        self,
        uri: str,
        content: str,
        abstract: str = "",
        overview: str = "",
        session_id: str = "",
        meta: Optional[Dict[str, Any]] = None,
        context_type: str = "memory",
        raise_on_error: bool = False,
    ) -> None:
        """Run LLM derive and update Qdrant plus CortexFS."""
        self._deferred_derive_count += 1
        try:
            layers = await self._derive_layers(
                user_abstract=abstract,
                content=content,
                user_overview=overview,
            )
            new_abstract = layers.get("abstract") or abstract
            new_overview = layers.get("overview") or overview
            keywords = layers.get("keywords", "")
            entities = layers.get("entities", [])
            anchor_handles = layers.get("anchor_handles", [])
            fact_points = layers.get("fact_points", [])

            keywords_list = _split_keyword_string(keywords)
            keywords_str = ", ".join(keywords_list)

            vectorize_text = (
                f"{new_abstract} {keywords_str}".strip()
                if keywords_str
                else new_abstract
            )

            loop = asyncio.get_event_loop()
            result = None
            if self._embedder:
                result = await loop.run_in_executor(
                    None,
                    self._embedder.embed,
                    vectorize_text,
                )

            meta = dict(meta or {})
            if keywords_list:
                meta["topics"] = _merge_unique_strings(
                    meta.get("topics"), keywords_list
                )
            if anchor_handles:
                meta["anchor_handles"] = anchor_handles
            if entities:
                meta["entities"] = entities

            effective_category = self._extract_category_from_uri(uri)
            abstract_json = self._build_abstract_json(
                uri=uri,
                context_type=context_type,
                category=effective_category,
                abstract=new_abstract,
                overview=new_overview,
                content=content,
                entities=entities,
                meta=meta,
                keywords=keywords_list,
                parent_uri=self._derive_parent_uri(uri),
                session_id=session_id,
            )
            abstract_json["fact_points"] = fact_points

            update_payload: Dict[str, Any] = {
                "abstract": new_abstract,
                "overview": new_overview,
                "keywords": keywords_str,
                "entities": entities,
                "abstract_json": abstract_json,
            }
            if result and result.dense_vector:
                update_payload["vector"] = result.dense_vector
            if result and result.sparse_vector:
                update_payload["sparse_vector"] = result.sparse_vector

            existing = await self._get_record_by_uri(uri)
            if existing:
                await self._storage.update(
                    self._get_collection(),
                    str(existing["id"]),
                    update_payload,
                )
                record = dict(existing)
                record.update(update_payload)
                record["abstract_json"] = abstract_json
                await self._sync_anchor_projection_records(
                    source_record=record,
                    abstract_json=abstract_json,
                )

            await self._fs.write_context(
                uri=uri,
                content=content,
                abstract=new_abstract,
                abstract_json=abstract_json,
                overview=new_overview,
                is_leaf=True,
            )
            logger.info(
                "[DerivationService] deferred derive completed for %s",
                uri,
            )
        except Exception as exc:
            logger.warning(
                "[DerivationService] deferred derive failed for %s: %s",
                uri,
                exc,
            )
            if raise_on_error:
                raise
        finally:
            self._deferred_derive_count -= 1

    @staticmethod
    def _fallback_overview_from_content(
        *,
        user_overview: str,
        content: str,
    ) -> str:
        """Delegate overview fallback to MemoryLayerDerivationService."""
        return MemoryLayerDerivationService._fallback_overview_from_content(
            user_overview=user_overview,
            content=content,
        )

    @staticmethod
    def _is_retryable_layer_derivation_error(exc: Exception) -> bool:
        """Delegate retry classification to MemoryLayerDerivationService."""
        return MemoryLayerDerivationService._is_retryable_layer_derivation_error(exc)

    async def _derive_layers_llm_completion(self, prompt: str) -> str:
        """Delegate layer-derivation LLM calls to MemoryLayerDerivationService."""
        return await self._layer_derivation_service._derive_layers_llm_completion(
            prompt
        )

    @staticmethod
    def _derive_abstract_from_overview(
        *,
        user_abstract: str,
        overview: str,
        content: str,
    ) -> str:
        """Delegate abstract fallback to MemoryLayerDerivationService."""
        return MemoryLayerDerivationService._derive_abstract_from_overview(
            user_abstract=user_abstract,
            overview=overview,
            content=content,
        )

    def _build_abstract_json(
        self,
        *,
        uri: str,
        context_type: str,
        category: str,
        abstract: str,
        overview: str,
        content: str,
        entities: List[str],
        meta: Optional[Dict[str, Any]],
        keywords: Optional[List[str]] = None,
        parent_uri: str,
        session_id: str,
    ) -> Dict[str, Any]:
        """Delegate to MemoryRecordService._build_abstract_json."""
        return self._memory_record_service._build_abstract_json(
            uri=uri,
            context_type=context_type,
            category=category,
            abstract=abstract,
            overview=overview,
            content=content,
            entities=entities,
            meta=meta,
            keywords=keywords,
            parent_uri=parent_uri,
            session_id=session_id,
        )

    @staticmethod
    def _memory_object_payload(
        abstract_json: Dict[str, Any],
        *,
        is_leaf: bool,
    ) -> Dict[str, Any]:
        """Delegate to MemoryRecordService._memory_object_payload."""
        from opencortex.services.memory_record_service import MemoryRecordService

        return MemoryRecordService._memory_object_payload(
            abstract_json, is_leaf=is_leaf
        )

    @staticmethod
    def _anchor_projection_prefix(uri: str) -> str:
        """Delegate to MemoryRecordService._anchor_projection_prefix."""
        from opencortex.services.memory_record_service import MemoryRecordService

        return MemoryRecordService._anchor_projection_prefix(uri)

    @staticmethod
    def _fact_point_prefix(uri: str) -> str:
        """Delegate to MemoryRecordService._fact_point_prefix."""
        from opencortex.services.memory_record_service import MemoryRecordService

        return MemoryRecordService._fact_point_prefix(uri)

    @staticmethod
    def _is_valid_fact_point(text: str) -> bool:
        """Delegate to MemoryRecordService._is_valid_fact_point."""
        from opencortex.services.memory_record_service import MemoryRecordService

        return MemoryRecordService._is_valid_fact_point(text)

    def _fact_point_records(
        self,
        *,
        source_record: Dict[str, Any],
        fact_points_list: List[str],
    ) -> List[Dict[str, Any]]:
        """Delegate to MemoryRecordService._fact_point_records."""
        return self._memory_record_service._fact_point_records(
            source_record=source_record,
            fact_points_list=fact_points_list,
        )

    def _anchor_projection_records(
        self,
        *,
        source_record: Dict[str, Any],
        abstract_json: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Delegate to MemoryRecordService._anchor_projection_records."""
        return self._memory_record_service._anchor_projection_records(
            source_record=source_record,
            abstract_json=abstract_json,
        )

    async def _delete_derived_stale(
        self,
        collection: str,
        prefix: str,
        keep_uris: set,
    ) -> None:
        """Delegate to MemoryRecordService._delete_derived_stale."""
        await self._memory_record_service._delete_derived_stale(
            collection,
            prefix,
            keep_uris,
        )

    async def _sync_anchor_projection_records(
        self,
        *,
        source_record: Dict[str, Any],
        abstract_json: Dict[str, Any],
    ) -> None:
        """Delegate to MemoryRecordService._sync_anchor_projection_records."""
        await self._memory_record_service._sync_anchor_projection_records(
            source_record=source_record,
            abstract_json=abstract_json,
        )
