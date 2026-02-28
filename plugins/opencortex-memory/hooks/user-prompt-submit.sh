#!/usr/bin/env bash
# UserPromptSubmit hook: inject systemMessage prompting Claude to use
# memory_search MCP tool when context recall would be helpful.
# This hook is intentionally lightweight (< 50ms) — no subprocess calls,
# no HTTP requests. The actual recall decision is delegated to Claude itself.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

PROMPT="$(_json_val "$INPUT" "prompt" "")"
if [[ -z "$PROMPT" ]]; then
  echo '{}'
  exit 0
fi

# Only inject if session is active (config + state exist)
if [[ ! -f "$CONFIG_FILE" ]] || [[ ! -f "$STATE_FILE" ]]; then
  echo '{}'
  exit 0
fi

# Check session is active
ACTIVE="$(_json_val "$(cat "$STATE_FILE")" "active" "false")"
if [[ "$ACTIVE" != "true" ]]; then
  echo '{}'
  exit 0
fi

echo '{"systemMessage": "[opencortex-memory] Memory system active. If this query could benefit from past context, preferences, or learned patterns, use the memory_search MCP tool."}'
