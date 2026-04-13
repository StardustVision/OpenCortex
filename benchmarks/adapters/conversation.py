"""
Conversation adapter for LoCoMo and LongMemEval datasets.

LoCoMo evaluation methodology (ACL 2024):
  - Ingest: Store **observations** (structured assertions about speakers)
    via oc.store(), not raw dialogue turns. Observations are the best-performing
    RAG retrieval unit per the paper. Each observation includes session date
    for temporal grounding.
  - Retrieve: oc.search() (memory retrieval), not context_recall.
  - Recall@k: evidence dia_ids in QA items map to stored observation URIs.
  - Category 5 (adversarial): excluded from overall F1 per paper protocol.

LongMemEval evaluation methodology (ICLR 2025):
  - Ingest: Store haystack sessions as memory items (full session text).
    Sessions are deduplicated across QA items by session_id.
  - Retrieve: oc.search() for relevant sessions.
  - Recall@k: answer_session_ids map to stored session URIs.
  - 6 question types: single-session-user/assistant/preference,
    multi-session, temporal-reasoning, knowledge-update.

Dataset detection: auto-detects LoCoMo vs LongMemEval from JSON structure.
"""

import json
import time
from hashlib import md5
from typing import Any, Dict, List, Optional, Tuple

from benchmarks.adapters.base import EvalAdapter, IngestResult, QAItem


# LongMemEval question type → display name
LME_QUESTION_TYPES = {
    "single-session-user": "Single-Session (User)",
    "single-session-assistant": "Single-Session (Assistant)",
    "single-session-preference": "Single-Session (Preference)",
    "multi-session": "Multi-Session",
    "temporal-reasoning": "Temporal Reasoning",
    "knowledge-update": "Knowledge Update",
}


def _parse_locomo_sessions(conv: Dict) -> List[Dict]:
    """Parse LoCoMo sessions sorted chronologically."""
    raw = conv["conversation"]
    nums = sorted(
        int(k.split("_")[1])
        for k in raw
        if k.startswith("session_") and "date_time" not in k
    )
    sessions = []
    for n in nums:
        key = f"session_{n}"
        if key not in raw:
            continue
        sessions.append({
            "session_num": n,
            "date_time": raw.get(f"session_{n}_date_time", ""),
            "turns": raw[key],
        })
    return sessions


def _fmt_locomo_session(session: Dict) -> str:
    lines = [f"[{session['date_time']}]"]
    for t in session["turns"]:
        text = t["text"]
        if "blip_caption" in t:
            text += f" [image: {t['blip_caption']}]"
        lines.append(f"{t['speaker']}: {text}")
    return "\n".join(lines)


def _get_locomo_speakers(sessions: List[Dict]) -> List[str]:
    seen: Dict[str, int] = {}
    for s in sessions:
        for t in s["turns"]:
            seen[t["speaker"]] = seen.get(t["speaker"], 0) + 1
    return sorted(seen, key=lambda x: -seen[x])


def _get_qa_answer(qa: Dict) -> str:
    if "answer" in qa:
        return str(qa["answer"])
    if "adversarial_answer" in qa:
        return str(qa["adversarial_answer"])
    return ""


def _collect_observations(conv: Dict) -> List[Dict]:
    """Extract all observations from a LoCoMo conversation.

    Returns list of dicts with keys: text, dia_id, speaker, session_num, session_date.
    """
    obs_data = conv.get("observation", {})
    conv_data = conv.get("conversation", {})
    event_data = conv.get("event_summary", {})
    results = []

    for sess_key, sess_obs in obs_data.items():
        if not isinstance(sess_obs, dict):
            continue
        # Extract session number from key like "session_1_observation"
        parts = sess_key.split("_")
        if len(parts) < 3:
            continue
        sess_num = int(parts[1])
        sess_date = conv_data.get(f"session_{sess_num}_date_time", "")

        # Also get event date if available
        events = event_data.get(f"events_session_{sess_num}", {})
        event_date = events.get("date", "") if isinstance(events, dict) else ""
        date_str = event_date or sess_date

        for speaker, items in sess_obs.items():
            for item in items:
                if isinstance(item, list) and len(item) >= 2:
                    text, dia_id = item[0], item[1]
                else:
                    continue
                results.append({
                    "text": text,
                    "dia_id": dia_id,
                    "speaker": speaker,
                    "session_num": sess_num,
                    "session_date": date_str,
                })

    return results


class ConversationAdapter(EvalAdapter):
    """LoCoMo / LongMemEval evaluation adapter."""

    def __init__(self):
        super().__init__()
        # Keyed by (conv_id, dia_id) — dia_ids are NOT unique across conversations.
        self._dia_id_to_uri: Dict[Tuple[str, str], str] = {}
        # MCP lifecycle: session_id → URI mapping
        self._session_id_to_uri: Dict[str, str] = {}
        self._retrieve_method: str = "search"

    def load_dataset(self, dataset_path: str, **kwargs) -> None:
        with open(dataset_path, encoding="utf-8") as f:
            raw = json.load(f)

        if isinstance(raw, list):
            self._dataset = raw
        elif isinstance(raw, dict):
            self._dataset = [raw]
        else:
            raise ValueError(f"Unexpected dataset format: {type(raw)}")

        # Detect dataset type from first entry
        first = self._dataset[0]
        if "conversation" in first and any(
            k.startswith("session_") for k in first.get("conversation", {})
        ):
            self._dataset_type = "locomo"
        elif "haystack_sessions" in first and "question_type" in first:
            self._dataset_type = "longmemeval"
            self._lme_session_map: Dict[str, Dict] = {}  # session_id → {messages, date}
        elif "sessions" in first:
            self._dataset_type = "longmemeval"
            self._lme_session_map: Dict[str, Dict] = {}
        else:
            raise ValueError(
                "Cannot detect dataset type. Expected LoCoMo (conversation.session_N) "
                "or LongMemEval (haystack_sessions / sessions)."
            )

    async def ingest(self, oc: Any, **kwargs) -> IngestResult:
        """Ingest observations (LoCoMo) or sessions (LongMemEval).

        Supports two ingest methods (via ingest_method kwarg):
          - 'store': Use oc.store() for each observation/session (default)
          - 'mcp': Use MCP lifecycle (prepare/commit/end) for conversations
        """
        max_conv = kwargs.get("max_conv", 0)
        max_qa = kwargs.get("max_qa", 0)
        ingest_method = kwargs.get("ingest_method", "store")

        total = 0
        ingested = 0
        errors: List[str] = []
        self._dia_id_to_uri = {}
        self._session_id_to_uri = {}
        self._lme_session_to_uri: Dict[str, str] = {}
        self._ingest_method = ingest_method

        if self._dataset_type == "locomo":
            conversations = self._dataset
            if max_conv > 0:
                conversations = conversations[:max_conv]

            if ingest_method == "mcp":
                # MCP lifecycle path: ingest sessions via context_commit
                total, ingested, errors = await self._ingest_locomo_mcp(
                    oc, conversations, total, ingested, errors
                )
            else:
                # Default: ingest observations via oc.store()
                for conv in conversations:
                    conv_id = conv.get("sample_id", str(self._dataset.index(conv)))
                    observations = _collect_observations(conv)
                    total += len(observations)

                    for obs in observations:
                        abstract = f"[{obs['session_date']}] {obs['speaker']}: {obs['text']}"
                        embed_text = f"{obs['speaker']}: {obs['text']}"
                        try:
                            result = await oc.store(
                                abstract=abstract,
                                category="observation",
                                embed_text=embed_text,
                                meta={
                                    "dia_id": obs["dia_id"],
                                    "speaker": obs["speaker"],
                                    "session_num": obs["session_num"],
                                    "conv_id": conv_id,
                                },
                            )
                            uri = result.get("uri", "")
                            if uri:
                                self._dia_id_to_uri[(conv_id, obs["dia_id"])] = uri
                            ingested += 1
                        except Exception as e:
                            errors.append(f"conv={conv_id} dia_id={obs['dia_id']}: {e}")

        elif self._dataset_type == "longmemeval":
            qa_items = self._dataset
            if max_qa > 0:
                qa_items = qa_items[:max_qa]

            # Collect unique sessions across selected QA items
            unique_sessions: Dict[str, Dict] = {}
            for item in qa_items:
                session_ids = item.get("haystack_session_ids", [])
                sessions = item.get("haystack_sessions", [])
                dates = item.get("haystack_dates", [])
                for i, sid in enumerate(session_ids):
                    if sid not in unique_sessions:
                        msgs = sessions[i] if i < len(sessions) else []
                        date = dates[i] if i < len(dates) else ""
                        unique_sessions[sid] = {
                            "messages": msgs,
                            "date": date,
                            "session_id": sid,
                        }

            total = len(unique_sessions)
            self._lme_session_map = unique_sessions

            # Ingest each unique session as a memory item
            for sid, sess in unique_sessions.items():
                msgs = sess["messages"]
                date = sess["date"]
                # Build session text
                parts = []
                for m in msgs:
                    if isinstance(m, dict) and m.get("role") and m.get("content"):
                        parts.append(f"{m['role']}: {m['content']}")
                if not parts:
                    continue
                session_text = "\n".join(parts)
                # Abstract: date + first user message (truncated)
                first_user = next(
                    (m["content"][:200] for m in msgs
                     if isinstance(m, dict) and m.get("role") == "user"),
                    ""
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
                            "session_id": sid,
                            "date": date,
                            "num_messages": len(msgs),
                            "source": "longmemeval",
                        },
                    )
                    uri = result.get("uri", "")
                    if uri:
                        self._lme_session_to_uri[sid] = uri
                    ingested += 1
                except Exception as e:
                    errors.append(f"session={sid}: {e}")

        return IngestResult(
            total_items=total,
            ingested_items=ingested,
            errors=errors,
            meta={
                "dia_id_count": len(self._dia_id_to_uri),
                "lme_session_count": len(self._lme_session_to_uri),
                "ingest_method": ingest_method,
            },
        )

    async def _ingest_locomo_mcp(
        self,
        oc: Any,
        conversations: List[Dict],
        total: int,
        ingested: int,
        errors: List[str],
    ) -> Tuple[int, int, List[str]]:
        """Ingest LoCoMo sessions via MCP lifecycle (prepare/commit/end).

        Instead of storing observations, this ingests raw session turns
        as conversation messages, letting the server handle chunking and
        knowledge extraction.
        """
        for conv in conversations:
            conv_id = conv.get("sample_id", str(self._dataset.index(conv)))
            sessions = _parse_locomo_sessions(conv)
            total += len(sessions)

            for session in sessions:
                session_id = f"eval-{conv_id}-s{session['session_num']}"
                turns = session["turns"]
                # Build user/assistant message pairs
                turn_idx = 0
                for turn in turns:
                    speaker = turn.get("speaker", "")
                    text = turn.get("text", "")
                    if not text:
                        continue

                    # Determine role from speaker
                    role = "user" if speaker.lower() in ("user", "person a") else "assistant"
                    turn_idx += 1

                    try:
                        result = await oc.context_commit(
                            session_id=session_id,
                            turn_id=f"t{turn_idx}",
                            messages=[{"role": role, "content": text}],
                        )
                        uri = result.get("uri", "")
                        if uri:
                            dia_id = turn.get("dia_id", f"turn_{turn_idx}")
                            self._dia_id_to_uri[(conv_id, dia_id)] = uri
                    except Exception as e:
                        errors.append(f"conv={conv_id} session={session_id} t={turn_idx}: {e}")

                # End session → triggers Alpha pipeline
                try:
                    await oc.context_end(session_id)
                    ingested += 1
                except Exception as e:
                    errors.append(f"conv={conv_id} end session={session_id}: {e}")

        return total, ingested, errors

    def build_qa_items(self, **kwargs) -> List[QAItem]:
        """Build QA items with evidence dia_ids / session_ids mapped to stored URIs."""
        max_qa = kwargs.get("max_qa", 0)
        max_conv = kwargs.get("max_conv", 0)
        items: List[QAItem] = []

        if self._dataset_type == "locomo":
            conversations = self._dataset
            if max_conv > 0:
                conversations = conversations[:max_conv]

            for conv in conversations:
                conv_id = conv.get("sample_id", "")
                qa_list = conv.get("qa", [])
                for q in qa_list:
                    evidence = q.get("evidence", [])
                    expected_uris = [
                        self._dia_id_to_uri[(conv_id, eid)]
                        for eid in evidence
                        if (conv_id, eid) in self._dia_id_to_uri
                    ]
                    items.append(QAItem(
                        question=q["question"],
                        answer=_get_qa_answer(q),
                        category=str(q.get("category", "")),
                        difficulty=q.get("difficulty", ""),
                        expected_ids=evidence,
                        expected_uris=expected_uris,
                        meta={
                            "conv_id": conv_id,
                            "dataset": "locomo",
                        },
                    ))

        elif self._dataset_type == "longmemeval":
            for item in self._dataset:
                answer_sids = item.get("answer_session_ids", [])
                expected_uris = [
                    self._lme_session_to_uri[sid]
                    for sid in answer_sids
                    if sid in self._lme_session_to_uri
                ]
                items.append(QAItem(
                    question=item["question"],
                    answer=str(item.get("answer", "")),
                    category=item.get("question_type", "unknown"),
                    difficulty="",
                    expected_ids=answer_sids,
                    expected_uris=expected_uris,
                    meta={
                        "question_id": item.get("question_id", ""),
                        "question_date": item.get("question_date", ""),
                        "dataset": "longmemeval",
                    },
                ))

        if max_qa > 0:
            items = items[:max_qa]
        return items

    def get_baseline_context(self, qa_item: QAItem) -> str:
        """Full conversation text for baseline evaluation."""
        dataset = qa_item.meta.get("dataset", "")

        if dataset == "longmemeval" or self._dataset_type == "longmemeval":
            return self._lme_baseline_context(qa_item)

        # LoCoMo path
        conv_id = qa_item.meta.get("conv_id", "")
        for conv in self._dataset:
            cid = conv.get("sample_id", conv.get("id", ""))
            if str(cid) != str(conv_id) and conv_id:
                continue
            sessions = _parse_locomo_sessions(conv)
            speakers = _get_locomo_speakers(sessions)
            header = f"Conversation between {' and '.join(speakers)} over multiple sessions.\n\n"
            return header + "\n\n".join(_fmt_locomo_session(s) for s in sessions)

        return ""

    def _lme_baseline_context(self, qa_item: QAItem) -> str:
        """Build baseline context from LongMemEval haystack sessions."""
        qid = qa_item.meta.get("question_id", "")
        # Find the QA item in the dataset
        item = None
        for d in self._dataset:
            if d.get("question_id") == qid:
                item = d
                break
        if not item:
            return ""

        session_ids = item.get("haystack_session_ids", [])
        sessions = item.get("haystack_sessions", [])
        dates = item.get("haystack_dates", [])

        parts = []
        for i in range(len(session_ids)):
            msgs = sessions[i] if i < len(sessions) else []
            date = dates[i] if i < len(dates) else ""
            session_lines = [f"--- Session: {session_ids[i]} [{date}] ---"]
            for m in msgs:
                if isinstance(m, dict) and m.get("role") and m.get("content"):
                    session_lines.append(f"{m['role']}: {m['content']}")
            parts.append("\n".join(session_lines))
        return "\n\n".join(parts)

    async def retrieve(self, oc: Any, qa_item: QAItem, top_k: int) -> Tuple[List[Dict], float]:
        """Memory search retrieval.

        LoCoMo (store): searches observations (category=observation).
        LoCoMo (mcp): uses context_recall for session-based retrieval.
        LongMemEval: searches all conversations (no category filter).
        --retrieve-method recall: always uses context_recall (production path).
        """
        t0 = time.perf_counter()

        if self._dataset_type == "locomo" and self._ingest_method == "mcp":
            # MCP path: use context_recall with the first session ID
            conv_id = qa_item.meta.get("conv_id", "")
            session_id = f"ev-{conv_id}-recall"
            turn_id = "t-" + md5(qa_item.question.encode()).hexdigest()[:12]
            result = await oc.context_recall(
                session_id=session_id,
                query=qa_item.question,
                turn_id=turn_id,
                limit=top_k,
                detail_level="l0",
            )
            results = result.get("memory", [])
            latency_ms = (time.perf_counter() - t0) * 1000
            return results, latency_ms

        if self._retrieve_method == "recall":
            # Production path: context_recall with IntentRouter + multi-query
            conv_id = qa_item.meta.get("conv_id", "")
            qid = qa_item.meta.get("question_id", "")
            session_id = f"ev-{conv_id or qid}-recall"
            # Unique turn_id per question to avoid cache collision
            turn_id = "t-" + md5(qa_item.question.encode()).hexdigest()[:12]
            result = await oc.context_recall(
                session_id=session_id,
                query=qa_item.question,
                turn_id=turn_id,
                limit=top_k,
                detail_level="l0",
            )
            results = result.get("memory", [])
            latency_ms = (time.perf_counter() - t0) * 1000
            return results, latency_ms

        category = "observation" if self._dataset_type == "locomo" else ""
        results = await oc.search(
            query=qa_item.question,
            limit=top_k,
            category=category,
            detail_level="l0",
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        return results, latency_ms
