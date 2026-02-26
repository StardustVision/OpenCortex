---
name: 'opencortex-mcp'
description: 'Start the OpenCortex MCP memory server for AI agents. Supports stdio/sse/http transport with configurable tenant and user.'
allowed-tools: Bash(python:*),Read
---

# OpenCortex MCP Server

Start the OpenCortex MCP server to expose memory tools to external AI agents.

## Instructions

1. Check if an `opencortex.json` config file exists in the project root. If not, ask the user for:
   - `tenant_id` (team/organization identifier)
   - `user_id` (user identifier)
   - Transport mode: stdio (default), sse, or streamable-http
   - Port (default 8920, only for sse/http)

2. Create or update `opencortex.json` with the provided values:
```json
{
  "tenant_id": "<tenant>",
  "user_id": "<user>",
  "data_root": "./data",
  "embedding_dimension": 1024,
  "ruvector_host": "127.0.0.1",
  "ruvector_port": 6921,
  "mcp_transport": "stdio",
  "mcp_port": 8920
}
```

3. Start the MCP server:
```bash
python -m opencortex.mcp_server --config opencortex.json --transport <transport> --port <port>
```

4. Report the available tools:
   - `memory_store` — Store memories/resources/skills
   - `memory_search` — Semantic search
   - `memory_feedback` — SONA reinforcement feedback
   - `memory_stats` — System statistics
   - `memory_decay` — Time-decay trigger
   - `memory_health` — Health check

$ARGUMENTS
