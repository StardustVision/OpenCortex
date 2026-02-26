#!/usr/bin/env bash
# SessionStart hook: start RuVector + MCP server, then initialize memory session.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

# ---------------------------------------------------------------------------
# Auto-start RuVector server if not running
# ---------------------------------------------------------------------------
RUVECTOR_PORT="$(_json_val "$(cat "$CONFIG_FILE" 2>/dev/null)" "ruvector_port" "6921")"
RUVECTOR_SCRIPT="$PLUGIN_ROOT/scripts/ruvector-server.js"

mkdir -p "$PROJECT_DIR/.opencortex"

if ! curl -sf "http://127.0.0.1:${RUVECTOR_PORT}/health" >/dev/null 2>&1; then
  if command -v node >/dev/null 2>&1 && [[ -f "$RUVECTOR_SCRIPT" ]]; then
    # Kill stale ruvector processes that failed to release the DB lock
    pkill -f "node.*ruvector-server\\.js.*--port ${RUVECTOR_PORT}" 2>/dev/null || true
    sleep 0.2

    nohup node "$RUVECTOR_SCRIPT" \
      --port "$RUVECTOR_PORT" \
      --data-dir "$PROJECT_DIR/data/ruvector" \
      >"$PROJECT_DIR/.opencortex/ruvector.log" 2>&1 &

    # Wait for server to be ready (RuVector starts in ~0.2s normally)
    for i in 1 2 3; do
      sleep 0.3
      curl -sf "http://127.0.0.1:${RUVECTOR_PORT}/health" >/dev/null 2>&1 && break
    done
  fi
fi

# ---------------------------------------------------------------------------
# Auto-start MCP server (SSE) if configured and not running
# ---------------------------------------------------------------------------
MCP_TRANSPORT="$(_json_val "$(cat "$CONFIG_FILE" 2>/dev/null)" "mcp_transport" "stdio")"
MCP_PORT="$(_json_val "$(cat "$CONFIG_FILE" 2>/dev/null)" "mcp_port" "8920")"

if [[ "$MCP_TRANSPORT" == "sse" || "$MCP_TRANSPORT" == "http" ]]; then
  if ! curl -sf "http://127.0.0.1:${MCP_PORT}/health" >/dev/null 2>&1; then
    if [[ -n "$PYTHON_BIN" ]]; then
      nohup "$PYTHON_BIN" -m opencortex.mcp_server \
        --transport "$MCP_TRANSPORT" \
        --port "$MCP_PORT" \
        --config "$CONFIG_FILE" \
        >"$PROJECT_DIR/.opencortex/mcp.log" 2>&1 &
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Initialize memory session (original logic)
# ---------------------------------------------------------------------------
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
