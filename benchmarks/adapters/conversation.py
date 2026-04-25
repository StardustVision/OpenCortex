"""LongMemEval conversation benchmark adapter."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from benchmarks.adapters.base import EvalAdapter, IngestResult, QAItem

LME_QUESTION_TYPES = {
    "single-session-user": "Single-Session (User)",
    "single-session-assistant": "Single-Session (Assistant)",
    "single-session-preference": "Single-Session (Preference)",
    "multi-session": "Multi-Session",
    "temporal-reasoning": "Temporal Reasoning",
    "knowledge-update": "Knowledge Update",
}

_MAINSTREAM_INGEST_METHODS = {
    "longmemeval-mainstream",
    "mainstream",
    "pairs",
    "recall-eval",
}


def _normalize_text_set(values: Iterable[Any]) -> Set[str]:
    """Normalize heterogeneous string values for exact-set matching."""
    normalized: Set[str] = set()
    for value in values:
        text = str(value or "").strip().lower()
        if text:
            normalized.add(text)
    return normalized


def _message_span(record: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """Return a record's message span from the top-level ``msg_range`` contract."""
    raw_range = record.get("msg_range")
    if not isinstance(raw_range, list) or len(raw_range) != 2:
        return None
    try:
        start = int(raw_range[0])
        end = int(raw_range[1])
    except (TypeError, ValueError):
        return None
    if start > end:
        return None
    return start, end


def _ranges_overlap(left: Tuple[int, int], right: Tuple[int, int]) -> bool:
    """Return whether two inclusive ranges overlap."""
    return max(left[0], right[0]) <= min(left[1], right[1])


def _overlap_width(left: Tuple[int, int], right: Tuple[int, int]) -> int:
    """Return inclusive overlap width for two spans."""
    if not _ranges_overlap(left, right):
        return 0
    return min(left[1], right[1]) - max(left[0], right[0]) + 1


def _record_time_refs(record: Dict[str, Any]) -> Set[str]:
    """Extract normalized temporal anchors from a memory list payload."""
    values: List[Any] = []
    meta = record.get("meta")
    if isinstance(meta, dict):
        values.extend(meta.get("time_refs") or [])
        values.append(meta.get("event_date"))

    abstract_json = record.get("abstract_json")
    if isinstance(abstract_json, dict):
        slots = abstract_json.get("slots")
        if isinstance(slots, dict):
            values.extend(slots.get("time_refs") or [])

    values.append(record.get("event_date"))
    return _normalize_text_set(values)


class LongMemEvalBench(EvalAdapter):
    """LongMemEval benchmark implementation."""

    def __init__(self) -> None:
        super().__init__()
        self._ingest_method = "longmemeval-mainstream"
        self._retrieve_method = "search"
        self._lme_session_to_uri: Dict[str, str] = {}
        self._selected_indices: List[int] = []

    def load_dataset(self, dataset_path: str, **kwargs: Any) -> None:
        """Load LongMemEval JSON dataset."""
        with open(dataset_path, encoding="utf-8") as file_obj:
            raw = json.load(file_obj)
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            raise ValueError("LongMemEval dataset must be a non-empty JSON array")
        first = raw[0]
        if "haystack_sessions" not in first and "sessions" not in first:
            raise ValueError(
                "LongMemEval dataset must include 'haystack_sessions' or 'sessions'"
            )
        self._dataset = raw

    @staticmethod
    def _select_dataset_indices(
        dataset: List[Dict[str, Any]],
        *,
        max_qa: int = 0,
        per_type: int = 0,
    ) -> List[int]:
        """Select LongMemEval item indices consistently for ingest and QA."""
        indices: List[int] = []
        per_type_counts: Dict[str, int] = {}
        for index, item in enumerate(dataset):
            question_type = str(item.get("question_type", "unknown") or "unknown")
            if per_type > 0:
                count = per_type_counts.get(question_type, 0)
                if count >= per_type:
                    continue
                per_type_counts[question_type] = count + 1
            indices.append(index)
            if max_qa > 0 and len(indices) >= max_qa:
                break
        return indices

    @staticmethod
    def _message_payload(
        message: Dict[str, Any],
        *,
        date: str,
        item_index: int,
        session_index: int,
        session_id: str,
        segment_index: int,
        segment_kind: str,
    ) -> Optional[Dict[str, Any]]:
        """Normalize one LongMemEval haystack message for benchmark ingest."""
        role = str(message.get("role", "") or "").strip()
        content = str(message.get("content", "") or "").strip()
        if not role or not content:
            return None
        meta = {
            **({"event_date": date} if date else {}),
            **({"time_refs": [date]} if date else {}),
            "lme_item_index": item_index,
            "lme_session_index": session_index,
            "lme_session_id": session_id,
            "lme_segment_index": segment_index,
            "lme_segment_kind": segment_kind,
        }
        return {"role": role, "content": content, "meta": meta}

    @classmethod
    def _session_messages(
        cls,
        *,
        session_messages: List[Dict[str, Any]],
        date: str,
        item_index: int,
        session_index: int,
        session_id: str,
    ) -> List[Dict[str, Any]]:
        """Normalize a complete haystack session."""
        messages: List[Dict[str, Any]] = []
        for message_index, message in enumerate(session_messages):
            if not isinstance(message, dict):
                continue
            payload = cls._message_payload(
                message,
                date=date,
                item_index=item_index,
                session_index=session_index,
                session_id=session_id,
                segment_index=message_index,
                segment_kind="session",
            )
            if payload:
                messages.append(payload)
        return messages

    @classmethod
    def _pair_segments(
        cls,
        *,
        session_messages: List[Dict[str, Any]],
        date: str,
        item_index: int,
        session_index: int,
        session_id: str,
    ) -> List[List[Dict[str, Any]]]:
        """Split one haystack session into user/assistant evidence pairs."""
        normalized: List[Dict[str, Any]] = []
        for message in session_messages:
            if isinstance(message, dict):
                payload = cls._message_payload(
                    message,
                    date=date,
                    item_index=item_index,
                    session_index=session_index,
                    session_id=session_id,
                    segment_index=0,
                    segment_kind="pair",
                )
                if payload:
                    normalized.append(payload)

        segments: List[List[Dict[str, Any]]] = []
        cursor = 0
        while cursor < len(normalized):
            segment = [normalized[cursor]]
            if cursor + 1 < len(normalized):
                segment.append(normalized[cursor + 1])
            segment_index = len(segments)
            for message in segment:
                message["meta"]["lme_segment_index"] = segment_index
            segments.append(segment)
            cursor += 2
        return segments

    @staticmethod
    async def _memory_record_snapshot(oc: Any) -> Dict[str, Dict[str, Any]]:
        """Snapshot current memory records for diff-based ground-truth mapping."""
        offset = 0
        limit = 500
        records_by_uri: Dict[str, Dict[str, Any]] = {}
        while True:
            payload = await oc.memory_list(
                context_type="memory",
                category="events",
                limit=limit,
                offset=offset,
                include_payload=True,
            )
            results = payload.get("results", [])
            for item in results:
                uri = str(item.get("uri", "") or "")
                if uri:
                    records_by_uri[uri] = dict(item)
            if len(results) < limit:
                break
            offset += limit
        return records_by_uri

    @classmethod
    def _map_session_uris(
        cls,
        *,
        session_spans: Dict[int, Tuple[int, int]],
        session_time_refs: Dict[int, Set[str]],
        records_by_uri: Dict[str, Dict[str, Any]],
        conversation_session_id: str,
    ) -> Dict[int, List[str]]:
        """Map inner LongMemEval sessions to final merged URIs."""
        relevant_records: Dict[str, Dict[str, Any]] = {}
        for uri, record in records_by_uri.items():
            record_session_id = str(record.get("session_id", "") or "")
            if record_session_id and record_session_id != conversation_session_id:
                continue
            relevant_records[uri] = record

        mapped: Dict[int, List[Tuple[str, int, int, int]]] = {
            session_num: [] for session_num in session_spans
        }
        unmatched_records: Dict[str, Dict[str, Any]] = {}

        for uri, record in relevant_records.items():
            span = _message_span(record)
            if span is None:
                unmatched_records[uri] = record
                continue
            width = span[1] - span[0]
            for session_num, session_span in session_spans.items():
                if not _ranges_overlap(span, session_span):
                    continue
                mapped[session_num].append(
                    (uri, width, _overlap_width(span, session_span), span[0])
                )

        if unmatched_records:
            for session_num, time_refs in session_time_refs.items():
                if mapped[session_num] or not time_refs:
                    continue
                for uri, record in unmatched_records.items():
                    if time_refs.intersection(_record_time_refs(record)):
                        span = _message_span(record)
                        width = span[1] - span[0] if span is not None else 10**9
                        mapped[session_num].append(
                            (uri, width, 0, span[0] if span else 10**9)
                        )

        result: Dict[int, List[str]] = {}
        for session_num, candidates in mapped.items():
            if not candidates:
                result[session_num] = []
                continue
            best_uri, _, _, _ = sorted(
                candidates,
                key=lambda item: (-item[2], item[1], item[3], item[0]),
            )[0]
            result[session_num] = [best_uri]
        return result

    async def ingest(self, oc: Any, **kwargs: Any) -> IngestResult:
        """Ingest each LongMemEval item using mainstream or internal mode."""
        max_qa = int(kwargs.get("max_qa", 0) or 0)
        per_type = int(kwargs.get("per_type", 0) or 0)
        ingest_method = str(
            kwargs.get("ingest_method") or getattr(self, "_ingest_method", "mcp")
        ).lower()
        selected_indices = self._select_dataset_indices(
            self._dataset,
            max_qa=max_qa,
            per_type=per_type,
        )
        self._selected_indices = selected_indices
        self._lme_session_to_uri = {}
        errors: List[str] = []
        ingested = 0

        for item_index in selected_indices:
            item = self._dataset[item_index]
            conversation_session_id = f"lme-item-{item_index}"
            session_ids = item.get("haystack_session_ids", [])
            sessions = item.get("haystack_sessions", [])
            dates = item.get("haystack_dates", [])

            try:
                next_msg_index = 0
                session_spans: Dict[int, Tuple[int, int]] = {}
                session_time_refs: Dict[int, Set[str]] = {}
                committed_segments = 0
                segments: List[List[Dict[str, Any]]] = []

                if ingest_method == "mcp":
                    before_records = await self._memory_record_snapshot(oc)

                for session_index, session_messages in enumerate(sessions):
                    if not isinstance(session_messages, list):
                        continue
                    date = (
                        str(dates[session_index]) if session_index < len(dates) else ""
                    )
                    lme_session_id = (
                        str(session_ids[session_index])
                        if session_index < len(session_ids)
                        else str(session_index)
                    )

                    if ingest_method in _MAINSTREAM_INGEST_METHODS:
                        session_segments = self._pair_segments(
                            session_messages=session_messages,
                            date=date,
                            item_index=item_index,
                            session_index=session_index,
                            session_id=lme_session_id,
                        )
                        messages = [
                            message
                            for segment in session_segments
                            for message in segment
                        ]
                        segments.extend(session_segments)
                    else:
                        messages = self._session_messages(
                            session_messages=session_messages,
                            date=date,
                            item_index=item_index,
                            session_index=session_index,
                            session_id=lme_session_id,
                        )

                    if not messages:
                        continue

                    start_index = next_msg_index
                    end_index = start_index + len(messages) - 1
                    session_spans[session_index] = (start_index, end_index)
                    session_time_refs[session_index] = _normalize_text_set(
                        [date] if date else []
                    )
                    next_msg_index = end_index + 1

                    if ingest_method == "store":
                        segments.append(messages)
                    elif ingest_method == "mcp":
                        await oc.context_commit(
                            session_id=conversation_session_id,
                            turn_id=f"turn-{session_index}",
                            messages=messages,
                        )
                    committed_segments += 1

                if committed_segments == 0:
                    continue

                if ingest_method in _MAINSTREAM_INGEST_METHODS:
                    payload = await oc.benchmark_conversation_ingest(
                        session_id=conversation_session_id,
                        segments=segments,
                        include_session_summary=False,
                        ingest_shape="direct_evidence",
                    )
                    new_records = {
                        str(record.get("uri", "") or ""): dict(record)
                        for record in payload.get("records", [])
                        if str(record.get("uri", "") or "")
                    }
                elif ingest_method == "store":
                    payload = await oc.benchmark_conversation_ingest(
                        session_id=conversation_session_id,
                        segments=segments,
                    )
                    new_records = {
                        str(record.get("uri", "") or ""): dict(record)
                        for record in payload.get("records", [])
                        if str(record.get("uri", "") or "")
                    }
                else:
                    await oc.context_end(session_id=conversation_session_id)
                    after_records = await self._memory_record_snapshot(oc)
                    new_records = {
                        uri: record
                        for uri, record in after_records.items()
                        if uri not in before_records
                    }

                session_uris_by_index = self._map_session_uris(
                    session_spans=session_spans,
                    session_time_refs=session_time_refs,
                    records_by_uri=new_records,
                    conversation_session_id=conversation_session_id,
                )

                for session_index, session_id in enumerate(session_ids):
                    uris = session_uris_by_index.get(session_index, [])
                    if session_id not in self._lme_session_to_uri and uris:
                        self._lme_session_to_uri[session_id] = uris[0]

                ingested += 1
            except Exception as exc:
                errors.append(f"item={item_index}: {exc}")

        return IngestResult(
            total_items=len(selected_indices),
            ingested_items=ingested,
            errors=errors,
            meta={
                "benchmark_flavor": "recall-eval"
                if ingest_method == "recall-eval"
                else "mainstream"
                if ingest_method in _MAINSTREAM_INGEST_METHODS
                else "internal",
                "ingest_shape": "direct_evidence"
                if ingest_method in _MAINSTREAM_INGEST_METHODS
                else ingest_method,
                "selected_indices": selected_indices,
                "per_type": per_type,
            },
        )

    @staticmethod
    def _extract_evidence_texts(item: Dict[str, Any]) -> List[str]:
        """Extract evidence texts from has_answer turns in answer sessions."""
        sessions = item.get("haystack_sessions", [])
        session_ids = item.get("haystack_session_ids", [])
        answer_session_ids = set(item.get("answer_session_ids") or [])
        if not answer_session_ids:
            return []

        evidence: List[str] = []
        for idx, session in enumerate(sessions):
            sid = session_ids[idx] if idx < len(session_ids) else ""
            if sid not in answer_session_ids:
                continue
            for msg in session:
                if not isinstance(msg, dict):
                    continue
                if msg.get("has_answer") and msg.get("content"):
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    evidence.append(f"{role}: {content}" if role else content)
        return evidence

    def build_qa_items(self, **kwargs: Any) -> List[QAItem]:
        """Build LongMemEval QA items from selected dataset rows."""
        max_qa = int(kwargs.get("max_qa", 0) or 0)
        per_type = int(kwargs.get("per_type", 0) or 0)
        selected_indices = (
            list(self._selected_indices)
            if self._selected_indices
            else self._select_dataset_indices(
                self._dataset,
                max_qa=max_qa,
                per_type=per_type,
            )
        )

        items: List[QAItem] = []
        for index in selected_indices:
            item = self._dataset[index]
            expected_session_ids = item.get("answer_session_ids") or item.get(
                "haystack_session_ids",
                [],
            )
            expected_uris = [
                self._lme_session_to_uri[session_id]
                for session_id in expected_session_ids
                if session_id in self._lme_session_to_uri
            ]
            raw_question_type = str(item.get("question_type", ""))
            question_id = str(item.get("question_id", index))
            evidence_texts = self._extract_evidence_texts(item)
            items.append(
                QAItem(
                    question=str(item.get("question", "")),
                    answer=str(item.get("answer", "")),
                    category=LME_QUESTION_TYPES.get(
                        raw_question_type,
                        raw_question_type or "unknown",
                    ),
                    difficulty=str(item.get("difficulty", "")),
                    expected_ids=[
                        str(session_id) for session_id in expected_session_ids
                    ],
                    expected_uris=expected_uris,
                    meta={
                        "question_id": question_id,
                        "question_type": raw_question_type,
                        "item_index": index,
                        "dataset": "longmemeval",
                        **(
                            {"evidence_texts": evidence_texts} if evidence_texts else {}
                        ),
                    },
                )
            )
        return items

    def get_baseline_context(self, qa_item: QAItem) -> str:
        """All haystack sessions concatenated in chronological order."""
        target_question = None
        for item in self._dataset:
            if str(item.get("question", "")) == qa_item.question:
                target_question = item
                break
        if not target_question:
            return ""

        sessions = target_question.get("haystack_sessions", [])
        dates = target_question.get("haystack_dates", [])
        parts: List[str] = []
        for index, session in enumerate(sessions):
            date = dates[index] if index < len(dates) else ""
            header = f"[{date}]" if date else ""
            lines = [header] if header else []
            for message in session:
                if isinstance(message, dict):
                    role = message.get("role", "unknown")
                    content = message.get("content", "")
                    lines.append(f"{role}: {content}")
            parts.append("\n".join(lines))
        return "\n\n---\n\n".join(part for part in parts if part.strip())

    async def retrieve(
        self,
        oc: Any,
        qa_item: QAItem,
        top_k: int,
    ) -> Tuple[List[Dict[str, Any]], float]:
        """Retrieve LongMemEval sessions."""
        started = time.perf_counter()
        if self._retrieve_method == "recall":
            item_index = int(qa_item.meta.get("item_index", 0))
            session_id = f"lme-item-{item_index}"
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
        else:
            item_index = int(qa_item.meta.get("item_index", 0))
            session_id = f"lme-item-{item_index}"
            metadata_filter = {
                "op": "must",
                "field": "session_id",
                "conds": [session_id],
            }
            result = await oc.search_payload(
                query=qa_item.question,
                limit=top_k,
                context_type="memory",
                detail_level="l2",
                metadata_filter=metadata_filter,
            )
            self._set_last_retrieval_meta(
                result,
                endpoint="memory_search",
                session_scope=True,
            )
            results = result.get("results", [])

        latency_ms = (time.perf_counter() - started) * 1000
        return results, latency_ms
