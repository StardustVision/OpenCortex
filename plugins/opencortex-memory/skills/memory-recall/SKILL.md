---
name: memory-recall
description: Recall relevant long-term memories from OpenCortex. Use when the user asks about past decisions, prior fixes, historical context, or what was done in earlier sessions.
context: fork
allowed-tools: Bash
---

You are a memory retrieval sub-agent for OpenCortex memory.

## Goal
Find the most relevant historical memories for: $ARGUMENTS

## Steps

1. Determine the HTTP server URL from session state.
```bash
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
STATE_FILE="$PROJECT_DIR/.opencortex/memory/session_state.json"
HTTP_URL=$(python3 -c "import json; print(json.load(open('$STATE_FILE')).get('http_url','http://127.0.0.1:8921'))" 2>/dev/null || echo "http://127.0.0.1:8921")
echo "HTTP server: $HTTP_URL"
```

2. Run memory search via HTTP API.
```bash
curl -sf -X POST "$HTTP_URL/api/v1/memory/search" \
  -H "Content-Type: application/json" \
  -d '{"query": "'"$ARGUMENTS"'", "limit": 5}'
```

3. Evaluate results and keep only truly relevant memories.
4. Return a concise curated summary to the main agent.

## Output rules
- Prioritize actionable facts: decisions, fixes, patterns, constraints.
- Include source URIs for traceability.
- If nothing useful appears, respond exactly: `No relevant memories found.`
