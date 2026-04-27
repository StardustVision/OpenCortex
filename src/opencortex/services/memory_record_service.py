# SPDX-License-Identifier: Apache-2.0
"""Memory record shape, projection, and URI service for OpenCortex.

This service owns canonical memory record payload construction, derived
anchor/fact-point projection records, stale derived-record cleanup, TTL helpers,
and URI utilities. The orchestrator keeps compatibility wrappers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from uuid import uuid4

from opencortex.http.request_context import (
    get_effective_identity,
    get_effective_project_id,
)
from opencortex.memory import (
    MemoryKind,
    memory_abstract_from_record,
    memory_anchor_hits_from_abstract,
    memory_kind_policy,
    memory_merge_signature_from_abstract,
)
from opencortex.services.derivation_service import _merge_unique_strings
from opencortex.utils.uri import CortexURI

if TYPE_CHECKING:
    from opencortex.orchestrator import MemoryOrchestrator

logger = logging.getLogger(__name__)


class MemoryRecordService:
    """Own memory record/projection/URI behavior using orchestrator subsystems."""

    def __init__(self, orchestrator: "MemoryOrchestrator") -> None:
        self._orch = orchestrator

    @property
    def _storage(self) -> Any:
        return self._orch._storage

    @property
    def _embedder(self) -> Any:
        return self._orch._embedder

    def _get_collection(self) -> str:
        return self._orch._get_collection()

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
        """Build the fixed shared `.abstract.json` payload for one entry."""
        record = {
            "uri": uri,
            "context_type": context_type,
            "category": category,
            "abstract": abstract,
            "overview": overview,
            "content": content,
            "entities": entities,
            "keywords": keywords or [],
            "metadata": meta or {},
            "parent_uri": parent_uri,
            "session_id": session_id,
        }
        result = memory_abstract_from_record(record).to_dict()
        meta_dict = meta or {}
        anchor_handles = meta_dict.get("anchor_handles")
        if anchor_handles:
            existing_values = {
                a.get("value", "").lower()
                for a in result.get("anchors") or []
                if isinstance(a, dict)
            }
            for handle in anchor_handles:
                if (
                    isinstance(handle, str)
                    and handle.strip()
                    and handle.lower() not in existing_values
                ):
                    result.setdefault("anchors", []).append(
                        {
                            "anchor_type": "handle",
                            "value": handle.strip(),
                            "text": handle.strip(),
                        }
                    )
                    existing_values.add(handle.lower())
        return result

    @staticmethod
    def _memory_object_payload(
        abstract_json: Dict[str, Any],
        *,
        is_leaf: bool,
    ) -> Dict[str, Any]:
        """Project canonical abstract payload into flat vector metadata."""
        memory_kind = MemoryKind(str(abstract_json["memory_kind"]))
        policy = memory_kind_policy(memory_kind)
        anchor_hits = memory_anchor_hits_from_abstract(abstract_json)
        return {
            "memory_kind": memory_kind.value,
            "anchor_hits": anchor_hits,
            "merge_signature": memory_merge_signature_from_abstract(abstract_json),
            "mergeable": policy.mergeable,
            "retrieval_surface": "l0_object" if is_leaf else "",
            "anchor_surface": bool(is_leaf and anchor_hits),
        }

    @staticmethod
    def _anchor_projection_prefix(uri: str) -> str:
        """Return the reserved child prefix for derived anchor projection records."""
        return f"{uri}/anchors"

    @staticmethod
    def _fact_point_prefix(uri: str) -> str:
        """Return the reserved child prefix for derived fact point records."""
        return f"{uri}/fact_points"

    @staticmethod
    def _is_valid_fact_point(text: str) -> bool:
        """Return True only if text is a short, concrete atomic fact."""
        if not text or len(text) < 8 or len(text) > 80:
            return False
        if "\n" in text:
            return False
        concrete_signal = re.compile(
            r"[\d]"
            r"|[A-Z][a-z]+[A-Z]"
            r"|[A-Z]{2,}"
            r"|[\u4e00-\u9fa5].*[\d]"
            r"|[/\\.]"
            r"|[\u4e00-\u9fa5]{2,}"
        )
        return bool(concrete_signal.search(text))

    def _fact_point_records(
        self,
        *,
        source_record: Dict[str, Any],
        fact_points_list: List[str],
    ) -> List[Dict[str, Any]]:
        """Build fact_point projection records for one leaf object."""
        source_uri = str(source_record.get("uri", "") or "")
        if not source_uri:
            return []

        prefix = self._fact_point_prefix(source_uri)
        records: List[Dict[str, Any]] = []

        for text in fact_points_list:
            if len(records) >= 8:
                break
            if not self._is_valid_fact_point(text):
                continue
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
            fp_record = {
                "id": uuid4().hex,
                "uri": f"{prefix}/{digest}",
                "parent_uri": source_uri,
                "is_leaf": False,
                "abstract": "",
                "overview": text,
                "content": "",
                "retrieval_surface": "fact_point",
                "anchor_surface": False,
                "meta": {
                    "derived": True,
                    "derived_kind": "fact_point",
                    "projection_target_uri": source_uri,
                },
                "projection_target_uri": source_uri,
                "context_type": source_record.get("context_type", ""),
                "category": source_record.get("category", ""),
                "scope": source_record.get("scope", ""),
                "source_user_id": source_record.get("source_user_id", ""),
                "source_tenant_id": source_record.get("source_tenant_id", ""),
                "session_id": source_record.get("session_id", ""),
                "project_id": source_record.get("project_id", ""),
                "memory_kind": source_record.get("memory_kind", ""),
                "source_doc_id": source_record.get("source_doc_id", ""),
                "source_doc_title": source_record.get("source_doc_title", ""),
                "source_section_path": source_record.get("source_section_path", ""),
                "keywords": text,
                "entities": source_record.get("entities", []),
                "mergeable": False,
                "merge_signature": "",
                "anchor_hits": "",
            }
            records.append(fp_record)

        return records

    def _anchor_projection_records(
        self,
        *,
        source_record: Dict[str, Any],
        abstract_json: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Build dedicated anchor projection records for one leaf object."""
        source_uri = str(source_record.get("uri", "") or "")
        if not source_uri:
            return []

        projection_records: List[Dict[str, Any]] = []
        anchors = abstract_json.get("anchors") or []
        prefix = self._anchor_projection_prefix(source_uri)
        base_anchor_hits = memory_anchor_hits_from_abstract(abstract_json)

        for index, anchor in enumerate(anchors):
            if not isinstance(anchor, dict):
                continue
            anchor_text = str(anchor.get("text") or anchor.get("value") or "").strip()
            anchor_value = str(anchor.get("value") or anchor_text).strip()
            anchor_type = str(anchor.get("anchor_type") or "topic").strip() or "topic"
            if not anchor_text:
                continue
            if len(anchor_text) < 4:
                continue

            digest = hashlib.sha1(
                f"{anchor_type}:{anchor_value}:{index}".encode("utf-8")
            ).hexdigest()[:12]
            projection_uri = f"{prefix}/{digest}"
            projection_record = {
                "id": uuid4().hex,
                "uri": projection_uri,
                "parent_uri": source_uri,
                "is_leaf": False,
                "abstract": "",
                "overview": (
                    anchor_text
                    if len(anchor_text) >= 15
                    else f"{anchor_type}: {anchor_text}"
                ),
                "content": "",
                "context_type": source_record.get("context_type", ""),
                "category": source_record.get("category", ""),
                "scope": source_record.get("scope", ""),
                "source_user_id": source_record.get("source_user_id", ""),
                "source_tenant_id": source_record.get("source_tenant_id", ""),
                "session_id": source_record.get("session_id", ""),
                "project_id": source_record.get("project_id", ""),
                "keywords": ", ".join(
                    value for value in [anchor_text, anchor_value] if value
                ),
                "entities": source_record.get("entities", []),
                "meta": {
                    "derived": True,
                    "derived_kind": "anchor_projection",
                    "anchor_type": anchor_type,
                    "anchor_value": anchor_value,
                    "anchor_text": anchor_text,
                    "projection_target_uri": source_uri,
                },
                "memory_kind": source_record.get("memory_kind", ""),
                "anchor_hits": _merge_unique_strings(
                    anchor_text, anchor_value, *base_anchor_hits
                ),
                "merge_signature": "",
                "mergeable": False,
                "retrieval_surface": "anchor_projection",
                "anchor_surface": True,
                "source_doc_id": source_record.get("source_doc_id", ""),
                "source_doc_title": source_record.get("source_doc_title", ""),
                "source_section_path": source_record.get("source_section_path", ""),
                "chunk_role": source_record.get("chunk_role", ""),
                "speaker": source_record.get("speaker", ""),
                "event_date": source_record.get("event_date"),
                "projection_target_uri": source_uri,
                "projection_target_abstract": source_record.get("abstract", ""),
                "projection_target_overview": source_record.get("overview", ""),
            }
            projection_records.append(projection_record)

        return projection_records

    async def _delete_derived_stale(
        self,
        collection: str,
        prefix: str,
        keep_uris: set,
    ) -> None:
        """Delete derived records under *prefix* whose URIs are not in *keep_uris*."""
        try:
            old_records = await self._storage.filter(
                collection,
                {"op": "prefix", "field": "uri", "prefix": prefix},
                limit=50,
            )
        except Exception as exc:
            logger.warning(
                "[MemoryRecordService] _delete_derived_stale filter failed "
                "prefix=%s: %s",
                prefix,
                exc,
            )
            return
        descendant_prefix = prefix if prefix.endswith("/") else prefix + "/"
        stale_ids = [
            str(r["id"])
            for r in old_records
            if isinstance(r.get("uri"), str)
            and (r["uri"] == prefix or r["uri"].startswith(descendant_prefix))
            and r["uri"] not in keep_uris
        ]
        if stale_ids:
            try:
                await self._storage.delete(collection, stale_ids)
            except Exception as exc:
                logger.warning(
                    "[MemoryRecordService] _delete_derived_stale delete failed: %s",
                    exc,
                )

    async def _sync_anchor_projection_records(
        self,
        *,
        source_record: Dict[str, Any],
        abstract_json: Dict[str, Any],
    ) -> None:
        """Replace derived anchor and fact_point records for one leaf object."""
        if not bool(source_record.get("is_leaf", False)):
            return

        source_uri = str(source_record.get("uri", "") or "")
        if not source_uri:
            return

        anchor_prefix = self._anchor_projection_prefix(source_uri)
        fp_prefix = self._fact_point_prefix(source_uri)
        anchor_records = self._anchor_projection_records(
            source_record=source_record,
            abstract_json=abstract_json,
        )

        raw_fact_points = abstract_json.get("fact_points") or []
        if not isinstance(raw_fact_points, list):
            raw_fact_points = []
        fp_records = self._fact_point_records(
            source_record=source_record,
            fact_points_list=raw_fact_points,
        )

        all_new_records = anchor_records + fp_records
        if not all_new_records and not abstract_json:
            return

        if all_new_records and self._embedder:
            texts = [r["overview"] for r in all_new_records]
            loop = asyncio.get_running_loop()
            try:
                embed_results = await asyncio.wait_for(
                    loop.run_in_executor(None, self._embedder.embed_batch, texts),
                    timeout=5.0,
                )
                for record, embed_result in zip(
                    all_new_records, embed_results, strict=False
                ):
                    if embed_result.dense_vector:
                        record["vector"] = embed_result.dense_vector
                    if getattr(embed_result, "sparse_vector", None):
                        record["sparse_vector"] = embed_result.sparse_vector
            except Exception as exc:
                logger.warning(
                    "[MemoryRecordService] derived records embed_batch failed: %s",
                    exc,
                )

        for new_record in all_new_records:
            await self._storage.upsert(self._get_collection(), new_record)

        new_anchor_uris = {r["uri"] for r in anchor_records}
        new_fp_uris = {r["uri"] for r in fp_records}
        collection = self._get_collection()
        await self._delete_derived_stale(collection, anchor_prefix, new_anchor_uris)
        await self._delete_derived_stale(collection, fp_prefix, new_fp_uris)

    @staticmethod
    def _ttl_from_hours(hours: int) -> str:
        """Return RFC3339 UTC expiry string. Non-positive values disable TTL."""
        if hours <= 0:
            return ""
        expires = datetime.now(timezone.utc) + timedelta(hours=hours)
        return expires.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _auto_uri(self, context_type: str, category: str, abstract: str = "") -> str:
        """Generate a URI based on context type, category, and abstract text."""
        from opencortex.utils.semantic_name import semantic_node_name

        tid, uid = get_effective_identity()
        node_name = semantic_node_name(abstract) if abstract else uuid4().hex

        if context_type == "memory":
            categories = self._orch._USER_MEMORY_CATEGORIES
            cat = category if category in categories else "events"
            return CortexURI.build_private(tid, uid, "memories", cat, node_name)

        if context_type == "case":
            return CortexURI.build_shared(tid, "shared", "cases", node_name)

        if context_type == "pattern":
            return CortexURI.build_shared(tid, "shared", "patterns", node_name)

        if context_type == "resource":
            project = get_effective_project_id()
            if category:
                return CortexURI.build_shared(
                    tid, "resources", project, category, node_name
                )
            return CortexURI.build_shared(tid, "resources", project, node_name)

        if context_type == "staging":
            return CortexURI.build_private(tid, uid, "staging", node_name)

        return CortexURI.build_private(tid, uid, "memories", "events", node_name)

    async def _uri_exists(self, uri: str) -> bool:
        """Check if a URI already exists in the context collection."""
        try:
            results = await self._storage.filter(
                self._get_collection(),
                {"op": "must", "field": "uri", "conds": [uri]},
                limit=1,
            )
            return len(results) > 0
        except Exception:
            return False

    async def _resolve_unique_uri(self, uri: str, max_attempts: int = 100) -> str:
        """Ensure URI is unique, appending _1, _2, ... if needed."""
        if not await self._orch._uri_exists(uri):
            return uri
        for i in range(1, max_attempts + 1):
            candidate = f"{uri}_{i}"
            if not await self._orch._uri_exists(candidate):
                return candidate
        raise ValueError(
            f"URI conflict unresolved after {max_attempts} attempts: {uri}"
        )

    @staticmethod
    def _extract_category_from_uri(uri: str) -> str:
        """Extract category from URI path."""
        parts = uri.split("/")
        for parent in (
            "memories",
            "cases",
            "patterns",
            "skills",
            "staging",
            "resources",
        ):
            if parent in parts:
                idx = parts.index(parent)
                if parent in ("cases", "patterns"):
                    return parent
                if parent == "resources":
                    cat_idx = idx + 2
                    if cat_idx < len(parts):
                        candidate = parts[cat_idx]
                        if len(candidate) != 12:
                            return candidate
                    continue
                if idx + 1 < len(parts):
                    candidate = parts[idx + 1]
                    if len(candidate) != 12:
                        return candidate
        return ""

    @staticmethod
    def _enrich_abstract(abstract: str, content: str) -> str:
        """Append missing hard keywords when the abstract has poor term coverage."""
        if not content.strip():
            return abstract

        term_pattern = re.compile(
            r"[a-z]+[A-Z][a-zA-Z0-9]*|\b[A-Z]{2,}\b|[a-zA-Z0-9]+[_./-][a-zA-Z0-9]+"
        )
        candidates: list[str] = []
        seen: set[str] = set()
        for match in term_pattern.finditer(content):
            term = match.group(0)
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(term)

        if not candidates:
            return abstract

        abstract_lower = abstract.lower()
        covered = [term for term in candidates if term.lower() in abstract_lower]
        if len(covered) / len(candidates) >= 0.6:
            return abstract

        missing = [term for term in candidates if term.lower() not in abstract_lower][
            :10
        ]
        if not missing:
            return abstract
        return f"{abstract} [{', '.join(missing)}]"

    @staticmethod
    def _derive_parent_uri(uri: str) -> str:
        """Derive parent URI by removing the last path segment."""
        try:
            parsed = CortexURI(uri)
            parent = parsed.parent
            return str(parent) if parent else ""
        except ValueError:
            return ""
