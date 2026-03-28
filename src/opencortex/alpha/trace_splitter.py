"""
Trace Splitter — LLM-based session splitting into task-specific traces.

Takes a full session transcript and uses LLM to:
  1. Identify distinct tasks within the session
  2. Assign turns to tasks
  3. Generate L0 (abstract) and L1 (overview) for each trace
  4. Handle long sessions via sliding window

Design doc §5.2, §10.2.
"""

import orjson as json
import logging
import uuid

from opencortex.utils.text import smart_truncate
from typing import Any, Callable, Coroutine, Dict, List, Optional

from opencortex.alpha.types import Trace, Turn, TraceOutcome, TurnStatus
from opencortex.prompts import TRACE_SPLIT_PROMPT

logger = logging.getLogger(__name__)


class TraceSplitter:
    def __init__(
        self,
        llm_fn: Callable[..., Coroutine],
        max_context_tokens: int = 128000,
        chars_per_token: int = 4,
    ):
        self._llm_fn = llm_fn
        self._max_context_tokens = max_context_tokens
        self._chars_per_token = chars_per_token

    def _estimate_tokens(self, text: str) -> int:
        return len(text) // self._chars_per_token

    def _transcript_to_text(self, messages: List[Dict[str, Any]]) -> str:
        lines = []
        for i, msg in enumerate(messages):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if len(content) > 2000:
                truncated = smart_truncate(content, 2000)
                content = f"{truncated} [...{len(content) - len(truncated)} chars omitted]"
            lines.append(f"[Turn {i}] {role}: {content}")
        return "\n".join(lines)

    def _parse_outcome(self, outcome_str: str) -> Optional[TraceOutcome]:
        mapping = {
            "success": TraceOutcome.SUCCESS,
            "failure": TraceOutcome.FAILURE,
            "timeout": TraceOutcome.TIMEOUT,
            "cancelled": TraceOutcome.CANCELLED,
        }
        return mapping.get(outcome_str)

    def _messages_to_turns(
        self, messages: List[Dict[str, Any]], indices: List[int],
    ) -> List[Turn]:
        turns = []
        for idx in indices:
            if 0 <= idx < len(messages):
                msg = messages[idx]
                turns.append(Turn(
                    turn_id=f"t{idx}",
                    prompt_text=msg.get("content") if msg.get("role") == "user" else None,
                    final_text=msg.get("content") if msg.get("role") == "assistant" else None,
                    turn_status=TurnStatus.COMPLETE,
                ))
        return turns

    async def split(
        self,
        messages: List[Dict[str, Any]],
        session_id: str,
        tenant_id: str,
        user_id: str,
        source: str = "claude_code",
    ) -> List[Trace]:
        """Split a session transcript into task-specific traces."""
        if not messages:
            return []

        transcript_text = self._transcript_to_text(messages)
        estimated_tokens = self._estimate_tokens(transcript_text)

        if estimated_tokens <= self._max_context_tokens * 0.9:
            return await self._split_single(
                messages, transcript_text, session_id, tenant_id, user_id, source,
            )
        else:
            return await self._split_windowed(
                messages, session_id, tenant_id, user_id, source,
            )

    async def _split_single(
        self,
        messages: List[Dict[str, Any]],
        transcript_text: str,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source: str,
    ) -> List[Trace]:
        """Single LLM call for sessions within context window."""
        prompt = TRACE_SPLIT_PROMPT.format(
            turn_count=len(messages),
            transcript=transcript_text,
        )

        _fallback = [{
            "summary": "Full session",
            "key_steps": [f"{len(messages)} messages"],
            "turn_indices": list(range(len(messages))),
            "outcome": "success",
            "task_type": "unknown",
        }]
        try:
            response = await self._llm_fn(prompt)
            # Strip markdown code fences (```json ... ```)
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            tasks = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(
                "TraceSplitter JSON parse error: %s | raw (500 chars): %s",
                e, repr(response[:500]) if response else "(empty)",
            )
            tasks = _fallback
        except Exception as e:
            logger.warning("TraceSplitter LLM call failed: %r", e)
            tasks = _fallback

        traces = []
        for task in tasks:
            trace_id = f"tr-{uuid.uuid4().hex}"
            indices = task.get("turn_indices", list(range(len(messages))))
            turns = self._messages_to_turns(messages, indices)

            key_steps = task.get("key_steps", [])
            overview = "## Steps\n" + "\n".join(f"- {s}" for s in key_steps) if key_steps else None

            traces.append(Trace(
                trace_id=trace_id,
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                source=source,
                turns=turns,
                abstract=task.get("summary"),
                overview=overview,
                outcome=self._parse_outcome(task.get("outcome", "")),
                task_type=task.get("task_type"),
            ))

        return traces

    async def _split_windowed(
        self,
        messages: List[Dict[str, Any]],
        session_id: str,
        tenant_id: str,
        user_id: str,
        source: str,
    ) -> List[Trace]:
        """Sliding window splitting for long sessions."""
        window_chars = int(self._max_context_tokens * 0.9 * self._chars_per_token)
        all_traces = []
        previous_summaries = []
        offset = 0

        while offset < len(messages):
            # Build window
            window_messages = []
            char_count = 0
            # Include previous summaries as context
            context_prefix = ""
            if previous_summaries:
                context_prefix = "Previous tasks:\n" + "\n".join(
                    f"- {s}" for s in previous_summaries
                ) + "\n\n"
                char_count = len(context_prefix)

            end = offset
            for i in range(offset, len(messages)):
                msg_text = f"[Turn {i}] {messages[i].get('role', '')}: {messages[i].get('content', '')}\n"
                if char_count + len(msg_text) > window_chars:
                    break
                char_count += len(msg_text)
                window_messages.append(messages[i])
                end = i + 1

            if not window_messages:
                # Single message too large, skip it
                offset += 1
                continue

            transcript_text = context_prefix + self._transcript_to_text(
                [{**m, "_orig_idx": offset + j} for j, m in enumerate(window_messages)]
            )

            window_traces = await self._split_single(
                window_messages, transcript_text,
                session_id, tenant_id, user_id, source,
            )

            # Adjust turn indices for the window offset
            for trace in window_traces:
                for turn in trace.turns:
                    idx = int(turn.turn_id.replace("t", ""))
                    turn.turn_id = f"t{offset + idx}"

            all_traces.extend(window_traces)
            previous_summaries.extend(
                t.abstract for t in window_traces if t.abstract
            )
            offset = end

        return all_traces
