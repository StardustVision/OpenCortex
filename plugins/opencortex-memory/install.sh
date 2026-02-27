#!/usr/bin/env bash
# Install OpenCortex memory plugin into Claude Code.
#
# Merges plugin hooks into .claude/settings.json so they fire every turn.
# Safe to run multiple times (idempotent).
#
# Usage:
#   bash plugins/opencortex-memory/install.sh          # from project root
#   bash install.sh                                     # from plugin dir

set -euo pipefail

# Resolve paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$SCRIPT_DIR"
PROJECT_DIR="$(cd "$PLUGIN_ROOT/../.." && pwd)"
SETTINGS="$PROJECT_DIR/.claude/settings.json"

# Plugin hook path prefix (relative to project root)
PLUGIN_REL="plugins/opencortex-memory"

echo "[opencortex-memory] Installing plugin hooks..."

# Ensure .claude/ exists
mkdir -p "$PROJECT_DIR/.claude"

# Python inline script to merge hooks into settings.json
python3 - "$SETTINGS" "$PLUGIN_REL" << 'PYTHON'
import json, sys
from pathlib import Path

settings_path = Path(sys.argv[1])
plugin_rel = sys.argv[2]

# Load or create settings
if settings_path.exists():
    settings = json.loads(settings_path.read_text())
else:
    settings = {}

hooks = settings.setdefault("hooks", {})

# Marker used to identify our hooks for clean uninstall
MARKER = f"bash {plugin_rel}/hooks/"

# Define the hooks to register
PLUGIN_HOOKS = {
    "SessionStart": {
        "type": "command",
        "command": f"bash {plugin_rel}/hooks/session-start.sh",
        "timeout": 12000,
    },
    "UserPromptSubmit": {
        "type": "command",
        "command": f"bash {plugin_rel}/hooks/user-prompt-submit.sh",
        "timeout": 15000,
    },
    "Stop": {
        "type": "command",
        "command": f"bash {plugin_rel}/hooks/stop.sh",
        "timeout": 120000,
    },
    "SubagentStop": {
        "type": "command",
        "command": f"bash {plugin_rel}/hooks/stop.sh",
        "timeout": 120000,
    },
}

changed = False

for event, hook_def in PLUGIN_HOOKS.items():
    entries = hooks.get(event, [])

    # Check if already registered (by command prefix)
    already = any(
        any(MARKER in h.get("command", "") for h in entry.get("hooks", []))
        for entry in entries
    )
    if already:
        continue

    # Append our hook
    entries.append({"hooks": [hook_def]})
    hooks[event] = entries
    changed = True
    print(f"  + {event} -> {hook_def['command']}")

if changed:
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    print(f"\n[opencortex-memory] Hooks written to {settings_path}")
else:
    print("[opencortex-memory] Hooks already registered, no changes needed.")
PYTHON

echo "[opencortex-memory] Install complete. Restart Claude Code to activate."
