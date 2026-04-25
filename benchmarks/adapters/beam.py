"""BEAM-like pressure benchmark adapter."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from benchmarks.adapters.base import EvalAdapter, IngestResult, QAItem


class BeamBench(EvalAdapter):
    """Adapter for BEAM-like bucketed long-memory pressure datasets."""

    def __init__(self) -> None:
        super().__init__()
        self._dataset: List[Dict[str, Any]] = []
        self._selected_indices: List[int] = []
        self._item_to_uris: Dict[int, List[str]] = {}
        self._beam_tier = ""
        self._retrieve_method = "recall"

    def load_dataset(self, dataset_path: str, **kwargs: Any) -> None:
        """Load BEAM-like JSON from a list or common dict wrapper shape."""
        with open(dataset_path, "r", encoding="utf-8") as file_obj:
            raw = json.load(file_obj)

        if isinstance(raw, list):
            records = raw
        elif isinstance(raw, dict):
            records = self._records_from_dict(raw)
        else:
            raise ValueError("BEAM dataset must be a JSON list or object")

        self._beam_tier = str(kwargs.get("beam_tier") or "").strip()
        self._dataset = [dict(record) for record in records if isinstance(record, dict)]
        self._selected_indices = self._select_indices(beam_tier=self._beam_tier)
        self._item_to_uris = {}

    @staticmethod
    def _records_from_dict(raw: Dict[str, Any]) -> List[Any]:
        """Extract records from common BEAM-like dict wrappers."""
        for key in ("data", "items", "records", "examples"):
            value = raw.get(key)
            if isinstance(value, list):
                return value
        return [raw]

    @staticmethod
    def _record_tier(record: Dict[str, Any]) -> str:
        """Return the pressure tier/bucket label for a record."""
        return str(
            record.get("beam_tier")
            or record.get("tier")
            or record.get("bucket")
            or ""
        )

    def _select_indices(
        self,
        *,
        beam_tier: str = "",
        max_qa: int = 0,
    ) -> List[int]:
        """Select records by optional BEAM tier and sample limit."""
        selected = [
            index
            for index, record in enumerate(self._dataset)
            if not beam_tier or self._record_tier(record) == beam_tier
        ]
        if max_qa > 0:
            selected = selected[:max_qa]
        return selected

    @staticmethod
    def _normalize_message(
        raw: Any,
        *,
        item_index: int,
        segment_index: int,
    ) -> Dict[str, Any]:
        """Normalize a BEAM haystack message into the benchmark conversation shape."""
        if isinstance(raw, dict):
            role = str(raw.get("role") or raw.get("speaker") or "user")
            content = str(raw.get("content") or raw.get("text") or raw.get("message") or "")
            meta = dict(raw.get("meta") or raw.get("metadata") or {})
        else:
            role = "user"
            content = str(raw or "")
            meta = {}

        meta.setdefault("beam_item_index", item_index)
        meta.setdefault("beam_segment_index", segment_index)
        return {"role": role, "content": content, "meta": meta}

    def _segments_for_record(
        self,
        record: Dict[str, Any],
        item_index: int,
    ) -> List[List[Dict[str, Any]]]:
        """Extract haystack sessions/messages into direct-evidence segments."""
        raw_segments = (
            record.get("haystack_sessions")
            or record.get("sessions")
            or record.get("haystack")
            or record.get("messages")
            or []
        )
        if isinstance(raw_segments, dict):
            raw_segments = list(raw_segments.values())
        if not isinstance(raw_segments, list):
            raw_segments = [raw_segments]

        if raw_segments and all(
            isinstance(item, dict) and not self._looks_like_message(item)
            for item in raw_segments
        ):
            raw_segments = [
                list(item.get("messages") or item.get("turns") or [])
                for item in raw_segments
            ]
        elif raw_segments and all(self._looks_like_message(item) for item in raw_segments):
            raw_segments = [raw_segments]

        bucket = str(record.get("bucket") or self._record_tier(record) or "")
        tier = str(record.get("tier") or self._record_tier(record) or "")
        segments: List[List[Dict[str, Any]]] = []
        for segment_index, raw_segment in enumerate(raw_segments):
            raw_messages = raw_segment if isinstance(raw_segment, list) else [raw_segment]
            messages = [
                self._normalize_message(
                    raw_message,
                    item_index=item_index,
                    segment_index=segment_index,
                )
                for raw_message in raw_messages
            ]
            for message in messages:
                meta = message.setdefault("meta", {})
                meta.setdefault("beam_bucket", bucket)
                meta.setdefault("beam_tier", tier)
                meta.setdefault("beam_dataset", "beam")
            non_empty = [message for message in messages if message.get("content")]
            if non_empty:
                segments.append(non_empty)
        return segments

    @staticmethod
    def _looks_like_message(value: Any) -> bool:
        """Return whether a value looks like one chat message."""
        return isinstance(value, dict) and any(
            key in value for key in ("content", "text", "message", "role", "speaker")
        )

    async def ingest(self, oc: Any, **kwargs: Any) -> IngestResult:
        """Ingest selected BEAM items as isolated direct-evidence conversations."""
        beam_tier = str(kwargs.get("beam_tier") or self._beam_tier or "").strip()
        max_qa = int(kwargs.get("max_qa", 0) or 0)
        selected_indices = self._select_indices(beam_tier=beam_tier, max_qa=max_qa)
        self._selected_indices = selected_indices
        self._item_to_uris = {}
        errors: List[str] = []
        ingested = 0

        for item_index in selected_indices:
            record = self._dataset[item_index]
            session_id = f"beam-item-{item_index}"
            segments = self._segments_for_record(record, item_index)
            if not segments:
                errors.append(f"item={item_index}: no haystack messages")
                continue
            try:
                payload = await oc.benchmark_conversation_ingest(
                    session_id=session_id,
                    segments=segments,
                    include_session_summary=False,
                    ingest_shape="direct_evidence",
                )
                self._item_to_uris[item_index] = [
                    str(item.get("uri") or "")
                    for item in payload.get("records", [])
                    if str(item.get("uri") or "")
                ]
                ingested += 1
            except Exception as exc:
                errors.append(f"item={item_index}: {exc}")

        return IngestResult(
            total_items=len(selected_indices),
            ingested_items=ingested,
            errors=errors,
            meta={
                "benchmark_flavor": "pressure",
                "ingest_shape": "direct_evidence",
                "selected_indices": selected_indices,
                "beam_tier": beam_tier,
            },
        )

    def build_qa_items(self, **kwargs: Any) -> List[QAItem]:
        """Build QA items from selected BEAM records."""
        beam_tier = str(kwargs.get("beam_tier") or self._beam_tier or "").strip()
        max_qa = int(kwargs.get("max_qa", 0) or 0)
        selected_indices = (
            self._select_indices(beam_tier=beam_tier, max_qa=max_qa)
            if beam_tier
            else self._selected_indices
            or self._select_indices(beam_tier=beam_tier, max_qa=max_qa)
        )

        items: List[QAItem] = []
        for item_index in selected_indices:
            record = self._dataset[item_index]
            bucket = str(record.get("bucket") or self._record_tier(record) or "")
            tier = str(record.get("tier") or self._record_tier(record) or "")
            question = str(record.get("question") or record.get("query") or "")
            answer = self._answer_text(record.get("answer") or record.get("answers"))
            items.append(
                QAItem(
                    question=question,
                    answer=answer,
                    category=bucket or tier or "beam",
                    expected_uris=list(self._item_to_uris.get(item_index, [])),
                    meta={
                        "dataset": "beam",
                        "item_index": item_index,
                        "beam_bucket": bucket,
                        "beam_tier": tier,
                        "question_id": str(
                            record.get("id")
                            or record.get("question_id")
                            or item_index
                        ),
                    },
                )
            )
        return items

    @staticmethod
    def _answer_text(value: Any) -> str:
        """Normalize BEAM answer variants into one answer string."""
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        if value is None:
            return ""
        return str(value)

    def get_baseline_context(self, qa_item: QAItem) -> str:
        """Return all haystack messages for the QA item's source record."""
        item_index = int(qa_item.meta.get("item_index", 0))
        if item_index < 0 or item_index >= len(self._dataset):
            return ""
        segments = self._segments_for_record(self._dataset[item_index], item_index)
        parts: List[str] = []
        for segment in segments:
            lines = []
            for message in segment:
                role = str(message.get("role") or "user")
                content = str(message.get("content") or "")
                lines.append(f"{role}: {content}" if role else content)
            if lines:
                parts.append("\n".join(lines))
        return "\n\n---\n\n".join(parts)

    async def retrieve(
        self,
        oc: Any,
        qa_item: QAItem,
        top_k: int,
    ) -> Tuple[List[Dict[str, Any]], float]:
        """Retrieve item-scoped BEAM memories via recall or raw search."""
        started = time.perf_counter()
        item_index = int(qa_item.meta.get("item_index", 0))
        session_id = f"beam-item-{item_index}"
        if self._retrieve_method == "search":
            result = await oc.search_payload(
                query=qa_item.question,
                limit=top_k,
                context_type="memory",
                metadata_filter={
                    "op": "must",
                    "field": "session_id",
                    "conds": [session_id],
                },
            )
            self._set_last_retrieval_meta(
                result,
                endpoint="memory_search",
                session_scope=True,
            )
            results = result.get("results", [])
        else:
            result = await oc.context_recall(
                session_id=session_id,
                query=qa_item.question,
                limit=top_k,
                detail_level="l0",
                session_scope=True,
            )
            self._set_last_retrieval_meta(
                result,
                endpoint="context_recall",
                session_scope=True,
            )
            results = result.get("memory", [])

        latency_ms = (time.perf_counter() - started) * 1000
        return results, latency_ms
