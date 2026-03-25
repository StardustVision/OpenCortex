# opencortex-memory

OpenCortex MCP server package for Claude Code, Codex, and other MCP clients.

## Install

**Claude Code:**

```
/plugin install
```

Select `opencortex-memory`, then run:

```bash
npx opencortex-cli setup
```

The setup wizard will guide you through local/remote mode, server URL, and JWT token configuration.

**Codex CLI:**

```bash
codex mcp add opencortex -- npx -y opencortex-memory
npx opencortex-cli setup
```

**Run directly:**

```bash
npx -y opencortex-memory
```

## Verify

```bash
npx opencortex-cli health
```

## Configuration

The setup wizard writes config to `~/.opencortex/mcp.json`. You can also edit it manually:

```json
{
  "mode": "remote",
  "token": "<jwt-token>",
  "remote": { "http_url": "http://your-server:8921" }
}
```

Config search order: `./mcp.json` (project) > `~/.opencortex/mcp.json` (global).

Environment variable overrides: `OPENCORTEX_MODE`, `OPENCORTEX_HTTP_URL`, `OPENCORTEX_TOKEN`, `OPENCORTEX_HTTP_PORT`.

## Included binaries

- `opencortex-mcp` — MCP stdio server
- `opencortex-cli` — CLI (setup, health, store, recall, status, feedback, decay)
