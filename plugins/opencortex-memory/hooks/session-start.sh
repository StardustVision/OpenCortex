#!/usr/bin/env bash
# SessionStart hook: start servers (local) and initialize memory session.
#
# Local mode:  start HTTP server + MCP server in background, save PIDs.
# Remote mode: verify remote HTTP server is reachable.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

mkdir -p "$STATE_DIR"

# ---------------------------------------------------------------------------
# Validate config
# ---------------------------------------------------------------------------
if [[ -z "$CONFIG_FILE" || ! -f "$CONFIG_FILE" ]]; then
  msg='[opencortex-memory] WARNING: config not found. Create opencortex.json or $HOME/.opencortex/opencortex.json'
  json_msg=$(_json_encode_str "$msg")
  echo "{\"systemMessage\": $json_msg}"
  exit 0
fi

MODE="$(get_plugin_mode)"
HTTP_URL="$(get_http_url)"

# ---------------------------------------------------------------------------
# Local mode: start HTTP server + MCP server
# ---------------------------------------------------------------------------
if [[ "$MODE" == "local" ]]; then
  HTTP_PORT="$(get_plugin_config "local.http_port" "8921")"
  MCP_PORT="$(get_plugin_config "local.mcp_port" "8920")"

  # Check if HTTP server is already running
  if ! http_server_ready; then
    # Start HTTP server
    PYTHONPATH="${PROJECT_DIR}/src${PYTHONPATH:+:$PYTHONPATH}" \
      nohup "$PYTHON_BIN" -m opencortex.http \
        --config "$CONFIG_FILE" --port "$HTTP_PORT" --log-level WARNING \
        > "$STATE_DIR/http.log" 2>&1 &
    HTTP_PID=$!

    # Wait for HTTP server to be ready (max 10s)
    for _i in $(seq 1 10); do
      if http_server_ready; then
        break
      fi
      sleep 1
    done

    if ! http_server_ready; then
      msg="[opencortex-memory] WARNING: HTTP server failed to start on port ${HTTP_PORT}"
      json_msg=$(_json_encode_str "$msg")
      echo "{\"systemMessage\": $json_msg}"
      exit 0
    fi
  else
    HTTP_PID=""
  fi

  # Check if MCP server is already running
  MCP_ALIVE=$(curl -sf "http://127.0.0.1:${MCP_PORT}/mcp" 2>/dev/null; echo $?)
  if [[ "$MCP_ALIVE" != "0" ]]; then
    # Start MCP server in remote mode (forwards to HTTP server)
    PYTHONPATH="${PROJECT_DIR}/src${PYTHONPATH:+:$PYTHONPATH}" \
      nohup "$PYTHON_BIN" -m opencortex.mcp_server \
        --config "$CONFIG_FILE" --transport streamable-http \
        --port "$MCP_PORT" --mode remote --log-level WARNING \
        > "$STATE_DIR/mcp.log" 2>&1 &
    MCP_PID=$!
    sleep 1
  else
    MCP_PID=""
  fi

  # Save state
  CONFIG_DATA="$(cat "$CONFIG_FILE")"
  TENANT="$(_json_val "$CONFIG_DATA" "tenant_id" "default")"
  USER_ID="$(_json_val "$CONFIG_DATA" "user_id" "default")"

  # Write session state with PIDs
  if [[ -n "$PYTHON_BIN" ]]; then
    "$PYTHON_BIN" -c "
import json, time
state = {
    'active': True,
    'mode': '${MODE}',
    'project_dir': '${PROJECT_DIR}',
    'config_path': '${CONFIG_FILE}',
    'http_url': '${HTTP_URL}',
    'tenant_id': '${TENANT}',
    'user_id': '${USER_ID}',
    'http_pid': ${HTTP_PID:-0},
    'mcp_pid': ${MCP_PID:-0},
    'last_turn_uuid': '',
    'ingested_turns': 0,
    'started_at': int(time.time()),
}
with open('${STATE_FILE}', 'w') as f:
    json.dump(state, f, indent=2)
"
  fi

  STATUS="[opencortex-memory] local mode — HTTP :${HTTP_PORT} MCP :${MCP_PORT} tenant=${TENANT} user=${USER_ID}"

# ---------------------------------------------------------------------------
# Remote mode: verify connectivity
# ---------------------------------------------------------------------------
else
  REMOTE_HTTP="$(get_plugin_config "remote.http_url" "")"
  if [[ -z "$REMOTE_HTTP" ]]; then
    msg="[opencortex-memory] WARNING: remote.http_url not configured in config.json"
    json_msg=$(_json_encode_str "$msg")
    echo "{\"systemMessage\": $json_msg}"
    exit 0
  fi

  # Test connectivity
  if ! curl -sf "${REMOTE_HTTP}/api/v1/memory/health" >/dev/null 2>&1; then
    msg="[opencortex-memory] WARNING: remote HTTP server unreachable at ${REMOTE_HTTP}"
    json_msg=$(_json_encode_str "$msg")
    echo "{\"systemMessage\": $json_msg}"
    exit 0
  fi

  CONFIG_DATA="$(cat "$CONFIG_FILE")"
  TENANT="$(_json_val "$CONFIG_DATA" "tenant_id" "default")"
  USER_ID="$(_json_val "$CONFIG_DATA" "user_id" "default")"

  # Write session state (no PIDs for remote mode)
  if [[ -n "$PYTHON_BIN" ]]; then
    "$PYTHON_BIN" -c "
import json, time
state = {
    'active': True,
    'mode': 'remote',
    'project_dir': '${PROJECT_DIR}',
    'config_path': '${CONFIG_FILE}',
    'http_url': '${REMOTE_HTTP}',
    'tenant_id': '${TENANT}',
    'user_id': '${USER_ID}',
    'http_pid': 0,
    'mcp_pid': 0,
    'last_turn_uuid': '',
    'ingested_turns': 0,
    'started_at': int(time.time()),
}
with open('${STATE_FILE}', 'w') as f:
    json.dump(state, f, indent=2)
"
  fi

  STATUS="[opencortex-memory] remote mode — ${REMOTE_HTTP} tenant=${TENANT} user=${USER_ID}"
fi

json_status=$(_json_encode_str "$STATUS")
echo "{\"systemMessage\": $json_status}"
