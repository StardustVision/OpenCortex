#!/usr/bin/env bash
# UserPromptSubmit hook: auto-recall memories on every prompt.
# Results are injected into model context AND printed to terminal (stderr).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

PROMPT="$(_json_val "$INPUT" "prompt" "")"
if [[ -z "$PROMPT" || ${#PROMPT} -lt 10 ]]; then
  echo '{}'
  exit 0
fi

if [[ ! -f "$CONFIG_FILE" || ! -f "$STATE_FILE" ]]; then
  echo '{}'
  exit 0
fi

# Auto-recall: search memories using the user prompt as query
RECALL_OUTPUT="$(run_bridge recall --query "$PROMPT" --top-k 5 2>/dev/null || true)"

if [[ -n "$RECALL_OUTPUT" && "$RECALL_OUTPUT" != "No relevant memories found." ]]; then
  # Print to terminal so user can see recall results
  echo -e "\033[36m[opencortex-recall]\033[0m" >&2
  echo "$RECALL_OUTPUT" >&2
  echo "" >&2

  # Also inject into model context
  RECALL_JSON=$(_json_encode_str "[opencortex-memory] Auto-recall results:
$RECALL_OUTPUT")
  echo "{\"systemMessage\": $RECALL_JSON}"
else
  echo '{"systemMessage":"[opencortex-memory] No relevant memories found for this prompt."}'
fi
