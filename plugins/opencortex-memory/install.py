#!/usr/bin/env python3
"""Cross-platform installer for OpenCortex memory plugin.

Detects the current platform and registers the appropriate hook scripts
(bash on macOS/Linux, PowerShell on Windows) into .claude/settings.json.

Safe to run multiple times (idempotent).

Usage:
    python3 plugins/opencortex-memory/install.py          # from project root
    python3 install.py                                     # from plugin dir
"""

import json
import sys
from pathlib import Path


def detect_platform():
    """Return (shell_cmd, ext) for the current platform."""
    if sys.platform == "win32":
        return "powershell -NoProfile -ExecutionPolicy Bypass -File", ".ps1"
    else:
        return "bash", ".sh"


def build_hook_command(shell_cmd: str, plugin_rel: str, script: str) -> str:
    """Build the full hook command string."""
    return f"{shell_cmd} {plugin_rel}/hooks/{script}"


def main():
    # Resolve paths
    script_dir = Path(__file__).resolve().parent
    plugin_root = script_dir
    project_dir = plugin_root.parent.parent
    settings_path = project_dir / ".claude" / "settings.json"
    plugin_rel = "plugins/opencortex-memory"

    shell_cmd, ext = detect_platform()

    print(f"[opencortex-memory] Installing plugin hooks (platform: {sys.platform})...")
    print(f"  shell: {shell_cmd}")
    print(f"  ext:   {ext}")

    # Ensure .claude/ exists
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Load or create settings
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})

    # Markers for identifying our hooks (both platforms) for clean replacement
    MARKERS = (
        f"bash {plugin_rel}/hooks/",
        f"powershell -NoProfile -ExecutionPolicy Bypass -File {plugin_rel}/hooks/",
    )

    def is_our_hook(command: str) -> bool:
        return any(marker in command for marker in MARKERS)

    # Define hooks to register
    plugin_hooks = {
        "SessionStart": {
            "type": "command",
            "command": build_hook_command(shell_cmd, plugin_rel, f"session-start{ext}"),
            "timeout": 12000,
        },
        "UserPromptSubmit": {
            "type": "command",
            "command": build_hook_command(shell_cmd, plugin_rel, f"user-prompt-submit{ext}"),
            "timeout": 15000,
        },
        "Stop": {
            "type": "command",
            "command": build_hook_command(shell_cmd, plugin_rel, f"stop{ext}"),
            "timeout": 120000,
        },
        "SubagentStop": {
            "type": "command",
            "command": build_hook_command(shell_cmd, plugin_rel, f"stop{ext}"),
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

        # Remove any existing opencortex hooks (from either platform)
        new_entries = []
        for entry in entries:
            entry_hooks = entry.get("hooks", [])
            filtered = [h for h in entry_hooks if not is_our_hook(h.get("command", ""))]
            if filtered:
                entry["hooks"] = filtered
                new_entries.append(entry)

        # Add current platform hook
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
