#!/usr/bin/env python3
"""Cross-platform installer for OpenCortex memory plugin.

Registers node-based hook commands into .claude/settings.json.
All hooks use `node hook_runner.mjs <name>` which auto-dispatches
to bash (.sh) on macOS/Linux or PowerShell (.ps1) on Windows.

Safe to run multiple times (idempotent).

Usage:
    python3 plugins/opencortex-memory/install.py          # from project root
    python3 install.py                                     # from plugin dir
"""

import json
import sys
from pathlib import Path


def build_hook_command(plugin_rel: str, hook_name: str) -> str:
    """Build the node hook command string."""
    return f"node {plugin_rel}/hooks/hook_runner.mjs {hook_name}"


def main():
    # Resolve paths
    script_dir = Path(__file__).resolve().parent
    plugin_root = script_dir
    project_dir = plugin_root.parent.parent
    settings_path = project_dir / ".claude" / "settings.json"
    plugin_rel = "plugins/opencortex-memory"

    print(f"[opencortex-memory] Installing plugin hooks (platform: {sys.platform})...")

    # Ensure .claude/ exists
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Load or create settings
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})

    # Markers for identifying our hooks (all variants) for clean replacement
    MARKERS = (
        f"node {plugin_rel}/hooks/hook_runner.mjs",
        f"bash {plugin_rel}/hooks/",
        f"powershell -NoProfile -ExecutionPolicy Bypass -File {plugin_rel}/hooks/",
        f"python3 {plugin_rel}/hooks/hook_runner.py",
    )

    def is_our_hook(command: str) -> bool:
        return any(marker in command for marker in MARKERS)

    # Define hooks to register (all use node dispatcher)
    plugin_hooks = {
        "SessionStart": {
            "type": "command",
            "command": build_hook_command(plugin_rel, "session-start"),
            "timeout": 12000,
        },
        "UserPromptSubmit": {
            "type": "command",
            "command": build_hook_command(plugin_rel, "user-prompt-submit"),
            "timeout": 15000,
        },
        "Stop": {
            "type": "command",
            "command": build_hook_command(plugin_rel, "stop"),
            "timeout": 120000,
        },
        "SubagentStop": {
            "type": "command",
            "command": build_hook_command(plugin_rel, "stop"),
            "timeout": 120000,
        },
    }

    changed = False

    for event, hook_def in plugin_hooks.items():
        entries = hooks.get(event, [])

        # Check if the correct hook is already registered
        already_correct = any(
            any(h.get("command", "") == hook_def["command"] for h in entry.get("hooks", []))
            for entry in entries
        )
        if already_correct:
            continue

        # Remove any existing opencortex hooks (old bash/powershell/python variants)
        new_entries = []
        for entry in entries:
            entry_hooks = entry.get("hooks", [])
            filtered = [h for h in entry_hooks if not is_our_hook(h.get("command", ""))]
            if filtered:
                entry["hooks"] = filtered
                new_entries.append(entry)

        # Add node hook
        new_entries.append({"hooks": [hook_def]})
        hooks[event] = new_entries
        changed = True
        print(f"  + {event} -> {hook_def['command']}")

    if changed:
        settings_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\n[opencortex-memory] Hooks written to {settings_path}")
    else:
        print("[opencortex-memory] Hooks already registered, no changes needed.")

    print("[opencortex-memory] Install complete. Restart Claude Code to activate.")


if __name__ == "__main__":
    main()
