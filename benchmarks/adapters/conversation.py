"""LongMemEval conversation benchmark adapter."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, Set, Tuple

from benchmarks.adapters import conversation_mapping as cm
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
_CONTEXT_LIFECYCLE_INGEST_METHOD = "context_lifecycle"


def _normalize_ingest_method(value: str) -> str:
    """Normalize deprecated benchmark ingest method aliases."""
    if value == "mcp":
        return _CONTEXT_LIFECYCLE_INGEST_METHOD
    return value


class LongMemEvalBench(EvalAdapter):
    """LongMemEval benchmark implementation."""

    def __init__(self) -> None:
        super().__init__()
        self._ingest_method = "longmemeval-mainstream"
        self._retrieve_method = "search"
        self._lme_session_to_uri: Dict[str, str] = {}
        self._selected_indices: List[int] = []

    def _validate_dataset(self, raw: Any) -> None:
        if isinstance(raw, dict):
            raw = [raw]
            self._dataset = raw
        if not isinstance(raw, list) or not raw:
            raise ValueError("LongMemEval dataset must be a non-empty JSON array")
        first = raw[0]
        if "haystack_sessions" not in first and "sessions" not in first:
            raise ValueError(
                "LongMemEval dataset must include 'haystack_sessions' or 'sessions'"
            )

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

    async def ingest(self, oc: Any, **kwargs: Any) -> IngestResult:
        """Ingest each LongMemEval item using mainstream or internal mode.

        Mainstream and store paths run with bounded concurrency so the
        benchmark suite finishes in operationally feasible time. The context
        lifecycle path stays serial because it relies on per-session live
        buffers.
        """
        max_qa = int(kwargs.get("max_qa", 0) or 0)
        per_type = int(kwargs.get("per_type", 0) or 0)
        ingest_method = _normalize_ingest_method(
            str(
                kwargs.get("ingest_method")
                or getattr(self, "_ingest_method", _CONTEXT_LIFECYCLE_INGEST_METHOD)
            ).lower()
        )
        ingest_concurrency = max(
            1, int(kwargs.get("ingest_concurrency", getattr(self, "_ingest_concurrency", 4)))
        )
        selected_indices = self._select_dataset_indices(
            self._dataset,
            max_qa=max_qa,
            per_type=per_type,
        )
        self._selected_indices = selected_indices
        self._lme_session_to_uri = {}
        errors: List[str] = []
        ingested = 0
        # Store / mainstream paths get bounded concurrency; context lifecycle
        # stays serial because each context_commit mutates per-session state.
        concurrent_paths = _MAINSTREAM_INGEST_METHODS | {"store"}
        semaphore = asyncio.Semaphore(
            ingest_concurrency if ingest_method in concurrent_paths else 1
        )

        async def _process_one(item_index: int) -> Tuple[bool, Optional[str]]:
            item = self._dataset[item_index]
            conversation_session_id = f"lme-item-{item_index}"
            session_ids = item.get("haystack_session_ids", [])
            sessions = item.get("haystack_sessions", [])
            dates = item.get("haystack_dates", [])

            async with semaphore:
                try:
                    next_msg_index = 0
                    session_spans: Dict[int, Tuple[int, int]] = {}
                    session_time_refs: Dict[int, Set[str]] = {}
                    committed_segments = 0
                    segments: List[List[Dict[str, Any]]] = []

                    if ingest_method == _CONTEXT_LIFECYCLE_INGEST_METHOD:
                        before_records = await cm.memory_record_snapshot(oc)

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
                        session_time_refs[session_index] = cm.normalize_text_set(
                            [date] if date else []
                        )
                        next_msg_index = end_index + 1

                        if ingest_method == "store":
                            segments.append(messages)
                        elif ingest_method == _CONTEXT_LIFECYCLE_INGEST_METHOD:
                            await oc.context_commit(
                                session_id=conversation_session_id,
                                turn_id=f"turn-{session_index}",
                                messages=messages,
                            )
                        committed_segments += 1

                    if committed_segments == 0:
                        return (False, None)

                    if ingest_method in _MAINSTREAM_INGEST_METHODS:
                        payload = await oc.benchmark_conversation_ingest(
                            session_id=conversation_session_id,
                            segments=segments,
                            include_session_summary=False,
                            ingest_shape="direct_evidence",
                        )
                        new_records = cm.extract_records_by_uri(payload)
                    elif ingest_method == "store":
                        # Benchmark scoring does not consume session_summary
                        # leaves; opting out matches the mainstream branch
                        # above and shaves ~1 LLM call + 2 filter scans per
                        # conversation. Direct API callers keep default-True.
                        payload = await oc.benchmark_conversation_ingest(
                            session_id=conversation_session_id,
                            segments=segments,
                            include_session_summary=False,
                        )
                        new_records = cm.extract_records_by_uri(payload)
                    else:
                        await oc.context_end(session_id=conversation_session_id)
                        after_records = await cm.memory_record_snapshot(oc)
                        new_records = {
                            uri: record
                            for uri, record in after_records.items()
                            if uri not in before_records
                        }

                    session_uris_by_index = cm.map_session_uris(
                        session_spans=session_spans,
                        session_time_refs=session_time_refs,
                        records_by_uri=new_records,
                        conversation_session_id=conversation_session_id,
                        return_all=False,
                    )

                    for session_index, session_id in enumerate(session_ids):
                        uris = session_uris_by_index.get(session_index, [])
                        if session_id not in self._lme_session_to_uri and uris:
                            self._lme_session_to_uri[session_id] = uris[0]

                    return (True, None)
                except asyncio.CancelledError:
                    # Per-item cancellation must not cascade to siblings
                    # (REVIEW REL-04). Surface as a structured error.
                    return (False, f"item={item_index}: cancelled")
                except Exception as exc:
                    return (False, f"item={item_index}: {exc}")

        # ``return_exceptions=True`` keeps a single bad item from
        # aborting the rest of the benchmark run — the inner try/except
        # turns recoverable failures into structured tuples, and any
        # BaseException that does escape appears as an exception object
        # we surface alongside the structured results.
        results = await asyncio.gather(
            *[_process_one(idx) for idx in selected_indices],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                errors.append(f"item=?: {result!r}")
                continue
            ok, err = result
            if ok:
                ingested += 1
            elif err:
                errors.append(err)

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

    def _get_retrieval_session_id(self, qa_item: QAItem) -> str:
        return f"lme-item-{int(qa_item.meta.get('item_index', 0))}"

    def _get_retrieval_session_scope(self) -> bool:
        return True

    def _get_retrieval_context_type(self) -> str:
        return "memory"

    def _get_retrieval_detail_level(self) -> str:
        return "l2" if self._retrieve_method == "search" else "l0"
