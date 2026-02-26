# MemCortex

MemCortex is a local-first memory pipeline scaffold for AI coding tools.

Current implementation focus (Phase-1 scaffold):

- Hook-driven capture from clients (initially Claude Code and Cursor)
- SQLite spool queue with retries and dead-letter handling
- Threshold-based micro-batch flush
- Local vector storage write path
- Mock MCP sync channel for remote learning integration

## Architecture (current scaffold)

`capture -> maybe_flush -> flush(local) -> sync(remote MCP mock)`

See detailed design notes in:

- `_bmad-output/memcortex-architecture.md`
- `docs/phase1-scaffold.md`

## Quick start

```bash
MEMCORTEX_HOME=.memcortex-dev PYTHONPATH=src python3 -m memcortex.cli init

MEMCORTEX_HOME=.memcortex-dev PYTHONPATH=src python3 -m memcortex.cli capture \
  --source-tool claude_code \
  --session-id sess_001 \
  --event-type tool_use_end \
  --content "word to markdown conversion succeeded" \
  --meta-json "{\"project\":\"memcortex\"}"

MEMCORTEX_HOME=.memcortex-dev PYTHONPATH=src python3 -m memcortex.cli maybe-flush --local-only
MEMCORTEX_HOME=.memcortex-dev PYTHONPATH=src python3 -m memcortex.cli flush --force
MEMCORTEX_HOME=.memcortex-dev PYTHONPATH=src python3 -m memcortex.cli status
```

## CLI commands

- `memcortex init`
- `memcortex capture`
- `memcortex maybe-flush`
- `memcortex flush`
- `memcortex sync`
- `memcortex status`

## Hook examples

- `examples/hooks/claude-code/observe.sh`
- `examples/hooks/cursor/observe.sh`

## Notes

- Default vector backend is SQLite fallback for zero-dependency startup.
- LanceDB backend is supported when `lancedb` package is installed and env vars are set.
