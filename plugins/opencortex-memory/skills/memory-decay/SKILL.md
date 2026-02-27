---
name: memory-decay
description: Apply reinforcement learning reward decay to all stored memories. Reduces reward scores over time so only consistently valuable memories retain high ranking.
context: fork
allowed-tools: Bash
---

You are a memory maintenance sub-agent for OpenCortex memory.

## Goal
Apply reward decay to all stored memories.

## Steps

1. Determine the HTTP server URL from session state.
```bash
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
STATE_FILE="$PROJECT_DIR/.opencortex/memory/session_state.json"
HTTP_URL=$(python3 -c "import json; print(json.load(open('$STATE_FILE')).get('http_url','http://127.0.0.1:8921'))" 2>/dev/null || echo "http://127.0.0.1:8921")
```

2. Trigger decay via HTTP API.
```bash
curl -sf -X POST "$HTTP_URL/api/v1/memory/decay" \
  -H "Content-Type: application/json" \
  -d '{}'
```

3. Report the decay results.

## Output rules
- Report: records processed, decayed, below threshold, archived.
- Decay rates: normal=0.95, protected=0.99, threshold=0.01.
- Protected memories decay slower (set via memory-feedback).
