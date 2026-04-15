"""LongMemEval conversation benchmark adapter."""

from __future__ import annotations

import json
import time
from hashlib import md5
from typing import Any, Dict, Iterable, List, Set, Tuple

from benchmarks.adapters.base import EvalAdapter, IngestResult, QAItem


LME_QUESTION_TYPES = {
    "single-session-user": "Single-Session (User)",
    "single-session-assistant": "Single-Session (Assistant)",
    "single-session-preference": "Single-Session (Preference)",
    "multi-session": "Multi-Session",
    "temporal-reasoning": "Temporal Reasoning",
    "knowledge-update": "Knowledge Update",
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
    """Return a record's message span if the payload exposes ``meta.msg_range``."""
    meta = record.get("meta")
    if not isinstance(meta, dict):
        return None
    raw_range = meta.get("msg_range")
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
        self._retrieve_method = "search"
        self._lme_session_map: Dict[str, Dict[str, Any]] = {}
        self._lme_session_to_uri: Dict[str, str] = {}
        self._session_uris_by_key: Dict[Tuple[int, str], List[str]] = {}

    def load_dataset(self, dataset_path: str, **kwargs) -> None:
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

        mapped: Dict[int, List[Tuple[str, int]]] = {
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
                mapped[session_num].append((uri, width))

        if unmatched_records:
            for session_num, time_refs in session_time_refs.items():
                if mapped[session_num] or not time_refs:
                    continue
                for uri, record in unmatched_records.items():
                    if time_refs.intersection(_record_time_refs(record)):
                        span = _message_span(record)
                        width = span[1] - span[0] if span is not None else 10**9
                        mapped[session_num].append((uri, width))

        result: Dict[int, List[str]] = {}
        for session_num, candidates in mapped.items():
            if not candidates:
                result[session_num] = []
                continue
            min_width = min(width for _, width in candidates)
            result[session_num] = sorted(
                {uri for uri, width in candidates if width == min_width}
            )
        return result

    async def ingest(self, oc: Any, **kwargs) -> IngestResult:
        """Ingest each LongMemEval item via conversation mode (commit + end)."""
        qa_items = self._dataset
        max_qa = kwargs.get("max_qa", 0)
        if max_qa > 0:
            qa_items = qa_items[:max_qa]

        self._lme_session_map = {}
        self._lme_session_to_uri = {}
        self._session_uris_by_key = {}
        errors: List[str] = []
        ingested = 0

        for item_index, item in enumerate(qa_items):
            conversation_session_id = f"lme-item-{item_index}"
            session_ids = item.get("haystack_session_ids", [])
            sessions = item.get("haystack_sessions", [])
            dates = item.get("haystack_dates", [])

            try:
                before_records = await self._memory_record_snapshot(oc)
                next_msg_index = 0
                session_spans: Dict[int, Tuple[int, int]] = {}
                session_time_refs: Dict[int, Set[str]] = {}
                committed_segments = 0

                for session_index, session_messages in enumerate(sessions):
                    date = str(dates[session_index]) if session_index < len(dates) else ""
                    messages = []
                    for msg in session_messages:
                        if isinstance(msg, dict) and msg.get("role") and msg.get("content"):
                            messages.append({
                                "role": msg["role"],
                                "content": msg["content"],
                                "meta": {
                                    **({"event_date": date} if date else {}),
                                    **({"time_refs": [date]} if date else {}),
                                },
                            })

                    if not messages:
                        continue

                    start_index = next_msg_index
                    end_index = start_index + len(messages) - 1
                    session_spans[session_index] = (start_index, end_index)
                    session_time_refs[session_index] = _normalize_text_set([date] if date else [])
                    next_msg_index = end_index + 1

                    await oc.context_commit(
                        session_id=conversation_session_id,
                        turn_id=f"turn-{session_index}",
                        messages=messages,
                    )
                    committed_segments += 1

                if committed_segments == 0:
                    continue

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
                    key = (item_index, session_id)
                    self._session_uris_by_key[key] = list(uris)
                    if session_id not in self._lme_session_to_uri and uris:
                        self._lme_session_to_uri[session_id] = uris[0]

                ingested += 1
            except Exception as exc:
                errors.append(f"item={item_index}: {exc}")

        return IngestResult(
            total_items=len(qa_items),
            ingested_items=ingested,
            errors=errors,
        )

    def build_qa_items(self, **kwargs) -> List[QAItem]:
        items: List[QAItem] = []
        for index, item in enumerate(self._dataset):
            expected_session_ids = item.get("answer_session_ids") or item.get(
                "haystack_session_ids",
                [],
            )
            expected_uris = [
                self._lme_session_to_uri[session_id]
                for session_id in expected_session_ids
                if session_id in self._lme_session_to_uri
            ]
            items.append(
                QAItem(
                    question=str(item.get("question", "")),
                    answer=str(item.get("answer", "")),
                    category=LME_QUESTION_TYPES.get(
                        str(item.get("question_type", "")),
                        str(item.get("question_type", "unknown")),
                    ),
                    difficulty=str(item.get("difficulty", "")),
                    expected_ids=[str(session_id) for session_id in expected_session_ids],
                    expected_uris=expected_uris,
                    meta={
                        "question_id": item.get("question_id", index),
                        "item_index": index,
                        "dataset": "longmemeval",
                    },
                )
            )

        max_qa = kwargs.get("max_qa", 0)
        if max_qa > 0:
            items = items[:max_qa]
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
            result = await oc.search_payload(query=qa_item.question, limit=top_k)
            self._set_last_retrieval_meta(
                result,
                endpoint="memory_search",
                session_scope=False,
            )
            results = result.get("results", [])

        latency_ms = (time.perf_counter() - started) * 1000
        return results, latency_ms
