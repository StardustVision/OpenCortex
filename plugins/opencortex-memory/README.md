# opencortex-memory

OpenCortex MCP server package for Codex, Claude, and other MCP clients.

## Quick start

Use with Codex CLI:

```bash
codex mcp add opencortex -- npx -y opencortex-memory
```

Or run directly:

```bash
npx -y opencortex-memory
```

The server reads MCP client config from `mcp.json` in your project, then falls back to `~/.opencortex/mcp.json`.

## Included binaries

- `opencortex-mcp` (default MCP stdio server entrypoint)
- `opencortex-cli` (utility CLI for health/store/recall/status)

