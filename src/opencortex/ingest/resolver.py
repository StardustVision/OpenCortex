"""IngestModeResolver — route content to memory/document/conversation mode."""

import re

# Dialog patterns: "User:", "Assistant:", "Human:", "AI:"
_DIALOG_RE = re.compile(
    r"^(User|Assistant|Human|AI|System)\s*:", re.MULTILINE | re.IGNORECASE
)
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_SMALL_DOC_THRESHOLD = 4000  # tokens (estimated)


def _estimate_tokens(text: str) -> int:
    """Estimate token count (CJK chars * 0.7 + other * 0.3)."""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cjk
    return int(cjk * 0.7 + other * 0.3)


class IngestModeResolver:
    """Determine ingestion mode from input signals.

    Resolution order (explicit first):
    1. meta.ingest_mode (forced)
    2. batch_store / source_path / scan_meta → document
    3. session_id → conversation
    4. Dialog patterns in content → conversation
    5. Headings + length > 4000 tokens → document
    6. Default → memory
    """

    @staticmethod
    def resolve(
        content: str = "",
        *,
        meta: dict | None = None,
        source_path: str = "",
        scan_meta: dict | None = None,
        session_id: str = "",
        is_batch: bool = False,
    ) -> str:
        meta = meta or {}

        # Priority 1: explicit mode
        explicit = meta.get("ingest_mode", "")
        if explicit in ("memory", "document", "conversation"):
            return explicit

        # Priority 2: batch / source_path / scan_meta → document
        if is_batch or source_path or scan_meta:
            return "document"

        # Priority 3: session_id → conversation
        if session_id:
            return "conversation"

        # Priority 4: dialog patterns
        if content and len(_DIALOG_RE.findall(content)) >= 2:
            return "conversation"

        # Priority 5: headings + length
        if content and _HEADING_RE.search(content):
            if _estimate_tokens(content) > _SMALL_DOC_THRESHOLD:
                return "document"

        # Priority 6: default
        return "memory"
