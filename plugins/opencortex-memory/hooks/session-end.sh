#!/usr/bin/env bash
# SessionEnd hook: store session summary, kill local servers.
#
# 1. Store session summary via bridge (HTTP POST to server)
# 2. Kill HTTP + MCP server PIDs (local mode only)
# 3. Mark session state inactive

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

if [[ ! -f "$STATE_FILE" ]]; then
  exit 0
fi

# ---------------------------------------------------------------------------
# Store session summary via bridge (best-effort)
# ---------------------------------------------------------------------------
if [[ -f "$CONFIG_FILE" ]]; then
  run_bridge session-end >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------------------
# Kill local servers if we started them
# ---------------------------------------------------------------------------
STATE_DATA="$(cat "$STATE_FILE" 2>/dev/null || echo '{}')"
MODE="$(_json_val "$STATE_DATA" "mode" "local")"

if [[ "$MODE" == "local" ]]; then
  HTTP_PID="$(_json_val "$STATE_DATA" "http_pid" "0")"
  MCP_PID="$(_json_val "$STATE_DATA" "mcp_pid" "0")"

  if [[ "$MCP_PID" -gt 0 ]] 2>/dev/null; then
    kill "$MCP_PID" 2>/dev/null || true
  fi

  if [[ "$HTTP_PID" -gt 0 ]] 2>/dev/null; then
    kill "$HTTP_PID" 2>/dev/null || true
  fi
fi

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
INGESTED="$(_json_val "$STATE_DATA" "ingested_turns" "0")"
STATUS="[opencortex-memory] session ended — turns=${INGESTED}"
json_status=$(_json_encode_str "$STATUS")
echo "{\"systemMessage\": $json_status}"
