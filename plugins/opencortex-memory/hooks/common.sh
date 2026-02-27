#!/usr/bin/env bash
# Shared helpers for OpenCortex Claude Code hooks.

set -euo pipefail

INPUT="$(cat || true)"

for p in "$HOME/.local/bin" "$HOME/.cargo/bin" "$HOME/bin" "/usr/local/bin"; do
  [[ -d "$p" ]] && [[ ":$PATH:" != *":$p:"* ]] && export PATH="$p:$PATH"
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"

STATE_DIR="$PROJECT_DIR/.opencortex/memory"
STATE_FILE="$STATE_DIR/session_state.json"
BRIDGE="$PLUGIN_ROOT/scripts/oc_memory.py"

# Config file discovery: project local first, then global default
if [[ -f "$PROJECT_DIR/opencortex.json" ]]; then
  CONFIG_FILE="$PROJECT_DIR/opencortex.json"
elif [[ -f "$PROJECT_DIR/.opencortex.json" ]]; then
  CONFIG_FILE="$PROJECT_DIR/.opencortex.json"
elif [[ -f "$HOME/.opencortex/opencortex.json" ]]; then
  CONFIG_FILE="$HOME/.opencortex/opencortex.json"
else
  CONFIG_FILE=""
fi

# Python resolution: prefer project venv, then system python
if [[ -x "$PROJECT_DIR/.venv/bin/python3" ]]; then
  PYTHON_BIN="$PROJECT_DIR/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  PYTHON_BIN=""
fi

# Ensure PYTHONPATH includes project src so opencortex is importable
export PYTHONPATH="${PROJECT_DIR}/src${PYTHONPATH:+:$PYTHONPATH}"

_json_val() {
  local json="$1" key="$2" default="${3:-}"
  local result=""

  if command -v jq >/dev/null 2>&1; then
    result=$(printf '%s' "$json" | jq -r ".${key} // empty" 2>/dev/null) || true
  elif [[ -n "$PYTHON_BIN" ]]; then
    result=$(
      "$PYTHON_BIN" -c '
import json, sys
obj = json.loads(sys.argv[1])
val = obj
for k in sys.argv[2].split("."):
    if isinstance(val, dict):
        val = val.get(k)
    else:
        val = None
        break
if val is None:
    print("")
elif isinstance(val, bool):
    print("true" if val else "false")
else:
    print(val)
' "$json" "$key" 2>/dev/null
    ) || true
  fi

  if [[ -z "$result" ]]; then
    printf '%s' "$default"
  else
    printf '%s' "$result"
  fi
}

_json_encode_str() {
  local str="$1"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$str" | jq -Rs .
    return 0
  fi
  if [[ -n "$PYTHON_BIN" ]]; then
    printf '%s' "$str" | "$PYTHON_BIN" -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
    return 0
  fi
  printf '"%s"' "$str"
}

PLUGIN_CONFIG="$PLUGIN_ROOT/config.json"

# Read a value from plugin config.json using dotted key notation.
# Usage: get_plugin_config "local.mcp_port" "8920"
get_plugin_config() {
  local key="$1" default="${2:-}"
  if [[ -f "$PLUGIN_CONFIG" ]]; then
    _json_val "$(cat "$PLUGIN_CONFIG")" "$key" "$default"
  else
    printf '%s' "$default"
  fi
}

# Return current plugin mode: "local" or "remote" (default: "local")
get_plugin_mode() {
  get_plugin_config "mode" "local"
}

# Return the HTTP server URL based on mode.
# local  → http://127.0.0.1:{http_port}
# remote → value from remote.http_url
get_http_url() {
  local mode
  mode="$(get_plugin_mode)"
  if [[ "$mode" == "remote" ]]; then
    get_plugin_config "remote.http_url" "http://127.0.0.1:8921"
  else
    local port
    port="$(get_plugin_config "local.http_port" "8921")"
    printf 'http://127.0.0.1:%s' "$port"
  fi
}

# Return the MCP URL based on mode.
# local  → http://127.0.0.1:{mcp_port}/mcp
# remote → value from remote.mcp_url
get_mcp_url() {
  local mode
  mode="$(get_plugin_mode)"
  if [[ "$mode" == "remote" ]]; then
    get_plugin_config "remote.mcp_url" "http://127.0.0.1:8920/mcp"
  else
    local port
    port="$(get_plugin_config "local.mcp_port" "8920")"
    printf 'http://127.0.0.1:%s/mcp' "$port"
  fi
}

# Check if HTTP server is reachable.
http_server_ready() {
  local url
  url="$(get_http_url)"
  curl -sf "${url}/api/v1/memory/health" >/dev/null 2>&1
}

ensure_state_dir() {
  mkdir -p "$STATE_DIR"
}

run_bridge() {
  if [[ -z "$PYTHON_BIN" ]]; then
    echo '{"ok": false, "error": "python not found"}'
    return 1
  fi
  if [[ ! -f "$BRIDGE" ]]; then
    echo '{"ok": false, "error": "bridge script not found"}'
    return 1
  fi

  ensure_state_dir
  "$PYTHON_BIN" "$BRIDGE" \
    --project-dir "$PROJECT_DIR" \
    --state-file "$STATE_FILE" \
    --config "$CONFIG_FILE" \
    "$@"
}
