#!/usr/bin/env bash
# Start OpenCortex MCP Server in streamable-http mode.
#
# Usage:
#   ./scripts/start-mcp.sh              # foreground
#   ./scripts/start-mcp.sh --background # background with PID file
#
# Environment:
#   OPENCORTEX_PORT    — port (default: 8920)
#   OPENCORTEX_HOST    — host (default: 127.0.0.1)
#   OPENCORTEX_CONFIG  — config file path (optional)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_DIR/.opencortex-mcp.pid"

PORT="${OPENCORTEX_PORT:-8920}"
HOST="${OPENCORTEX_HOST:-127.0.0.1}"
CONFIG="${OPENCORTEX_CONFIG:-}"

cd "$PROJECT_DIR"

CMD=(
    uv run python -m opencortex.mcp_server
    --transport streamable-http
    --host "$HOST"
    --port "$PORT"
    --stateless
    --json-response
)

if [ -n "$CONFIG" ]; then
    CMD+=(--config "$CONFIG")
fi

if [ "${1:-}" = "--background" ]; then
    echo "Starting OpenCortex MCP Server in background on $HOST:$PORT..."
    PYTHONPATH=src "${CMD[@]}" &
    echo $! > "$PID_FILE"
    echo "PID: $(cat "$PID_FILE") (saved to $PID_FILE)"
    echo "Stop with: kill \$(cat $PID_FILE)"
elif [ "${1:-}" = "--stop" ]; then
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID"
            echo "Stopped MCP Server (PID: $PID)"
        else
            echo "Process $PID not running"
        fi
        rm -f "$PID_FILE"
    else
        echo "No PID file found"
    fi
else
    echo "Starting OpenCortex MCP Server on $HOST:$PORT..."
    PYTHONPATH=src exec "${CMD[@]}"
fi
