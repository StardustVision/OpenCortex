---
name: claude-hooks-simulator
description: Simulate Claude Code hook lifecycle behavior for OpenCortex inside Codex by manually triggering session-start, user-prompt-submit, stop, and session-end through oc-cli. Use when you need to test hook outputs, debug hook-side effects, or reproduce hook-driven memory flow without Claude's native hook events.
allowed-tools: Bash
---

# Claude Hooks Simulator

Use `npx opencortex-cli` to trigger hooks manually.

This skill uses the new CLI commands:
- `hook <name>` for one hook
- `simulate` for full lifecycle

## Workflow

1. Use full lifecycle simulation first.
```bash
npx opencortex-cli simulate \
  --prompt "测试记忆召回流程"
```

2. Use single-hook mode for focused debugging.
```bash
npx opencortex-cli hook user-prompt-submit \
  --prompt "这个问题要不要召回历史记忆"
```

3. For `stop` hook validation, pass a real transcript if available.
```bash
npx opencortex-cli hook stop \
  --transcript /path/to/transcript.jsonl
```

## Output Expectations

- `session-start`: starts/checks server and writes `.opencortex/memory/session_state.json`
- `user-prompt-submit`: returns recall guidance message or empty result
- `stop`: ingests last turn from transcript (best-effort, often empty on missing transcript)
- `session-end`: stores session summary and marks state inactive
