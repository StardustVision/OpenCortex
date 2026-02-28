#!/usr/bin/env bash
# Stop hook (async): ingest latest turn into OpenCortex memory.
# This runs after each assistant response completes.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

STOP_HOOK_ACTIVE="$(_json_val "$INPUT" "stop_hook_active" "false")"
if [[ "$STOP_HOOK_ACTIVE" == "true" ]]; then
  echo '{}'
  exit 0
fi

if [[ ! -f "$CONFIG_FILE" || ! -f "$STATE_FILE" ]]; then
  echo '{}'
  exit 0
fi

TRANSCRIPT_PATH="$(_json_val "$INPUT" "transcript_path" "")"
if [[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]]; then
  echo '{}'
  exit 0
fi

# Fork ingest to a fully detached process so the hook returns immediately.
# nohup + disown + fd redirects ensure Claude Code doesn't wait on child processes.
# LLM summarization has been removed from the ingest path, so this finishes in <2s.
PYTHONPATH="${PROJECT_DIR}/src${PYTHONPATH:+:$PYTHONPATH}" \
  nohup "$PYTHON_BIN" "$BRIDGE" \
    --project-dir "$PROJECT_DIR" \
    --state-file "$STATE_FILE" \
    --config "$CONFIG_FILE" \
    ingest-stop --transcript-path "$TRANSCRIPT_PATH" \
    </dev/null >/dev/null 2>&1 &
disown

echo '{}'
