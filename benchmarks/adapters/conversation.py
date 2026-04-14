"""LongMemEval conversation benchmark adapter."""

from __future__ import annotations

import json
import time
from hashlib import md5
from typing import Any, Dict, List, Tuple

from benchmarks.adapters.base import EvalAdapter, IngestResult, QAItem


LME_QUESTION_TYPES = {
    "single-session-user": "Single-Session (User)",
    "single-session-assistant": "Single-Session (Assistant)",
    "single-session-preference": "Single-Session (Preference)",
    "multi-session": "Multi-Session",
    "temporal-reasoning": "Temporal Reasoning",
    "knowledge-update": "Knowledge Update",
}


class LongMemEvalBench(EvalAdapter):
    """LongMemEval benchmark implementation."""

    def __init__(self) -> None:
        super().__init__()
        self._retrieve_method = "search"
        self._lme_session_map: Dict[str, Dict[str, Any]] = {}
        self._lme_session_to_uri: Dict[str, str] = {}

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

    async def ingest(self, oc: Any, **kwargs) -> IngestResult:
        """Ingest each unique LongMemEval session as a single memory item."""
        qa_items = self._dataset
        max_qa = kwargs.get("max_qa", 0)
        if max_qa > 0:
            qa_items = qa_items[:max_qa]

        unique_sessions: Dict[str, Dict[str, Any]] = {}
        for item in qa_items:
            session_ids = item.get("haystack_session_ids", [])
            sessions = item.get("haystack_sessions", [])
            dates = item.get("haystack_dates", [])
            for index, session_id in enumerate(session_ids):
                if session_id in unique_sessions:
                    continue
                unique_sessions[session_id] = {
                    "messages": sessions[index] if index < len(sessions) else [],
                    "date": dates[index] if index < len(dates) else "",
                    "session_id": session_id,
                }

        self._lme_session_map = unique_sessions
        self._lme_session_to_uri = {}
        errors: List[str] = []
        ingested = 0

        for session_id, session in unique_sessions.items():
            messages = session["messages"]
            date = str(session["date"])
            parts = []
            for message in messages:
                if isinstance(message, dict) and message.get("role") and message.get("content"):
                    parts.append(f"{message['role']}: {message['content']}")
            if not parts:
                continue

            session_text = "\n".join(parts)
            first_user = next(
                (
                    str(message["content"])[:200]
                    for message in messages
                    if isinstance(message, dict) and message.get("role") == "user"
                ),
                "",
            )
            abstract = f"[{date}] {first_user}" if date else first_user
            if len(abstract) > 300:
                abstract = abstract[:297] + "..."

            try:
                result = await oc.store(
                    abstract=abstract,
                    content=session_text,
                    category="conversation",
                    embed_text=first_user or session_text[:500],
                    meta={
                        "session_id": session_id,
                        "date": date,
                        "num_messages": len(messages),
                        "source": "longmemeval",
                    },
                )
                uri = result.get("uri", "")
                if uri:
                    self._lme_session_to_uri[session_id] = uri
                ingested += 1
            except Exception as exc:
                errors.append(f"session={session_id}: {exc}")

        return IngestResult(
            total_items=len(unique_sessions),
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
                        "question_id": item.get("id", index),
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
            session_id = "ev-lme-" + md5(qa_item.question.encode()).hexdigest()[:12]
            result = await oc.context_recall(
                session_id=session_id,
                query=qa_item.question,
                limit=top_k,
                detail_level="l0",
            )
            self._set_last_retrieval_meta(result)
            results = result.get("memory", [])
        else:
            result = await oc.search_payload(query=qa_item.question, limit=top_k)
            self._set_last_retrieval_meta(result)
            results = result.get("results", [])

        latency_ms = (time.perf_counter() - started) * 1000
        return results, latency_ms
