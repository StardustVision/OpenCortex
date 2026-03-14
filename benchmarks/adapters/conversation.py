"""
Conversation adapter for LoCoMo and LongMemEval datasets.

Ingest: Simulates real MCP conversation flow per session:
  1. context_recall() at session start
  2. context_commit() per turn pair
  3. context_end() to flush Observer/TraceSplitter

Dataset detection: auto-detects LoCoMo vs LongMemEval from JSON structure.
  - LoCoMo: conversation.session_N structure with speaker fields
  - LongMemEval: sessions[].messages[] with role fields
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


class ConversationAdapter(EvalAdapter):
    """LoCoMo / LongMemEval evaluation adapter."""

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
        """Ingest conversations via MCP conversation flow."""
        max_conv = kwargs.get("max_conv", 0)
        conversations = self._dataset
        if max_conv > 0:
            conversations = conversations[:max_conv]

        total = 0
        ingested = 0
        errors: List[str] = []

        for conv in conversations:
            conv_id = conv.get("sample_id", str(conversations.index(conv)))
            if self._dataset_type == "locomo":
                sessions = _parse_locomo_sessions(conv)
            else:
                sessions = self._parse_longmemeval_sessions(conv)

            total += len(sessions)

            for i, session in enumerate(sessions):
                session_id = f"eval-{conv_id}-s{session['session_num']}"
                try:
                    await self._ingest_session(oc, session, session_id, conv_id)
                    ingested += 1
                except Exception as e:
                    errors.append(f"conv={conv_id} session={session['session_num']}: {e}")

        return IngestResult(total_items=total, ingested_items=ingested, errors=errors)

    async def _ingest_session(
        self, oc: Any, session: Dict, session_id: str, conv_id: str
    ) -> None:
        """Ingest a single session via 3-phase MCP flow."""
        turns = session.get("turns", [])
        date = session.get("date_time", "")

        # 1. Prepare phase (recall at session start)
        first_text = ""
        if turns:
            first_text = (turns[0].get("text", "") or turns[0].get("content", ""))[:120]
        try:
            await oc.context_recall(session_id, f"{date} {first_text}", turn_id="t0", limit=3)
        except Exception:
            pass  # non-fatal on first session

        # 2. Build and commit message pairs
        if self._dataset_type == "locomo":
            msg_list = self._build_locomo_messages(turns, date)
        else:
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

        # 3. End phase
        await oc.context_end(session_id)

    def _build_locomo_messages(self, turns: List[Dict], date: str) -> List[Dict[str, str]]:
        """Build message list from LoCoMo turns (speaker-based role mapping)."""
        msg_list: List[Dict[str, str]] = []
        first_speaker = turns[0]["speaker"] if turns else ""
        for t in turns:
            role = "user" if t["speaker"] == first_speaker else "assistant"
            text = t["text"]
            if "blip_caption" in t:
                text += f" [image: {t['blip_caption']}]"
            msg_list.append({"role": role, "content": f"[{date}] {t['speaker']}: {text}"})
        return msg_list

    def _build_longmemeval_messages(self, turns: List[Dict]) -> List[Dict[str, str]]:
        """Build message list from LongMemEval turns (role field maps directly)."""
        return [
            {"role": t["role"], "content": t["content"]}
            for t in turns
            if t.get("role") and t.get("content")
        ]

    def _parse_longmemeval_sessions(self, conv: Dict) -> List[Dict]:
        """Parse LongMemEval sessions structure."""
        sessions = []
        for i, sess in enumerate(conv.get("sessions", [])):
            sessions.append({
                "session_num": i + 1,
                "date_time": sess.get("date", ""),
                "turns": sess.get("messages", []),
            })
        return sessions

    def build_qa_items(self, **kwargs) -> List[QAItem]:
        """Build QA items from dataset."""
        max_qa = kwargs.get("max_qa", 0)
        items: List[QAItem] = []

        for conv in self._dataset:
            if self._dataset_type == "locomo":
                qa_list = conv.get("qa", [])
                for q in qa_list:
                    items.append(QAItem(
                        question=q["question"],
                        answer=_get_qa_answer(q),
                        category=str(q.get("category", "")),
                        difficulty=q.get("difficulty", ""),
                        meta={"conv_id": conv.get("sample_id", ""), "dataset": "locomo"},
                    ))
            else:
                # LongMemEval: questions at top level
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
        """Full conversation text for baseline evaluation."""
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
        """Session-aware retrieval via Context API prepare phase."""
        conv_id = qa_item.meta.get("conv_id", "eval")
        session_id = f"eval-{conv_id}-eval"
        t0 = time.perf_counter()
        try:
            result = await oc.context_recall(session_id, qa_item.question, limit=top_k)
            memories = result.get("memory", [])
        except Exception:
            # Fallback to direct search
            memories = await oc.search(query=qa_item.question, limit=top_k)
        latency_ms = (time.perf_counter() - t0) * 1000
        return memories, latency_ms
