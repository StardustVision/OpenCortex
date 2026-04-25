---
date: 2026-04-23
topic: benchmark-offline-conversation-ingest
---

# Benchmark Offline Conversation Ingest

## Problem Frame

Current LoCoMo and LongMemEval benchmark ingestion replays the online conversation lifecycle (`context_commit` + `context_end`) for every benchmark conversation. That path now includes merge follow-up work, full-session recomposition, and session-summary generation, which makes full benchmark runs operationally impractical. The benchmark goal is to measure conversation retrieval and QA quality against the conversation-mode result shape, not to measure the online ingest pipeline's wall-clock cost.

The user wants benchmark ingestion to stop replaying the full online lifecycle while keeping the resulting conversation record shape close enough to current conversation mode that benchmark results remain meaningful.

## Requirements

**Benchmark Scope**
- R1. The change MUST apply only to benchmark ingestion paths for conversation datasets such as LoCoMo and LongMemEval.
- R2. Existing production and development conversation-mode runtime behavior MUST remain unchanged.
- R3. Benchmark retrieval, QA generation, and scoring paths MUST continue to use the existing evaluation flow after ingestion completes.

**Offline Result Shape**
- R4. Benchmark ingestion MUST support building conversation records offline from a full benchmark conversation/session input instead of replaying per-turn `context_commit` and synchronous `context_end`.
- R5. The offline ingest path MUST produce merged conversation leaves whose metadata contract remains compatible with current conversation retrieval and benchmark URI mapping, including `session_id`, `msg_range`, and `source_uri` when required by downstream consumers.
- R6. The offline ingest path MUST reuse the current full-session recomposition logic so that benchmark-generated directory records stay as close as possible to current conversation-mode `full_recompose`.
- R7. The offline ingest path MUST preserve enough original conversation text in merged leaves that downstream `full_recompose` continues to operate on conversation text rather than only compressed summaries.

**Benchmark Correctness and Operability**
- R8. LoCoMo benchmark URI/session mapping MUST remain valid under the offline ingest path.
- R9. LongMemEval benchmark URI/session mapping MUST remain valid under the offline ingest path.
- R10. The benchmark ingest path MUST avoid per-item full-collection snapshot scans whose cost grows with the total collection size.
- R11. The new path MUST make full LoCoMo and LongMemEval runs operationally feasible on local benchmark infrastructure without waiting for the full online session-end pipeline for each benchmark item.

## Success Criteria

- A full LoCoMo benchmark run can be ingested through the offline conversation benchmark path while preserving valid retrieval and QA scoring outputs.
- A full LongMemEval benchmark run can be ingested through the offline conversation benchmark path without the current multi-hour-per-small-fraction stall pattern.
- Benchmark-side ground-truth URI mapping still resolves correctly for both LoCoMo and LongMemEval.
- Resulting benchmark conversation records still support current recall/search evaluation without requiring production conversation-mode changes.

## Scope Boundaries

- Do not change production conversation-mode API semantics or runtime lifecycle behavior.
- Do not redesign online `context_commit`, `context_end`, merge follow-up, or session-end orchestration for normal users.
- Do not broaden this work into a general-purpose offline conversation import feature unless planning later decides it is a cheap extension.
- Do not change benchmark scoring methodology as part of this work.

## Key Decisions

- Benchmark-only path: The new ingest path is benchmark-specific because the immediate problem is benchmark operability, not product runtime behavior.
- Reuse current `full_recompose`: The benchmark path should align to current conversation-mode end-state by reusing the existing recomposition step instead of inventing a different directory-building algorithm.
- Preserve merged-leaf compatibility: The key compatibility target is not replaying online timing, but reproducing a conversation record shape that existing retrieval and mapping logic already understands.

## Dependencies / Assumptions

- Current benchmark LoCoMo and LongMemEval adapters rely on session-level URI mapping derived from stored records, verified in `benchmarks/adapters/locomo.py` and `benchmarks/adapters/conversation.py`.
- Current conversation recomposition and summary logic depend on `merged` records carrying valid `msg_range` and text-bearing fields, verified in `src/opencortex/context/manager.py`.

## Outstanding Questions

### Deferred to Planning
- [Affects R4-R7][Technical] What is the narrowest internal interface for writing offline merged leaves without reusing online `context_commit`?
- [Affects R8-R9][Technical] Should benchmark adapters map session evidence from directly returned offline-ingest artifacts rather than any post-hoc store diff?
- [Affects R11][Needs research] Should benchmark offline ingest skip `session_summary`, or keep it behind an option once baseline performance is acceptable?

## Next Steps

-> /ce:plan for structured implementation planning
