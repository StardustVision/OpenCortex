# SPDX-License-Identifier: Apache-2.0
"""Pure LLM-backed memory layer derivation service."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List

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
    from opencortex.cortex_memory import CortexMemory

logger = logging.getLogger(__name__)


class MemoryLayerDerivationService:
    """Derive memory layer fields from content without persistence side effects."""

    def __init__(self, orchestrator: CortexMemory) -> None:
        self._orch = orchestrator

    @property
    def _llm_completion(self) -> Any:
        return self._orch._llm_completion

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
                "[MemoryLayerDerivationService] _derive_parent_summary failed "
                "for '%s': %s",
                doc_title,
                exc,
            )
        return {}

    async def _derive_layers(
        self,
        user_abstract: str,
        content: str,
        user_overview: str = "",
    ) -> Dict[str, Any]:
        """Derive L0/L1/keywords from L2 with LLM assistance."""
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
                except Exception as exc:
                    logger.warning(
                        "[MemoryLayerDerivationService] _derive_layers chunked "
                        "LLM failed: %s",
                        exc,
                    )
            try:
                return await self._derive_layers_split_fields(
                    user_abstract=user_abstract,
                    content=content,
                    user_overview=user_overview,
                )
            except Exception as exc:
                logger.warning(
                    "[MemoryLayerDerivationService] _derive_layers LLM failed: %s",
                    exc,
                )

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
                "[MemoryLayerDerivationService] No LLM configured - abstract uses "
                "raw content"
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
            """Run a single LLM derivation prompt and return parsed JSON."""
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
                    "[MemoryLayerDerivationService] _derive_layers transient LLM "
                    "failure attempt=%d/%d: %s",
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
        """Derive a short abstract from a richer overview."""
        if user_abstract:
            return user_abstract

        overview_text = str(overview or "").strip()
        if overview_text:
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

            first_line = overview_text.splitlines()[0].strip()
            first_sentence = re.split(r"(?<=[.!?。！？])\s+", first_line)[0].strip()
            candidate = first_sentence or first_line
            if len(candidate) > 200:
                candidate = smart_truncate(candidate, 200).strip()
            if candidate:
                return candidate

        return str(content or "").strip()
