#!/usr/bin/env bash
# Uninstall OpenCortex memory plugin from Claude Code.
#
# Removes plugin hooks from .claude/settings.json.
# Preserves any other hooks registered by the user or other plugins.
#
# Usage:
#   bash plugins/opencortex-memory/uninstall.sh        # from project root
#   bash uninstall.sh                                   # from plugin dir

set -euo pipefail

# Resolve paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$SCRIPT_DIR"
PROJECT_DIR="$(cd "$PLUGIN_ROOT/../.." && pwd)"
SETTINGS="$PROJECT_DIR/.claude/settings.json"

# Plugin hook path prefix (relative to project root)
PLUGIN_REL="plugins/opencortex-memory"

echo "[opencortex-memory] Uninstalling plugin hooks..."

if [[ ! -f "$SETTINGS" ]]; then
    echo "[opencortex-memory] No settings.json found, nothing to remove."
    exit 0
fi

# Python inline script to remove our hooks from settings.json
python3 - "$SETTINGS" "$PLUGIN_REL" << 'PYTHON'
import json, sys
from pathlib import Path

settings_path = Path(sys.argv[1])
plugin_rel = sys.argv[2]

settings = json.loads(settings_path.read_text())
hooks = settings.get("hooks", {})
MARKER = f"bash {plugin_rel}/hooks/"

changed = False
empty_events = []

for event, entries in hooks.items():
    original_len = len(entries)
    # Keep only entries that do NOT contain our marker
    filtered = [
        entry for entry in entries
        if not any(MARKER in h.get("command", "") for h in entry.get("hooks", []))
    ]
    if len(filtered) < original_len:
        removed = original_len - len(filtered)
        print(f"  - {event}: removed {removed} hook(s)")
        hooks[event] = filtered
        changed = True
    if not filtered:
        empty_events.append(event)

# Clean up empty event lists
for event in empty_events:
    del hooks[event]

if changed:
    # Remove hooks key entirely if empty
    if not hooks:
        settings.pop("hooks", None)
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    print(f"\n[opencortex-memory] Hooks removed from {settings_path}")
else:
    print("[opencortex-memory] No plugin hooks found, nothing to remove.")
PYTHON

# Optionally clean up session state
if [[ -d "$PROJECT_DIR/.opencortex/memory" ]]; then
    echo "[opencortex-memory] Cleaning up session state..."
    rm -rf "$PROJECT_DIR/.opencortex/memory"
fi

echo "[opencortex-memory] Uninstall complete."
