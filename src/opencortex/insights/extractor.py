"""SessionMetaExtractor — pure-code (zero LLM) metric extraction from Trace objects."""

import os
from typing import Any, Dict, List

from opencortex.alpha.types import Trace, TurnStatus
from opencortex.insights.constants import (
    ERROR_CATEGORIES,
    EXTENSION_TO_LANGUAGE,
)
from opencortex.insights.types import SessionMeta


class SessionMetaExtractor:
    """
    Extracts per-session metrics from a Trace object without any LLM calls.

    Equivalent to Claude Code's extractToolStats() — pure deterministic counting
    over the tool_calls list in each Turn.
    """

    def extract(self, trace: Trace) -> SessionMeta:
        """Process all turns in a Trace and return a populated SessionMeta."""

        # Accumulators
        tool_counts: Dict[str, int] = {}
        languages: Dict[str, int] = {}
        git_commits: int = 0
        git_pushes: int = 0
        input_tokens: int = 0
        output_tokens: int = 0
        tool_errors: int = 0
        tool_error_categories: Dict[str, int] = {}
        uses_agent: bool = False
        uses_mcp: bool = False
        uses_web_search: bool = False
        uses_web_fetch: bool = False
        lines_added: int = 0
        lines_removed: int = 0
        files_modified_set: set = set()
        user_interruptions: int = 0
        user_message_count: int = 0
        assistant_message_count: int = 0
        first_prompt: str = ""

        for turn in trace.turns:
            # Message counting
            if turn.prompt_text:
                user_message_count += 1
                if not first_prompt:
                    first_prompt = turn.prompt_text[:200]

            if turn.final_text:
                assistant_message_count += 1

            # Interruption counting
            if turn.turn_status == TurnStatus.INTERRUPTED:
                user_interruptions += 1

            # Token counting
            if turn.token_count is not None:
                input_tokens += turn.token_count

            # Tool call processing
            for tc in turn.tool_calls:
                name: str = tc.get("name", "")
                if not name:
                    continue

                # Count by name
                tool_counts[name] = tool_counts.get(name, 0) + 1

                # Special tool detection
                if name == "Agent":
                    uses_agent = True
                if name.startswith("mcp__"):
                    uses_mcp = True
                if name == "WebSearch":
                    uses_web_search = True
                if name == "WebFetch":
                    uses_web_fetch = True

                # Error classification
                is_error = tc.get("is_error", False)
                if is_error:
                    tool_errors += 1
                    error_text = (tc.get("error_text") or "").lower()
                    category = self._classify_error(error_text)
                    tool_error_categories[category] = (
                        tool_error_categories.get(category, 0) + 1
                    )

                # input_params-dependent processing
                params: Dict[str, Any] = tc.get("input_params") or {}

                # Language detection from file_path (any tool)
                file_path: str = params.get("file_path", "")
                if file_path:
                    ext = os.path.splitext(file_path)[1].lower()
                    lang = EXTENSION_TO_LANGUAGE.get(ext)
                    if lang:
                        languages[lang] = languages.get(lang, 0) + 1

                # Files modified (Edit / Write)
                if name in ("Edit", "Write") and file_path:
                    files_modified_set.add(file_path)

                # Line change counting
                if name == "Write":
                    content: str = params.get("content", "")
                    if content:
                        lines_added += content.count("\n") + 1

                elif name == "Edit":
                    old_str: str = params.get("old_string", "")
                    new_str: str = params.get("new_string", "")
                    old_lines = old_str.count("\n") + 1 if old_str else 0
                    new_lines = new_str.count("\n") + 1 if new_str else 0
                    if new_lines > old_lines:
                        lines_added += new_lines - old_lines
                    elif old_lines > new_lines:
                        lines_removed += old_lines - new_lines

                # Git detection (Bash tool)
                if name == "Bash":
                    command: str = params.get("command", "")
                    if "git commit" in command:
                        git_commits += 1
                    if "git push" in command:
                        git_pushes += 1

        return SessionMeta(
            session_id=trace.session_id,
            tenant_id=trace.tenant_id,
            user_id=trace.user_id,
            project_path="",
            start_time=trace.created_at,
            duration_minutes=0.0,
            user_message_count=user_message_count,
            assistant_message_count=assistant_message_count,
            tool_counts=tool_counts,
            languages=languages,
            git_commits=git_commits,
            git_pushes=git_pushes,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            first_prompt=first_prompt,
            user_interruptions=user_interruptions,
            tool_errors=tool_errors,
            tool_error_categories=tool_error_categories,
            uses_agent=uses_agent,
            uses_mcp=uses_mcp,
            uses_web_search=uses_web_search,
            uses_web_fetch=uses_web_fetch,
            lines_added=lines_added,
            lines_removed=lines_removed,
            files_modified=len(files_modified_set),
        )

    def _classify_error(self, error_text: str) -> str:
        """Match error_text against ERROR_CATEGORIES rules, return category name."""
        for keywords, category in ERROR_CATEGORIES:
            if any(kw in error_text for kw in keywords):
                return category
        return "Other"
