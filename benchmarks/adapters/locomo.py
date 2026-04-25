"""LoCoMo benchmark adapter with conversation-level session ingestion.

Each LoCoMo ``conversation`` maps to one OpenCortex ``session_id``. The
dataset's inner ``session_N`` segments are used as sequential commit batches so
their temporal structure is preserved without splitting one long-lived
conversation into many isolated OpenCortex sessions.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from hashlib import md5
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from benchmarks.adapters.base import EvalAdapter, IngestResult, QAItem

_QUESTION_STOPWORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "to",
    "of",
    "in",
    "on",
    "at",
    "for",
    "from",
    "with",
    "did",
    "does",
    "do",
    "is",
    "was",
    "were",
    "what",
    "when",
    "where",
    "who",
    "why",
    "how",
    "would",
    "could",
    "should",
    "be",
    "been",
    "has",
    "have",
    "had",
    "will",
    "i",
    "me",
    "my",
    "you",
    "your",
    "her",
    "his",
    "their",
    "they",
}


def _parse_locomo_sessions(conv: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse LoCoMo sessions sorted chronologically."""
    raw = conv["conversation"]
    numbers = sorted(
        int(key.split("_")[1])
        for key in raw
        if key.startswith("session_") and "date_time" not in key
    )
    sessions: List[Dict[str, Any]] = []
    for number in numbers:
        key = f"session_{number}"
        if key not in raw:
            continue
        sessions.append(
            {
                "session_num": number,
                "date_time": raw.get(f"session_{number}_date_time", ""),
                "turns": raw[key],
            }
        )
    return sessions


def _turn_text(turn: Dict[str, Any]) -> str:
    """Render a LoCoMo turn into benchmark text."""
    text = str(turn.get("text", ""))
    blip_caption = turn.get("blip_caption")
    if blip_caption:
        text += f" [image: {blip_caption}]"
    return text


def _fmt_locomo_session(session: Dict[str, Any]) -> str:
    lines = [f"[{session['date_time']}]"]
    for turn in session["turns"]:
        lines.append(f"{turn['speaker']}: {_turn_text(turn)}")
    return "\n".join(lines)


def _get_locomo_speakers(sessions: List[Dict[str, Any]]) -> List[str]:
    seen: Dict[str, int] = {}
    for session in sessions:
        for turn in session["turns"]:
            speaker = str(turn.get("speaker", ""))
            seen[speaker] = seen.get(speaker, 0) + 1
    return sorted(seen, key=lambda item: -seen[item])


def _get_qa_answer(qa: Dict[str, Any]) -> str:
    if "answer" in qa:
        return str(qa["answer"])
    if "adversarial_answer" in qa:
        return str(qa["adversarial_answer"])
    return ""


def _normalize_locomo_datetime(raw_value: str) -> Tuple[str, List[str]]:
    """Normalize one LoCoMo session datetime for storage and anchor reuse."""
    normalized = str(raw_value or "").strip()
    if not normalized:
        return "", []

    refs = [normalized]
    try:
        parsed = datetime.strptime(normalized, "%I:%M %p on %d %B, %Y")
    except ValueError:
        return "", refs

    parsed = parsed.replace(tzinfo=timezone.utc)
    iso_datetime = parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
    iso_date = parsed.date().isoformat()
    human_date = f"{parsed.day} {parsed.strftime('%B')}, {parsed.year}"
    for value in (human_date, iso_date, iso_datetime):
        if value not in refs:
            refs.append(value)
    return iso_datetime, refs


def _flatten_evidence_tokens(value: Any) -> Iterable[str]:
    """Yield evidence tokens from nested LoCoMo evidence payloads."""
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _flatten_evidence_tokens(item)


def _evidence_session_numbers(qa: Dict[str, Any]) -> List[int]:
    """Extract session numbers referenced by LoCoMo evidence."""
    numbers: List[int] = []
    for token in _flatten_evidence_tokens(qa.get("evidence", [])):
        if not token.startswith("D") or ":" not in token:
            continue
        try:
            session_num = int(token[1:].split(":", 1)[0])
        except ValueError:
            continue
        if session_num not in numbers:
            numbers.append(session_num)
    return numbers


def _normalize_text_set(values: Iterable[Any]) -> Set[str]:
    """Normalize heterogeneous string values for exact-set matching."""
    normalized: Set[str] = set()
    for value in values:
        text = str(value or "").strip().lower()
        if text:
            normalized.add(text)
    return normalized


def _question_terms(question: str) -> List[str]:
    """Extract lightweight lexical terms from a QA question."""
    terms: List[str] = []
    for raw in str(question or "").lower().replace("?", " ").split():
        token = "".join(ch for ch in raw if ch.isalnum() or ch in {"-", "+"}).strip("-+")
        if len(token) < 3 or token in _QUESTION_STOPWORDS:
            continue
        if token not in terms:
            terms.append(token)
    return terms


def _question_phrases(question: str) -> List[str]:
    """Build simple multi-token phrases for lexical tie-breaking."""
    terms = _question_terms(question)
    phrases: List[str] = []
    for size in (3, 2):
        for index in range(len(terms) - size + 1):
            phrase = " ".join(terms[index : index + size])
            if phrase not in phrases:
                phrases.append(phrase)
    return phrases


def _record_match_text(record: Dict[str, Any]) -> str:
    """Flatten record text used for benchmark-side QA/leaf matching."""
    parts: List[str] = []
    for key in ("abstract", "overview", "content"):
        value = str(record.get(key, "") or "").strip()
        if value:
            parts.append(value.lower())

    meta = record.get("meta")
    if isinstance(meta, dict):
        for value in meta.get("time_refs") or []:
            normalized = str(value or "").strip().lower()
            if normalized:
                parts.append(normalized)

    abstract_json = record.get("abstract_json")
    if isinstance(abstract_json, dict):
        summary = str(abstract_json.get("summary", "") or "").strip().lower()
        if summary:
            parts.append(summary)
        for anchor in abstract_json.get("anchors") or []:
            if not isinstance(anchor, dict):
                continue
            normalized = str(anchor.get("text") or anchor.get("value") or "").strip().lower()
            if normalized:
                parts.append(normalized)

    return "\n".join(parts)


def _question_record_score(question: str, record: Dict[str, Any]) -> Tuple[int, int]:
    """Return lexical match score between QA question and one merged leaf."""
    haystack = _record_match_text(record)
    phrase_hits = sum(1 for phrase in _question_phrases(question) if phrase in haystack)
    term_hits = sum(1 for term in _question_terms(question) if term in haystack)
    return phrase_hits, term_hits


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


class LoCoMoBench(EvalAdapter):
    """LoCoMo benchmark implementation using conversation-level ingest."""

    def __init__(self) -> None:
        super().__init__()
        self._ingest_method = "mcp"
        self._retrieve_method = "recall"
        self._conversation_uris_by_id: Dict[str, List[str]] = {}
        self._conversation_by_id: Dict[str, Dict[str, Any]] = {}
        self._session_uris_by_key: Dict[Tuple[str, int], List[str]] = {}
        self._session_candidates_by_key: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}

    def load_dataset(self, dataset_path: str, **kwargs) -> None:
        with open(dataset_path, encoding="utf-8") as file_obj:
            raw = json.load(file_obj)
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            raise ValueError("LoCoMo dataset must be a non-empty JSON array")
        self._dataset = raw
        self._conversation_by_id = {
            str(conv.get("sample_id", index)): conv
            for index, conv in enumerate(self._dataset)
        }
        self._session_candidates_by_key = {}

    @staticmethod
    def _conversation_session_id(conv_id: str) -> str:
        return f"locomo-{conv_id}"

    @classmethod
    def _conversation_uri(cls, conv_id: str) -> str:
        return f"locomo-conversation://{cls._conversation_session_id(conv_id)}"

    def _selected_conversations(
        self,
        *,
        max_conv: int = 0,
        max_qa: int = 0,
    ) -> List[Dict[str, Any]]:
        conversations = list(self._dataset)
        if max_conv > 0:
            return conversations[:max_conv]
        if max_qa <= 0:
            return conversations

        selected: List[Dict[str, Any]] = []
        qa_count = 0
        for conv in conversations:
            selected.append(conv)
            qa_count += len(conv.get("qa", []))
            if qa_count >= max_qa:
                break
        return selected

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
        """Map inner LoCoMo sessions to final merged URIs."""
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
                        mapped[session_num].append((uri, width, 0, span[0] if span else 10**9))

        result: Dict[int, List[str]] = {}
        for session_num, candidates in mapped.items():
            if not candidates:
                result[session_num] = []
                continue
            ordered = sorted(
                candidates,
                key=lambda item: (-item[2], item[1], item[3], item[0]),
            )
            result[session_num] = [uri for uri, _, _, _ in ordered]
        return result

    @staticmethod
    def _select_best_session_uri(
        question: str,
        candidates: List[Dict[str, Any]],
    ) -> List[str]:
        """Choose one session leaf using question-aware lexical tie-breaking."""
        if not candidates:
            return []

        scored = [
            (_question_record_score(question, record), index, record)
            for index, record in enumerate(candidates)
        ]
        scored.sort(
            key=lambda record: (
                -record[0][0],
                -record[0][1],
                record[1],
            ),
        )
        best_uri = str(scored[0][2].get("uri", "") or "")
        return [best_uri] if best_uri else []

    async def ingest(self, oc: Any, **kwargs) -> IngestResult:
        """Ingest LoCoMo conversations via one session lifecycle each.

        For ingest_method='store' the per-conversation work is independent
        (each conversation has its own session_id, source_uri, and merged
        leaves) so we dispatch with bounded concurrency to amortize the
        ~30-60s of LLM + embed time per conversation. The 'mcp' path uses
        the legacy serial loop because each conversation's
        context_commit / context_end touches the live conversation buffer.
        """
        conversations = self._selected_conversations(
            max_conv=kwargs.get("max_conv", 0),
            max_qa=kwargs.get("max_qa", 0),
        )
        ingest_method = str(
            kwargs.get("ingest_method") or getattr(self, "_ingest_method", "mcp")
        ).lower()
        ingest_concurrency = max(
            1, int(kwargs.get("ingest_concurrency", getattr(self, "_ingest_concurrency", 4)))
        )
        self._conversation_uris_by_id = {}
        self._session_uris_by_key = {}
        self._session_candidates_by_key = {}

        total = len(conversations)
        ingested = 0
        errors: List[str] = []
        # Local concurrency knob; mcp path stays serial. Semaphore is
        # only consulted by store-path dispatch below.
        semaphore = asyncio.Semaphore(
            ingest_concurrency if ingest_method == "store" else 1
        )

        async def _process_one(conv: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
            conv_id = str(conv.get("sample_id", id(conv)))
            conversation_session_id = self._conversation_session_id(conv_id)
            committed_segments = 0
            next_msg_index = 0
            session_spans: Dict[int, Tuple[int, int]] = {}
            session_time_refs: Dict[int, Set[str]] = {}
            segments: List[List[Dict[str, Any]]] = []
            async with semaphore:
                try:
                    if ingest_method == "mcp":
                        before_records = await self._memory_record_snapshot(oc)
                    for session in _parse_locomo_sessions(conv):
                        session_num = int(session["session_num"])
                        normalized_event_date, time_refs = _normalize_locomo_datetime(
                            str(session.get("date_time", ""))
                        )

                        messages = [
                            {
                                "role": "user",
                                "content": f"[{turn.get('speaker', 'unknown')}]: {_turn_text(turn)}",
                                "meta": {
                                    "speaker": str(turn.get("speaker", "unknown")),
                                    **(
                                        {"event_date": normalized_event_date}
                                        if normalized_event_date
                                        else {}
                                    ),
                                    **({"time_refs": list(time_refs)} if time_refs else {}),
                                },
                            }
                            for turn in session["turns"]
                            if _turn_text(turn)
                        ]
                        if not messages:
                            continue

                        start_index = next_msg_index
                        end_index = start_index + len(messages) - 1
                        session_spans[session_num] = (start_index, end_index)
                        session_time_refs[session_num] = _normalize_text_set(
                            [normalized_event_date, *time_refs]
                        )
                        next_msg_index = end_index + 1

                        if ingest_method == "store":
                            segments.append(messages)
                        else:
                            await oc.context_commit(
                                session_id=conversation_session_id,
                                turn_id=f"turn-{session_num}",
                                messages=messages,
                            )
                        committed_segments += 1

                    if committed_segments == 0:
                        return (False, None)

                    if ingest_method == "store":
                        # Benchmark scoring does not consume session_summary
                        # leaves; opting out shaves ~1 LLM call + 2 filter
                        # scans + 1 add per conversation. Direct API callers
                        # keep the default-True behavior.
                        payload = await oc.benchmark_conversation_ingest(
                            session_id=conversation_session_id,
                            segments=segments,
                            include_session_summary=False,
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
                    new_uris = sorted(new_records)
                    self._conversation_uris_by_id[conv_id] = (
                        new_uris or [self._conversation_uri(conv_id)]
                    )
                    session_uris_by_num = self._map_session_uris(
                        session_spans=session_spans,
                        session_time_refs=session_time_refs,
                        records_by_uri=new_records,
                        conversation_session_id=conversation_session_id,
                    )
                    for session_num, uris in session_uris_by_num.items():
                        self._session_uris_by_key[(conv_id, session_num)] = list(uris)
                        self._session_candidates_by_key[(conv_id, session_num)] = [
                            dict(new_records[uri])
                            for uri in uris
                            if uri in new_records
                        ]
                    return (True, None)
                except Exception as exc:
                    return (False, f"conv={conv_id}: {exc}")

        results = await asyncio.gather(
            *[_process_one(conv) for conv in conversations],
            return_exceptions=False,
        )
        for ok, err in results:
            if ok:
                ingested += 1
            elif err:
                errors.append(err)

        return IngestResult(
            total_items=total,
            ingested_items=ingested,
            errors=errors,
            meta={
                "benchmark_flavor": "recall-eval"
                if self._retrieve_method == "recall"
                else "internal",
                "ingest_method": ingest_method,
            },
        )

    @classmethod
    def _resolve_evidence_texts(
        cls,
        conv: Dict[str, Any],
        qa: Dict[str, Any],
    ) -> List[str]:
        """Resolve LoCoMo evidence tokens (D{n}:{idx}) to actual turn text."""
        sessions = _parse_locomo_sessions(conv)
        session_by_num = {s["session_num"]: s for s in sessions}
        texts: List[str] = []
        for token in _flatten_evidence_tokens(qa.get("evidence", [])):
            if not token.startswith("D") or ":" not in token:
                texts.append(str(token))
                continue
            try:
                parts = token[1:].split(":", 1)
                session_num = int(parts[0])
                turn_idx = int(parts[1]) - 1  # dia_id is 1-based
            except (ValueError, IndexError):
                texts.append(str(token))
                continue
            session = session_by_num.get(session_num)
            if not session or turn_idx >= len(session["turns"]):
                texts.append(str(token))
                continue
            turn = session["turns"][turn_idx]
            turn_text = _turn_text(turn)
            if turn_text:
                texts.append(f"{turn.get('speaker', '')}: {turn_text}")
        return texts

    def build_qa_items(self, **kwargs) -> List[QAItem]:
        conversations = self._selected_conversations(
            max_conv=kwargs.get("max_conv", 0),
            max_qa=kwargs.get("max_qa", 0),
        )

        items: List[QAItem] = []
        for conv in conversations:
            conv_id = str(conv.get("sample_id", len(items)))
            for index, qa in enumerate(conv.get("qa", [])):
                evidence_sessions = _evidence_session_numbers(qa)
                expected_uris: List[str] = []
                for session_num in evidence_sessions:
                    session_candidates = self._session_candidates_by_key.get(
                        (conv_id, session_num),
                        [],
                    )
                    expected_uris.extend(
                        self._select_best_session_uri(qa.get("question", ""), session_candidates)
                        or self._session_uris_by_key.get((conv_id, session_num), [])
                    )
                if not expected_uris:
                    expected_uris = self._conversation_uris_by_id.get(conv_id) or [
                        self._conversation_uri(conv_id)
                    ]
                evidence_texts = self._resolve_evidence_texts(conv, qa)
                items.append(
                    QAItem(
                        question=str(qa.get("question", "")),
                        answer=_get_qa_answer(qa),
                        category=str(qa.get("category", "")),
                        expected_ids=[str(num) for num in evidence_sessions],
                        expected_uris=sorted(set(expected_uris)),
                        meta={
                            "conv_id": conv_id,
                            "question_id": f"{conv_id}_q{index}",
                            "dataset": "locomo",
                            "evidence_sessions": evidence_sessions,
                            "evidence_texts": evidence_texts,
                        },
                    )
                )

        max_qa = kwargs.get("max_qa", 0)
        if max_qa > 0:
            items = items[:max_qa]
        return items

    def get_baseline_context(self, qa_item: QAItem) -> str:
        conv = self._conversation_by_id.get(str(qa_item.meta.get("conv_id", "")))
        if not conv:
            return ""
        sessions = _parse_locomo_sessions(conv)
        speakers = _get_locomo_speakers(sessions)
        header = f"Conversation between {' and '.join(speakers)}\n\n"
        return header + "\n\n".join(_fmt_locomo_session(session) for session in sessions)

    async def retrieve(
        self,
        oc: Any,
        qa_item: QAItem,
        top_k: int,
    ) -> Tuple[List[Dict[str, Any]], float]:
        """Retrieve LoCoMo sessions via search or production context recall."""
        started = time.perf_counter()
        conv_id = str(qa_item.meta.get("conv_id", ""))
        session_id = self._conversation_session_id(conv_id)

        if self._retrieve_method == "search":
            result = await oc.search_payload(
                query=qa_item.question,
                limit=top_k,
                context_type="memory",
                detail_level="l2",
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
            raw_items = result.get("results", [])
        else:
            result = await oc.context_recall(
                session_id=session_id,
                turn_id="q-" + md5(qa_item.question.encode()).hexdigest()[:12],
                query=qa_item.question,
                limit=top_k,
                session_scope=True,
            )
            self._set_last_retrieval_meta(
                result,
                endpoint="context_recall",
                session_scope=True,
            )
            raw_items = result.get("memory", [])

        deduped: List[Dict[str, Any]] = []
        seen_uris: Set[str] = set()
        for item in raw_items:
            uri = str(item.get("uri", "") or "")
            if not uri or uri in seen_uris:
                continue
            seen_uris.add(uri)
            normalized = dict(item)
            normalized["uri"] = uri
            deduped.append(normalized)

        latency_ms = (time.perf_counter() - started) * 1000
        return deduped, latency_ms
