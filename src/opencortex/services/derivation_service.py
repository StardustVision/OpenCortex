# SPDX-License-Identifier: Apache-2.0
"""Derivation domain service for OpenCortex.

This module owns LLM-backed derive, deferred derive completion, and
derived anchor/fact-point projection record synchronization. The
orchestrator keeps thin compatibility wrappers for existing callers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from uuid import uuid4

from opencortex.memory import (
    MemoryKind,
    memory_abstract_from_record,
    memory_anchor_hits_from_abstract,
    memory_kind_policy,
    memory_merge_signature_from_abstract,
)
from opencortex.prompts import (
    build_layer_abstract_prompt,
    build_layer_anchor_handles_prompt,
    build_layer_derivation_prompt,
    build_layer_entities_prompt,
    build_layer_fact_points_prompt,
    build_layer_keywords_prompt,
    build_layer_overview_only_prompt,
    build_parent_summarization_prompt,
)
from opencortex.utils.json_parse import parse_json_from_response
from opencortex.utils.text import chunked_llm_derive, smart_truncate

if TYPE_CHECKING:
    from opencortex.orchestrator import MemoryOrchestrator

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
    """Own derive-domain logic while using orchestrator-owned subsystems."""

    def __init__(self, orchestrator: MemoryOrchestrator) -> None:
        self._orch = orchestrator

    @property
    def _deferred_derive_count(self) -> int:
        return self._orch._deferred_derive_count

    @_deferred_derive_count.setter
    def _deferred_derive_count(self, value: int) -> None:
        self._orch._deferred_derive_count = value

    @property
    def _llm_completion(self) -> Any:
        return self._orch._llm_completion

    @property
    def _embedder(self) -> Any:
        return self._orch._embedder

    @property
    def _storage(self) -> Any:
        return self._orch._storage

    @property
    def _fs(self) -> Any:
        return self._orch._fs

    def _get_collection(self) -> str:
        return self._orch._get_collection()

    def _extract_category_from_uri(self, uri: str) -> str:
        return self._orch._extract_category_from_uri(uri)

    def _derive_parent_uri(self, uri: str) -> str:
        return self._orch._derive_parent_uri(uri)

    async def _get_record_by_uri(self, uri: str) -> Optional[Dict[str, Any]]:
        return await self._orch._get_record_by_uri(uri)

    async def _derive_parent_summary(
        self,
        doc_title: str,
        children_abstracts: List[str],
    ) -> Dict[str, Any]:
        """LLM-derive L1/L0 for a parent/section node from children abstracts."""
        if not self._llm_completion:
            return {}
        try:
            prompt = build_parent_summarization_prompt(doc_title, children_abstracts)
            response = await self._derive_layers_llm_completion(prompt)
            data = parse_json_from_response(response)
            if isinstance(data, dict):
                return {
                    "abstract": str(data.get("abstract") or "").strip()[:200],
                    "overview": str(data.get("overview") or "").strip(),
                    "keywords": data.get("keywords", []),
                }
        except Exception as exc:
            logger.warning(
                "[DerivationService] _derive_parent_summary failed for '%s': %s",
                doc_title,
                exc,
            )
        return {}

    async def _derive_layers(
        self,
        user_abstract: str,
        content: str,
        user_overview: str = "",
    ) -> Dict[str, str]:
        """Derive L0/L1/keywords from L2 with LLM assistance.

        Returns {"abstract": str, "overview": str, "keywords": str}
        keywords is a comma-separated string (for Qdrant MatchText).
        """
        # Fast path: user already provided both abstract and overview
        if user_abstract and user_overview:
            return {
                "abstract": user_abstract,
                "overview": user_overview,
                "keywords": "",
                "entities": [],
                "anchor_handles": [],
                "fact_points": [],
            }

        if self._llm_completion:
            if len(content) > 4000:
                try:
                    result = await chunked_llm_derive(
                        content=content,
                        prompt_builder=lambda chunk: build_layer_derivation_prompt(
                            chunk, user_abstract
                        ),
                        llm_fn=self._derive_layers_llm_completion,
                        parse_fn=parse_json_from_response,
                        max_chars_per_chunk=4000,
                    )
                    llm_overview = str(result.get("overview") or "").strip()
                    keywords_list = result.get("keywords", [])
                    if isinstance(keywords_list, list):
                        keywords = ", ".join(str(k) for k in keywords_list if k)
                    else:
                        keywords = str(keywords_list)
                    entities_list = result.get("entities", [])
                    if isinstance(entities_list, list):
                        entities = [str(e).strip().lower() for e in entities_list if e][
                            :20
                        ]
                    else:
                        entities = []
                    anchor_handles_list = result.get("anchor_handles", [])
                    if isinstance(anchor_handles_list, list):
                        anchor_handles = [
                            str(handle).strip()
                            for handle in anchor_handles_list
                            if str(handle).strip()
                        ][:6]
                    else:
                        anchor_handles = []
                    fact_points_list = result.get("fact_points", [])
                    if isinstance(fact_points_list, list):
                        fact_points = [
                            str(fp).strip()
                            for fp in fact_points_list
                            if str(fp).strip()
                        ][:8]
                    else:
                        fact_points = []
                    resolved_overview = self._fallback_overview_from_content(
                        user_overview=user_overview or llm_overview,
                        content=content,
                    )
                    derived_abstract = self._derive_abstract_from_overview(
                        user_abstract=user_abstract,
                        overview=resolved_overview,
                        content=content,
                    )
                    return {
                        "abstract": derived_abstract,
                        "overview": resolved_overview,
                        "keywords": keywords,
                        "entities": entities,
                        "anchor_handles": anchor_handles,
                        "fact_points": fact_points,
                    }
                except Exception as e:
                    logger.warning(
                        "[DerivationService] _derive_layers chunked LLM failed: %s", e
                    )
            try:
                return await self._derive_layers_split_fields(
                    user_abstract=user_abstract,
                    content=content,
                    user_overview=user_overview,
                )
            except Exception as e:
                logger.warning("[DerivationService] _derive_layers LLM failed: %s", e)

        # No-LLM fallback
        overview = self._fallback_overview_from_content(
            user_overview=user_overview,
            content=content,
        )
        abstract = self._derive_abstract_from_overview(
            user_abstract=user_abstract,
            overview=overview,
            content=content,
        )
        if not user_abstract and not self._llm_completion:
            logger.warning(
                "[DerivationService] No LLM configured — abstract uses raw content"
            )
        return {
            "abstract": abstract,
            "overview": overview,
            "keywords": "",
            "entities": [],
            "anchor_handles": [],
            "fact_points": [],
        }

    @staticmethod
    def _coerce_derived_string(value: str) -> str:
        """Normalize a derived string field."""
        return str(value or "").strip()

    @staticmethod
    def _coerce_derived_list(
        value: Any,
        *,
        limit: int,
        lowercase: bool = False,
    ) -> List[str]:
        """Normalize a derived list field."""
        if not isinstance(value, list):
            return []
        result: List[str] = []
        for item in value:
            normalized = str(item).strip()
            if not normalized:
                continue
            result.append(normalized.lower() if lowercase else normalized)
            if len(result) >= limit:
                break
        return result

    async def _derive_layers_split_fields(
        self,
        *,
        user_abstract: str,
        content: str,
        user_overview: str,
    ) -> Dict[str, Any]:
        """Derive memory fields with split prompts and bounded inner concurrency."""
        semaphore = asyncio.Semaphore(3)
        prompt_builders = {
            "abstract": build_layer_abstract_prompt,
            "overview": build_layer_overview_only_prompt,
            "keywords": build_layer_keywords_prompt,
            "entities": build_layer_entities_prompt,
            "anchor_handles": build_layer_anchor_handles_prompt,
            "fact_points": build_layer_fact_points_prompt,
        }

        async def _run_field(
            field_name: str, prompt: str
        ) -> tuple[str, Dict[str, Any]]:
            """Run a single LLM derivation prompt and return parsed JSON.

            Args:
                field_name: Name of the derived field (e.g. ``"abstract"``).
                prompt: Fully rendered LLM prompt string.

            Returns:
                Tuple of ``(field_name, parsed_dict)``.
            """
            async with semaphore:
                response = await self._derive_layers_llm_completion(prompt)
            parsed = parse_json_from_response(response)
            return field_name, parsed if isinstance(parsed, dict) else {}

        tasks = [
            asyncio.create_task(
                _run_field(
                    field_name,
                    prompt_builder(content, user_abstract),
                )
            )
            for field_name, prompt_builder in prompt_builders.items()
        ]
        parsed_results = await asyncio.gather(*tasks)
        derived_fields = {field_name: data for field_name, data in parsed_results}
        combined_values: Dict[str, Any] = {}
        for _, data in parsed_results:
            for field_name in prompt_builders:
                if field_name in data and field_name not in combined_values:
                    combined_values[field_name] = data[field_name]

        def _field_value(field_name: str) -> Any:
            """Return a field value, supporting old all-fields LLM payloads."""
            if field_name in combined_values:
                return combined_values[field_name]
            return derived_fields.get(field_name, {}).get(field_name)

        llm_abstract = self._coerce_derived_string(_field_value("abstract"))
        llm_overview = self._coerce_derived_string(_field_value("overview"))
        keywords = ", ".join(
            self._coerce_derived_list(
                _field_value("keywords"),
                limit=15,
            )
        )
        entities = self._coerce_derived_list(
            _field_value("entities"),
            limit=20,
            lowercase=True,
        )
        anchor_handles = self._coerce_derived_list(
            _field_value("anchor_handles"),
            limit=6,
        )
        fact_points = self._coerce_derived_list(
            _field_value("fact_points"),
            limit=8,
        )
        resolved_overview = self._fallback_overview_from_content(
            user_overview=user_overview or llm_overview,
            content=content,
        )
        derived_abstract = (
            user_abstract
            or llm_abstract
            or self._derive_abstract_from_overview(
                user_abstract=user_abstract,
                overview=resolved_overview,
                content=content,
            )
        )
        return {
            "abstract": derived_abstract,
            "overview": resolved_overview,
            "keywords": keywords,
            "entities": entities,
            "anchor_handles": anchor_handles,
            "fact_points": fact_points,
        }

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
        """Build a deterministic overview fallback when LLM output is absent."""
        if user_overview:
            return user_overview

        normalized_content = str(content or "").strip()
        if not normalized_content:
            return ""

        max_chars = min(max(len(normalized_content), 1), 1200)
        overview = smart_truncate(normalized_content, max_chars).strip()
        return overview or normalized_content[:max_chars].strip()

    @staticmethod
    def _is_retryable_layer_derivation_error(exc: Exception) -> bool:
        """Return whether one layer-derivation LLM failure is transient."""
        try:
            import httpx
        except ImportError:
            return False

        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code == 429 or exc.response.status_code >= 500
        return False

    async def _derive_layers_llm_completion(self, prompt: str) -> str:
        """Call layer-derivation LLM with a small bounded retry budget."""
        override = self._orch.__dict__.get("_derive_layers_llm_completion")
        if override is not None:
            return await override(prompt)
        if self._llm_completion is None:
            raise RuntimeError("LLM completion unavailable")

        retry_delays = (0.0, 0.35, 0.8)
        for attempt, delay in enumerate(retry_delays, start=1):
            if delay > 0.0:
                await asyncio.sleep(delay)
            try:
                return await self._llm_completion(prompt)
            except Exception as exc:
                if not self._is_retryable_layer_derivation_error(exc) or attempt == len(
                    retry_delays
                ):
                    raise
                logger.warning(
                    "[DerivationService] _derive_layers transient LLM failure "
                    "attempt=%d/%d: %s",
                    attempt,
                    len(retry_delays),
                    exc,
                )

        raise RuntimeError("unreachable")

    @staticmethod
    def _derive_abstract_from_overview(
        *,
        user_abstract: str,
        overview: str,
        content: str,
    ) -> str:
        """Derive a short abstract from a richer overview.

        Extracts the first sentence under ## Summary heading when present,
        otherwise falls back to the first line of the overview text.
        """
        if user_abstract:
            return user_abstract

        overview_text = str(overview or "").strip()
        if overview_text:
            # If overview uses Markdown headings, extract from ## Summary
            summary_text = ""
            in_summary = False
            for line in overview_text.splitlines():
                if line.strip() == "## Summary":
                    in_summary = True
                    continue
                if in_summary and line.strip().startswith("## "):
                    break
                if in_summary and line.strip():
                    summary_text = line.strip()
                    break
            if summary_text:
                first_sentence = re.split(r"(?<=[.!?。！？])\s+", summary_text)[
                    0
                ].strip()
                candidate = first_sentence or summary_text
                if len(candidate) > 200:
                    candidate = smart_truncate(candidate, 200).strip()
                if candidate:
                    return candidate

            # Fallback: first line of overview
            first_line = overview_text.splitlines()[0].strip()
            first_sentence = re.split(r"(?<=[.!?。！？])\s+", first_line)[0].strip()
            candidate = first_sentence or first_line
            if len(candidate) > 200:
                candidate = smart_truncate(candidate, 200).strip()
            if candidate:
                return candidate

        return str(content or "").strip()

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
        # Inject anchor_handles from LLM derivation into the anchors list
        # so that _memory_object_payload can project them into anchor_hits.
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
        """Quality gate: return True only if text is a short, concrete atomic fact.

        Rejects: too short (<8), too long (>80), multiline (paragraph-style),
        or generic text lacking any concrete signal.
        Accepts: text containing digits, CamelCase, ALLCAPS, paths, CJK sequences.
        """
        if not text or len(text) < 8 or len(text) > 80:
            return False
        if "\n" in text:
            return False
        # Must contain at least one concrete signal:
        # digits, CamelCase, ALL_CAPS, paths, or 2+ consecutive CJK characters
        concrete_signal = re.compile(
            r"[\d]"  # digit
            r"|[A-Z][a-z]+[A-Z]"  # CamelCase
            r"|[A-Z]{2,}"  # ALLCAPS (2+ uppercase)
            r"|[\u4e00-\u9fa5].*[\d]"  # CJK text with digit
            r"|[/\\.]"  # path separator
            r"|[\u4e00-\u9fa5]{2,}"  # 2+ consecutive CJK chars (Chinese proper nouns)
        )
        return bool(concrete_signal.search(text))

    def _fact_point_records(
        self,
        *,
        source_record: Dict[str, Any],
        fact_points_list: List[str],
    ) -> List[Dict[str, Any]]:
        """Build fact_point projection records for one leaf object.

        Applies quality gate and caps at 8 records.
        """
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
                # Inherit access control from source leaf
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
            # R11: skip anchors that are too short to be meaningful
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
        """Delete derived records under *prefix* whose URIs are not in *keep_uris*.

        This implements the write-then-delete contract: caller writes new records
        first, then calls this to remove only the records that were NOT just written.
        Records with matching URIs (same content → same digest) are kept.

        Filter DSL ``op=prefix`` on the Qdrant adapter is tokenised (MatchText)
        and over-matches when a sibling URI shares a literal substring with
        *prefix* (see tests/test_cascade_qdrant_integration.py,
        ``test_delete_derived_stale_does_not_touch_sibling_prefix``).  To avoid
        deleting sibling records that happen to token-match, every candidate
        URI is re-checked with literal ``startswith`` before it is marked
        stale.
        """
        try:
            old_records = await self._storage.filter(
                collection,
                {"op": "prefix", "field": "uri", "prefix": prefix},
                limit=50,
            )
        except Exception as exc:
            logger.warning(
                "[DerivationService] _delete_derived_stale filter failed prefix=%s: %s",
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
                    "[DerivationService] _delete_derived_stale delete failed: %s", exc
                )

    async def _sync_anchor_projection_records(
        self,
        *,
        source_record: Dict[str, Any],
        abstract_json: Dict[str, Any],
    ) -> None:
        """Replace derived anchor and fact_point records for one leaf object.

        Ordering: write-new-then-delete-old (R25).
        Both anchor projections and fact_points are embedded in a single
        embed_batch() call for efficiency.
        """
        if not bool(source_record.get("is_leaf", False)):
            return

        source_uri = str(source_record.get("uri", "") or "")
        if not source_uri:
            return

        anchor_prefix = self._anchor_projection_prefix(source_uri)
        fp_prefix = self._fact_point_prefix(source_uri)

        # Build new anchor records
        anchor_records = self._anchor_projection_records(
            source_record=source_record,
            abstract_json=abstract_json,
        )

        # Build new fact_point records from abstract_json
        raw_fact_points = abstract_json.get("fact_points") or []
        if not isinstance(raw_fact_points, list):
            raw_fact_points = []
        fp_records = self._fact_point_records(
            source_record=source_record,
            fact_points_list=raw_fact_points,
        )

        all_new_records = anchor_records + fp_records

        # REVIEW closure tracker R3-P-06 — short-circuit when the new
        # projection is empty AND there's no abstract_json input.
        # The defer_derive initial leaf write hits this path with both
        # inputs empty and would otherwise pay 2 stale-filter scans
        # over Qdrant prefixes that are guaranteed to be empty (the
        # leaf is brand new). The ``not abstract_json`` guard keeps
        # the cleanup path on legitimate update flows where
        # abstract_json is present but happens to contribute no
        # anchors or fact_points — those updates DO need stale
        # cleanup.
        if not all_new_records and not abstract_json:
            return

        # Embed all texts in a single batch call
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
                    "[DerivationService] derived records embed_batch failed: %s", exc
                )

        # Write new records FIRST (write-then-delete)
        for new_record in all_new_records:
            await self._storage.upsert(self._get_collection(), new_record)

        # Only THEN delete stale records (those NOT in the new set)
        new_anchor_uris = {r["uri"] for r in anchor_records}
        new_fp_uris = {r["uri"] for r in fp_records}
        collection = self._get_collection()
        await self._delete_derived_stale(collection, anchor_prefix, new_anchor_uris)
        await self._delete_derived_stale(collection, fp_prefix, new_fp_uris)
