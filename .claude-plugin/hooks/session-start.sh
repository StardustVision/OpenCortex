#!/usr/bin/env bash
# SessionStart hook: initialize OpenCortex memory session.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

if [[ -z "$CONFIG_FILE" || ! -f "$CONFIG_FILE" ]]; then
  msg='[opencortex-memory] WARNING: config not found. Create $HOME/.opencortex/opencortex.json or run: python -c "from opencortex.config import CortexConfig; CortexConfig.ensure_default_config()"'
  json_msg=$(_json_encode_str "$msg")
  echo "{\"systemMessage\": $json_msg}"
  exit 0
fi

OUT="$(run_bridge session-start 2>/dev/null || true)"
OK="$(_json_val "$OUT" "ok" "false")"
STATUS="$(_json_val "$OUT" "status_line" "[opencortex-memory] initialization failed")"
ADDL="$(_json_val "$OUT" "additional_context" "")"

json_status=$(_json_encode_str "$STATUS")

if [[ "$OK" == "true" && -n "$ADDL" ]]; then
  json_addl=$(_json_encode_str "$ADDL")
  echo "{\"systemMessage\": $json_status, \"hookSpecificOutput\": {\"hookEventName\": \"SessionStart\", \"additionalContext\": $json_addl}}"
  exit 0
fi

echo "{\"systemMessage\": $json_status}"
