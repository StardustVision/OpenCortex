#!/usr/bin/env node
/**
 * Cross-platform hook dispatcher for OpenCortex Claude Code plugin.
 *
 * Detects platform and spawns the correct shell script:
 *   - macOS/Linux → bash <hook>.sh
 *   - Windows     → powershell -NoProfile -ExecutionPolicy Bypass -File <hook>.ps1
 *
 * Usage: node hook_runner.mjs <hook-name>
 *   e.g. node hook_runner.mjs session-start
 */

import { spawn } from "node:child_process";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { platform } from "node:os";

const __dirname = dirname(fileURLToPath(import.meta.url));
const hookName = process.argv[2];

if (!hookName) {
  process.stderr.write('{"error": "hook name required"}\n');
  process.exit(1);
}

// Collect stdin before spawning child
const chunks = [];
process.stdin.on("data", (chunk) => chunks.push(chunk));
process.stdin.on("end", () => {
  const stdinData = Buffer.concat(chunks);

  let cmd, args;
  if (platform() === "win32") {
    const script = join(__dirname, `${hookName}.ps1`);
    cmd = "powershell";
    args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script];
  } else {
    const script = join(__dirname, `${hookName}.sh`);
    cmd = "bash";
    args = [script];
  }

  const child = spawn(cmd, args, {
    env: {
      ...process.env,
      CLAUDE_PLUGIN_ROOT:
        process.env.CLAUDE_PLUGIN_ROOT || join(__dirname, ".."),
    },
    stdio: ["pipe", "pipe", "pipe"],
  });

  child.stdin.write(stdinData);
  child.stdin.end();

  child.stdout.pipe(process.stdout);
  child.stderr.pipe(process.stderr);

  child.on("close", (code) => process.exit(code ?? 0));
});

// Handle empty stdin (no piped data)
if (process.stdin.isTTY) {
  process.stdin.emit("end");
}
