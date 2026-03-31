"""Sliding window detection of concurrent session usage (CC-equivalent)."""

from datetime import datetime
from typing import Any, Dict, List

from opencortex.insights.constants import OVERLAP_WINDOW_MS
from opencortex.insights.types import SessionMeta


def detect_multi_clauding(sessions: List[SessionMeta]) -> Dict[str, int]:
    """Detect pattern: session1 → session2 → session1 within 30-min window."""
    all_messages: List[Dict[str, Any]] = []
    for meta in sessions:
        for ts_str in meta.user_message_timestamps:
            try:
                ts = datetime.fromisoformat(ts_str).timestamp() * 1000
                all_messages.append({"ts": ts, "session_id": meta.session_id})
            except (ValueError, TypeError):
                continue

    all_messages.sort(key=lambda m: m["ts"])

    session_pairs: set = set()
    messages_during: set = set()
    window_start = 0
    session_last_index: Dict[str, int] = {}

    for i, msg in enumerate(all_messages):
        # Shrink window from the left
        while (
            window_start < i
            and msg["ts"] - all_messages[window_start]["ts"] > OVERLAP_WINDOW_MS
        ):
            expiring = all_messages[window_start]
            if session_last_index.get(expiring["session_id"]) == window_start:
                del session_last_index[expiring["session_id"]]
            window_start += 1

        # Check for interleaving
        prev_idx = session_last_index.get(msg["session_id"])
        if prev_idx is not None:
            for j in range(prev_idx + 1, i):
                between = all_messages[j]
                if between["session_id"] != msg["session_id"]:
                    pair = tuple(sorted([msg["session_id"], between["session_id"]]))
                    session_pairs.add(pair)
                    messages_during.add(f"{all_messages[prev_idx]['ts']}:{msg['session_id']}")
                    messages_during.add(f"{between['ts']}:{between['session_id']}")
                    messages_during.add(f"{msg['ts']}:{msg['session_id']}")
                    break

        session_last_index[msg["session_id"]] = i

    involved: set = set()
    for s1, s2 in session_pairs:
        involved.add(s1)
        involved.add(s2)

    return {
        "overlap_events": len(session_pairs),
        "sessions_involved": len(involved),
        "user_messages_during": len(messages_during),
    }
