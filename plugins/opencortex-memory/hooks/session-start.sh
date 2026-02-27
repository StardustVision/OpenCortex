#!/usr/bin/env bash
# SessionStart hook: start RuVector + MCP server (local mode) or verify
# connectivity (remote mode), then initialize memory session.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

MODE="$(get_plugin_mode)"
MCP_URL="$(get_mcp_url)"

mkdir -p "$PROJECT_DIR/.opencortex"

# ---------------------------------------------------------------------------
# Dynamic .mcp.json generation (both modes)
# ---------------------------------------------------------------------------
MCP_JSON="$PLUGIN_ROOT/.mcp.json"
cat > "$MCP_JSON" <<EOF
{
  "mcpServers": {
    "opencortex": {
      "url": "${MCP_URL}"
    }
  }
}
EOF

# ---------------------------------------------------------------------------
# Mode-specific setup
# ---------------------------------------------------------------------------
if [[ "$MODE" == "local" ]]; then
  # --- Start RuVector ---
  RUVECTOR_PORT="$(get_plugin_config "local.ruvector_port" "6921")"
  DATA_DIR="$(get_plugin_config "local.data_dir" "data/ruvector")"
  RUVECTOR_SCRIPT="$PLUGIN_ROOT/scripts/ruvector-server.js"

  if ! curl -sf "http://127.0.0.1:${RUVECTOR_PORT}/health" >/dev/null 2>&1; then
    if command -v node >/dev/null 2>&1 && [[ -f "$RUVECTOR_SCRIPT" ]]; then
      pkill -f "node.*ruvector-server\\.js.*--port ${RUVECTOR_PORT}" 2>/dev/null || true
      sleep 0.2

      nohup node "$RUVECTOR_SCRIPT" \
        --port "$RUVECTOR_PORT" \
        --data-dir "$PROJECT_DIR/$DATA_DIR" \
        >"$PROJECT_DIR/.opencortex/ruvector.log" 2>&1 &

      for i in 1 2 3; do
        sleep 0.3
        curl -sf "http://127.0.0.1:${RUVECTOR_PORT}/health" >/dev/null 2>&1 && break
      done
    fi
  fi

  # --- Start MCP Server ---
  MCP_PORT="$(get_plugin_config "local.mcp_port" "8920")"
  MCP_TRANSPORT="$(get_plugin_config "local.mcp_transport" "streamable-http")"

  if ! curl -sf "http://127.0.0.1:${MCP_PORT}/health" >/dev/null 2>&1; then
    if [[ -n "$PYTHON_BIN" ]]; then
      nohup "$PYTHON_BIN" -m opencortex.mcp_server \
        --transport "$MCP_TRANSPORT" \
        --port "$MCP_PORT" \
        --config "$CONFIG_FILE" \
        >"$PROJECT_DIR/.opencortex/mcp.log" 2>&1 &
    fi
  fi

elif [[ "$MODE" == "remote" ]]; then
  # --- Remote: connectivity check only ---
  if ! curl -sf --max-time 3 "$MCP_URL" >/dev/null 2>&1; then
    echo "[opencortex-memory] WARNING: remote MCP server not reachable at $MCP_URL" >&2
  fi
fi

# ---------------------------------------------------------------------------
# Initialize memory session (shared by both modes)
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
