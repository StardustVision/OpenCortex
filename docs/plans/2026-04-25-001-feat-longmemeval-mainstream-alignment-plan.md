---
date: 2026-04-25
topic: longmemeval-mainstream-alignment
origin: docs/brainstorms/longmemeval-mainstream-eval-alignment.md
---

# Plan: LongMemEval Mainstream Evaluation Alignment

## Problem Frame
OpenCortex currently runs LongMemEval through the generic conversation benchmark path. That path is useful for internal recall/recomposition experiments, but it is not the benchmark shape used by mainstream memory frameworks. A comparable LongMemEval run needs isolated question haystacks, explicit evidence-unit ingest, high-k scoped search retrieval, LLM answer generation, and type-aware LLM judge accuracy as the headline score.

## Final Shape
LongMemEval becomes a first-class benchmark flavor named `mainstream`. It is not a thin flag over the current conversation flow.

The default mainstream flow is:
1. Select LongMemEval items by full run, `--per-type`, and/or `--max-qa`.
2. For each item, create an isolated benchmark session `lme-item-{index}`.
3. Convert haystack sessions into date-preserving user/assistant pair evidence units.
4. Store those evidence units directly as searchable memories, without full session recomposition or summary generation.
5. Retrieve with scoped search over the isolated session, using high-k cutoffs such as `10,20,50,200`.
6. Build answer context from retrieved evidence.
7. Score generated answer with existing LongMemEval-aware J-score judge prompts.
8. Report answer accuracy as headline and retrieval metrics as diagnostics.

## Scope
Implement the final benchmark-aligned path while preserving existing generic `store`, `mcp`, and `recall` behavior for internal experiments.

## Requirements Traceability
- R1-R3: Add direct evidence-unit ingest that isolates each question and avoids whole-haystack full recomposition.
- R4-R6: Add scoped high-k search retrieval and feed retrieved evidence into answer generation.
- R7-R9: Label reports with benchmark flavor, ingest shape, cutoffs, and model metadata.
- R10-R12: Support sample/full modes and keep internal recall distinct.

## Implementation Units

### 1. Benchmark Evidence Ingest API
Files:
- `src/opencortex/http/models.py`
- `src/opencortex/http/server.py`
- `src/opencortex/context/manager.py`
- `benchmarks/oc_client.py`
- `tests/test_http_server.py`

Plan:
- Extend benchmark conversation ingest to support an ingest shape that writes normalized segments directly as searchable evidence records.
- The shape must preserve `session_id`, `source_uri`, `msg_range`, `event_date`, `time_refs`, `lme_item_index`, `lme_session_id`, and `lme_segment_kind` metadata.
- Direct evidence ingest must not invoke full session recomposition or session summary generation.
- Return exported records so adapters can map LongMemEval answer sessions to stored URIs.

Test scenarios:
- Direct benchmark ingest returns records with traceability metadata.
- Direct benchmark ingest does not create a session summary.
- Existing merged offline ingest behavior remains covered by the current HTTP benchmark test.

### 2. LongMemEval Mainstream Adapter
Files:
- `benchmarks/adapters/conversation.py`
- `tests/test_locomo_bench.py`

Plan:
- Add a mainstream ingest method, e.g. `longmemeval-mainstream`, as the LongMemEval default when dataset is LongMemEval.
- Convert haystack sessions into pair evidence units: user+assistant pairs where possible, otherwise a single leftover message.
- Attach item/session/date metadata to each message.
- Maintain mapping from `answer_session_ids` to stored evidence URIs.
- Add `per_type` sampling and ensure ingest/build QA select the same items.
- Keep `store` and `mcp` available for internal comparison.

Test scenarios:
- Pair conversion produces one segment per user/assistant pair and preserves dates/session ids.
- `per_type=1` selects at most one item per question type for both ingest and QA.
- Mainstream ingest calls direct benchmark evidence ingest and does not call commit/end.

### 3. Scoped Search Retrieval
Files:
- `src/opencortex/http/models.py`
- `src/opencortex/http/server.py`
- `benchmarks/oc_client.py`
- `benchmarks/adapters/conversation.py`
- Existing search tests or new focused benchmark tests

Plan:
- Add benchmark-safe metadata filters to memory search, sufficient to restrict LongMemEval search to `session_id=lme-item-{index}`.
- Keep this as a general optional search filter, not hardcoded to LongMemEval.
- LongMemEval mainstream retrieval should use scoped search with `session_id` filter and `top_k` up to 200.

Test scenarios:
- Search client sends metadata filters when requested.
- LongMemEval mainstream retrieval uses scoped search and records retrieval contract metadata.
- Existing unfiltered search calls are unchanged.

### 4. Runner Metrics and Reporting
Files:
- `benchmarks/unified_eval.py`
- `benchmarks/metrics.py` if needed
- New or existing benchmark unit tests

Plan:
- Add `--benchmark-flavor` with `auto`, `mainstream`, and `internal` options.
- For LongMemEval `auto`, choose mainstream defaults.
- Add `--retrieval-cutoffs` defaulting to `10,20,50,200` for LongMemEval mainstream and `[1,3,5]` otherwise.
- Compute retrieval metrics, NDCG, and confidence intervals using configured cutoffs.
- Preserve answer J-score as headline accuracy and include method metadata in reports.

Test scenarios:
- Cutoff parser handles comma-delimited values.
- LongMemEval mainstream metadata includes flavor, ingest shape, cutoffs, and `per_type`.
- Retrieval metrics use the configured cutoffs.

## Validation
- `uv run --group dev pytest tests/test_locomo_bench.py tests/test_http_server.py::TestHTTPServer::test_04d_benchmark_conversation_ingest_preserves_traceability_contract -q`
- Add and run any new focused tests for search filters and cutoff parsing.
- Run a small LongMemEval sample with `--per-type 1`, mainstream flavor, scoped search, and low concurrency.

## Non-Goals
- Do not remove the internal recall path.
- Do not claim comparability for old `full recompose + recall top_k=5` reports.
- Do not copy private vendor implementation details.
