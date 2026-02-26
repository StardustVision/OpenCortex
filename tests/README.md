# MemCortex Test Scaffold

This folder contains Phase-1 CLI flow tests for the local-first memory pipeline.

## Run

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -p "test_*.py" -v
```

## Scope

- capture -> status
- maybe-flush threshold behavior
- force flush local processing + sync outbox creation
