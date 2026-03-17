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

Dataset detection: auto-detects LoCoMo vs LongMemEval from JSON structure.
"""

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from benchmarks.adapters.base import EvalAdapter, IngestResult, QAItem


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
        elif "sessions" in first:
            self._dataset_type = "longmemeval"
        else:
            raise ValueError(
                "Cannot detect dataset type. Expected LoCoMo (conversation.session_N) "
                "or LongMemEval (sessions[].messages[])."
            )

    async def ingest(self, oc: Any, **kwargs) -> IngestResult:
        """Ingest observations via oc.store() (LoCoMo RAG methodology)."""
        max_conv = kwargs.get("max_conv", 0)
        conversations = self._dataset
        if max_conv > 0:
            conversations = conversations[:max_conv]

        total = 0
        ingested = 0
        errors: List[str] = []
        self._dia_id_to_uri = {}

        for conv in conversations:
            conv_id = conv.get("sample_id", str(conversations.index(conv)))

            if self._dataset_type == "locomo":
                observations = _collect_observations(conv)
                total += len(observations)

                for obs in observations:
                    # Include date and speaker for temporal grounding
                    abstract = f"[{obs['session_date']}] {obs['speaker']}: {obs['text']}"
                    # Embed on speaker+text only; the [date] prefix
                    # adds noise that clusters all observations together.
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
            else:
                # LongMemEval: fall back to session-based ingest
                sessions = self._parse_longmemeval_sessions(conv)
                total += len(sessions)
                for session in sessions:
                    session_id = f"eval-{conv_id}-s{session['session_num']}"
                    try:
                        await self._ingest_longmemeval_session(oc, session, session_id)
                        ingested += 1
                    except Exception as e:
                        errors.append(f"conv={conv_id} session={session['session_num']}: {e}")

        return IngestResult(
            total_items=total,
            ingested_items=ingested,
            errors=errors,
            meta={"dia_id_count": len(self._dia_id_to_uri)},
        )

    async def _ingest_longmemeval_session(
        self, oc: Any, session: Dict, session_id: str
    ) -> None:
        """Fallback: ingest LongMemEval sessions via MCP flow."""
        turns = session.get("turns", [])
        msg_list = self._build_longmemeval_messages(turns)
        turn_idx = 0
        for j in range(0, len(msg_list) - 1, 2):
            pair = msg_list[j:j + 2]
            roles = {m["role"] for m in pair}
            if "user" not in roles or "assistant" not in roles:
                continue
            turn_idx += 1
            await oc.context_commit(
                session_id=session_id,
                turn_id=f"t{turn_idx}",
                messages=pair,
            )
        await oc.context_end(session_id)

    def _build_longmemeval_messages(self, turns: List[Dict]) -> List[Dict[str, str]]:
        return [
            {"role": t["role"], "content": t["content"]}
            for t in turns
            if t.get("role") and t.get("content")
        ]

    def _parse_longmemeval_sessions(self, conv: Dict) -> List[Dict]:
        sessions = []
        for i, sess in enumerate(conv.get("sessions", [])):
            sessions.append({
                "session_num": i + 1,
                "date_time": sess.get("date", ""),
                "turns": sess.get("messages", []),
            })
        return sessions

    def build_qa_items(self, **kwargs) -> List[QAItem]:
        """Build QA items with evidence dia_ids mapped to stored URIs."""
        max_qa = kwargs.get("max_qa", 0)
        max_conv = kwargs.get("max_conv", 0)
        items: List[QAItem] = []

        conversations = self._dataset
        if max_conv > 0:
            conversations = conversations[:max_conv]

        for conv in conversations:
            if self._dataset_type == "locomo":
                conv_id = conv.get("sample_id", "")
                qa_list = conv.get("qa", [])
                for q in qa_list:
                    # Map evidence dia_ids to stored URIs for Recall@k.
                    # dia_ids are NOT unique across conversations, so use
                    # (conv_id, dia_id) composite key.
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
            else:
                qa_list = conv.get("questions", [])
                for q in qa_list:
                    items.append(QAItem(
                        question=q["question"],
                        answer=str(q.get("answer", "")),
                        category=q.get("category", ""),
                        difficulty=q.get("difficulty", ""),
                        meta={"conv_id": conv.get("id", ""), "dataset": "longmemeval"},
                    ))

        if max_qa > 0:
            items = items[:max_qa]
        return items

    def get_baseline_context(self, qa_item: QAItem) -> str:
        """Full conversation text for baseline evaluation (matches paper)."""
        conv_id = qa_item.meta.get("conv_id", "")
        for conv in self._dataset:
            cid = conv.get("sample_id", conv.get("id", ""))
            if str(cid) != str(conv_id) and conv_id:
                continue

            if self._dataset_type == "locomo":
                sessions = _parse_locomo_sessions(conv)
                speakers = _get_locomo_speakers(sessions)
                header = f"Conversation between {' and '.join(speakers)} over multiple sessions.\n\n"
                return header + "\n\n".join(_fmt_locomo_session(s) for s in sessions)
            else:
                parts = []
                for sess in conv.get("sessions", []):
                    for msg in sess.get("messages", []):
                        parts.append(f"{msg['role']}: {msg['content']}")
                return "\n".join(parts)

        return ""

    async def retrieve(self, oc: Any, qa_item: QAItem, top_k: int) -> Tuple[List[Dict], float]:
        """Memory search retrieval (matches paper's RAG methodology).

        Uses oc.search() instead of context_recall() since observations
        are stored as memory items, not conversation transcripts.
        """
        t0 = time.perf_counter()
        results = await oc.search(
            query=qa_item.question,
            limit=top_k,
            category="observation",
            detail_level="l0",
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        return results, latency_ms
