#!/usr/bin/env python3
"""Cross-platform hook dispatcher for OpenCortex Claude Code plugin.

Detects the current platform and runs the appropriate hook script:
- macOS/Linux: bash <hook>.sh
- Windows: powershell -NoProfile -ExecutionPolicy Bypass -File <hook>.ps1

Usage:
    python3 hook_runner.py <hook-name>
    e.g. python3 hook_runner.py session-start
"""

import os
import platform
import subprocess
import sys


def main():
    if len(sys.argv) < 2:
        print('{"error": "hook name required"}', file=sys.stderr)
        sys.exit(1)

    hook_name = sys.argv[1]
    hooks_dir = os.path.dirname(os.path.abspath(__file__))

    # Read stdin (hook input JSON) before spawning subprocess
    stdin_data = b""
    if not sys.stdin.isatty():
        try:
            stdin_data = sys.stdin.buffer.read()
        except Exception:
            pass

    if platform.system() == "Windows":
        script = os.path.join(hooks_dir, f"{hook_name}.ps1")
        if not os.path.isfile(script):
            print(f'{{"error": "script not found: {hook_name}.ps1"}}')
            sys.exit(1)
        cmd = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", script,
        ]
    else:
        script = os.path.join(hooks_dir, f"{hook_name}.sh")
        if not os.path.isfile(script):
            print(f'{{"error": "script not found: {hook_name}.sh"}}')
            sys.exit(1)
        cmd = ["bash", script]

    # Propagate environment variables that Claude Code sets
    env = os.environ.copy()
    plugin_root = env.get("CLAUDE_PLUGIN_ROOT", os.path.dirname(hooks_dir))
    env.setdefault("CLAUDE_PLUGIN_ROOT", plugin_root)

    result = subprocess.run(
        cmd,
        input=stdin_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    # Forward stdout (hook JSON output) and stderr
    if result.stdout:
        sys.stdout.buffer.write(result.stdout)
        sys.stdout.buffer.flush()
    if result.stderr:
        sys.stderr.buffer.write(result.stderr)
        sys.stderr.buffer.flush()

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
