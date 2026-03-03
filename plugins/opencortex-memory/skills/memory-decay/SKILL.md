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

1. Trigger decay via OpenCortex CLI.
```bash
npx opencortex-cli decay
```

2. Report the decay results.

## Output rules
- Report: records processed, decayed, below threshold, archived.
- Decay rates: normal=0.95, protected=0.99, threshold=0.01.
- Protected memories decay slower (set via memory-feedback).
