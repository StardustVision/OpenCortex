# MemCortex Phase-1 Scaffold

This scaffold implements the agreed local-first pipeline:

`capture -> maybe_flush -> flush(local) -> sync(MCP mock)`

## What is included

- `memcortex capture`: append one event into SQLite spool
- `memcortex maybe-flush`: flush only if thresholds are met
- `memcortex flush`: reserve + process + store locally + optional sync
- `memcortex sync`: push processed records to a mock MCP outbox
- `memcortex status`: queue and sync counters

## Defaults (aligned with design decisions)

- Count threshold: 20
- Age threshold: 180 seconds
- Spool max size: 100 MB
- Lease timeout: 30 seconds
- Retry schedule: 10s -> 30s -> 120s
- Max retries: 3 (then dead letter)
- Sync only after local backlog <= 10

## Storage

- Spool DB: `$MEMCORTEX_HOME/spool/events.db`
- Lock file: `$MEMCORTEX_HOME/state/flush.lock`
- Sync outbox (mock MCP): `$MEMCORTEX_HOME/state/sync-outbox.jsonl`

## Quick start

```bash
MEMCORTEX_HOME=.memcortex-dev PYTHONPATH=src python3 -m memcortex.cli init

MEMCORTEX_HOME=.memcortex-dev PYTHONPATH=src python3 -m memcortex.cli capture \
  --source-tool claude_code \
  --session-id sess_demo \
  --event-type tool_use_end \
  --content "word to markdown conversion succeeded" \
  --meta-json "{\"project\":\"memcortex\"}"

MEMCORTEX_HOME=.memcortex-dev PYTHONPATH=src python3 -m memcortex.cli maybe-flush --local-only
MEMCORTEX_HOME=.memcortex-dev PYTHONPATH=src python3 -m memcortex.cli flush --force
MEMCORTEX_HOME=.memcortex-dev PYTHONPATH=src python3 -m memcortex.cli status
```

## LanceDB backend (optional)

This scaffold defaults to a SQLite vector store fallback for zero-dependency startup.
If you install `lancedb`, you can switch backend:

```bash
export MEMCORTEX_VECTOR_BACKEND=lancedb
export MEMCORTEX_LANCEDB_URI=.memcortex-dev/lancedb
```

## Next steps

1. Add Claude Code and Cursor hook adapters to call `capture`.
2. Replace mock sync client with real MCP transport.
3. Replace deterministic embedding with production embedding provider.
