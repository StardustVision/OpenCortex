from __future__ import annotations

from dataclasses import dataclass
from typing import Any


VALID_EVENT_TYPES = {
    "user_prompt",
    "assistant_response",
    "tool_use_end",
    "session_end",
    "error",
}


@dataclass
class MemoryEvent:
    event_id: str
    source_tool: str
    session_id: str
    event_type: str
    content: str
    meta: dict[str, Any]
    created_at: float
    domain_hint: str | None = None
    confidence: float | None = None

    def normalize(self) -> "MemoryEvent":
        normalized_type = self.event_type.strip().lower()
        if normalized_type not in VALID_EVENT_TYPES:
            normalized_type = "tool_use_end"
        self.event_type = normalized_type
        self.source_tool = self.source_tool.strip().lower()
        return self
