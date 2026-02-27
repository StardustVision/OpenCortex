---
name: memory-feedback
description: Provide positive or negative feedback on a memory to adjust its future retrieval ranking via reinforcement learning. Use when a recalled memory was helpful (+1) or unhelpful (-1).
context: fork
allowed-tools: Bash
---

You are a memory feedback sub-agent for OpenCortex memory.

## Goal
Submit reinforcement learning feedback for a memory: $ARGUMENTS

## Steps

1. Determine the HTTP server URL from session state.
```bash
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
STATE_FILE="$PROJECT_DIR/.opencortex/memory/session_state.json"
HTTP_URL=$(python3 -c "import json; print(json.load(open('$STATE_FILE')).get('http_url','http://127.0.0.1:8921'))" 2>/dev/null || echo "http://127.0.0.1:8921")
```

2. Parse the URI and reward value from user arguments.
   - Positive reward (+1.0): memory was helpful, boost future ranking.
   - Negative reward (-1.0): memory was irrelevant, lower future ranking.

3. Submit feedback via HTTP API.
```bash
curl -sf -X POST "$HTTP_URL/api/v1/memory/feedback" \
  -H "Content-Type: application/json" \
  -d '{"uri": "<memory-uri>", "reward": <+1.0 or -1.0>}'
```

4. Confirm the feedback was applied.

## Output rules
- Parse the URI from the arguments (e.g., `opencortex://...`).
- Default to +1.0 for "helpful/good" and -1.0 for "unhelpful/bad".
- Report: "Feedback applied: {uri} reward={reward}"
