#!/usr/bin/env bash
set -euo pipefail

# Example Cursor hook adapter.
# Usage:
#   observe.sh <event_type> <session_id> <content> [meta_json]

EVENT_TYPE="${1:-tool_use_end}"
SESSION_ID="${2:-sess_unknown}"
CONTENT="${3:-}"
META_JSON="${4:-{}}"

if [[ -z "$CONTENT" ]]; then
  exit 0
fi

MEMCORTEX_HOME="${MEMCORTEX_HOME:-$HOME/.memcortex}" \
PYTHONPATH="${PYTHONPATH:-src}" \
python3 -m memcortex.cli capture \
  --source-tool cursor \
  --session-id "$SESSION_ID" \
  --event-type "$EVENT_TYPE" \
  --content "$CONTENT" \
  --meta-json "$META_JSON" >/dev/null

MEMCORTEX_HOME="${MEMCORTEX_HOME:-$HOME/.memcortex}" \
PYTHONPATH="${PYTHONPATH:-src}" \
python3 -m memcortex.cli maybe-flush --local-only >/dev/null || true
