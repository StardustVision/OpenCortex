---
name: memory-store
description: Store a new memory into OpenCortex. Use when explicitly asked to remember something, save a decision, or record an important fact for future reference.
context: fork
allowed-tools: Bash
---

You are a memory storage sub-agent for OpenCortex memory.

## Goal
Store the following information as a long-term memory: $ARGUMENTS

## Steps

1. Determine the HTTP server URL from session state.
```bash
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
STATE_FILE="$PROJECT_DIR/.opencortex/memory/session_state.json"
HTTP_URL=$(python3 -c "import json; print(json.load(open('$STATE_FILE')).get('http_url','http://127.0.0.1:8921'))" 2>/dev/null || echo "http://127.0.0.1:8921")
```

2. Compose a clear abstract (1-line summary) and detailed content from the user's request.

3. Store via HTTP API.
```bash
curl -sf -X POST "$HTTP_URL/api/v1/memory/store" \
  -H "Content-Type: application/json" \
  -d '{
    "abstract": "<one-line summary of what to remember>",
    "content": "<detailed content with context>",
    "category": "<appropriate category: decision|pattern|fact|fix|preference>",
    "context_type": "memory",
    "meta": {"source": "skill:memory-store", "timestamp": '"$(date +%s)"'}
  }'
```

4. Confirm storage to the user with the returned URI.

## Output rules
- Write a clear, specific abstract that will be useful for future search.
- Include enough context in content so the memory is self-contained.
- Choose the most appropriate category.
- Report back: "Stored as: {uri}"
