---
date: 2026-04-25
topic: memory-benchmark-suite-three-layers
origin: conversation request for mainstream, recall, and pressure benchmark layers
---

# Plan: Three-Layer Memory Benchmark Suite

## Problem Frame
OpenCortex needs one benchmark suite that answers three different questions without mixing their metrics:

1. How do we compare with public memory-framework reports?
2. What does OpenCortex production `context_recall` add beyond raw search?
3. Does the system hold up under production-scale long-memory pressure?

The current LongMemEval mainstream path covers part of layer 1. The suite now needs explicit layer/flavor semantics, recall A/B support, and a pressure adapter path that can grow into BEAM and local production traces.

## Final Shape
The benchmark runner supports three first-class layers:

- `mainstream-search`: public-comparison layer using evidence ingest, scoped search, answer generation, and judge accuracy.
- `recall-eval`: OpenCortex production recall layer using the same evidence ingest and answer/judge flow but `context_recall` as retrieval.
- `pressure`: production pressure layer for BEAM-like bucketed data and large-scale trace datasets.

All three layers share report metadata and schema fields so results can be compared without pretending they answer the same question.

## Requirements Traceability
- R1. Add explicit benchmark layer/flavor metadata independent of dataset name.
- R2. Keep LongMemEval mainstream search compatible with public reports.
- R3. Add recall-eval A/B semantics: same ingest, same answerer, same judge, retrieval swapped to `context_recall`.
- R4. Add LoCoMo to mainstream-search/recall-eval where possible so layer 1 and layer 2 are not LongMemEval-only.
- R5. Add pressure dataset support with BEAM-compatible loader shape and bucket metadata.
- R6. Reports must include `benchmark_layer`, `benchmark_flavor`, `ingest_shape`, `retrieval_method`, `top_k`, `retrieval_cutoffs`, token/latency fields, and sample/full controls.
- R7. Tests must lock flavor resolution, sample selection, direct evidence ingest, scoped search, and pressure adapter loading.

## Implementation Units

### 1. Flavor Resolution and Runner Metadata
Files:
- `benchmarks/unified_eval.py`
- `tests/test_locomo_bench.py`

Plan:
- Extend `--benchmark-flavor` choices to `auto`, `mainstream-search`, `mainstream`, `recall-eval`, `pressure`, and `internal`.
- Normalize `mainstream` to `mainstream-search` for compatibility.
- For LongMemEval/LoCoMo with `auto`, select `mainstream-search`.
- For `recall-eval`, force evidence-style ingest where available and `retrieve_method=recall`.
- Add report metadata: `benchmark_layer`, `benchmark_flavor`, `ingest_shape`, `retrieval_cutoffs`, `per_type`, and effective top-k.

Test scenarios:
- LongMemEval auto resolves to `mainstream-search`.
- `mainstream` alias resolves to `mainstream-search`.
- `recall-eval` sets recall retrieval and keeps high-k/cutoff metadata stable.

### 2. Recall-Eval Retrieval Path
Files:
- `benchmarks/adapters/conversation.py`
- `benchmarks/adapters/locomo.py`
- `tests/test_locomo_bench.py`

Plan:
- Reuse direct evidence ingest for LongMemEval recall-eval.
- Ensure recall retrieval uses the same isolated session id as mainstream search.
- For LoCoMo, keep existing session-scoped recall but label it as recall-eval and keep ingest method explicit.
- Preserve retrieval traces for `context_recall` endpoint.

Test scenarios:
- LongMemEval recall-eval ingest uses direct evidence but retrieval calls `context_recall`.
- Retrieval contract records `method=recall`, `endpoint=context_recall`, and `session_scope=true`.
- LoCoMo route keeps recall semantics and does not regress existing store/mcp behavior.

### 3. Pressure Adapter Skeleton
Files:
- `benchmarks/adapters/beam.py`
- `benchmarks/unified_eval.py`
- `tests/test_beam_bench.py`

Plan:
- Add `BeamBench` adapter for BEAM-like JSON files.
- Support flexible records with `question`, `answer`, `bucket`/`tier`, and haystack sessions/messages.
- Use direct evidence ingest with bucket metadata and isolated `beam-item-{index}` sessions.
- Add `--beam-tier` to select a pressure bucket such as `100k`, `500k`, `1m`, or `10m`.
- Use `pressure` flavor defaults: recall retrieval unless the user overrides search.

Test scenarios:
- Beam adapter loads a minimal BEAM-like fixture.
- `beam_tier` filters records.
- Pressure ingest uses direct evidence and records bucket metadata.
- `_get_adapter` routes `dataset=beam` to `BeamBench`.

### 4. Documentation and Commands
Files:
- `docs/benchmark/memory-benchmark-suite.md`

Plan:
- Document the three layers and what each one is allowed to prove.
- Provide sample commands for LoCoMo, LongMemEval, and BEAM pressure smoke runs.
- Explain why public search comparisons and production recall evals should not be mixed.

Test scenarios:
- Documentation references real CLI flags and datasets supported by code.

## Sequencing
1. Update runner flavor resolution and metadata first.
2. Complete LongMemEval recall-eval behavior on top of the existing mainstream work.
3. Add BEAM adapter skeleton and route it through the runner.
4. Add docs and focused tests.
5. Run targeted benchmark tests and compile checks.

## Validation
- `uv run --group dev pytest tests/test_locomo_bench.py tests/test_beam_bench.py tests/test_http_server.py::TestHTTPServer::test_04d_benchmark_conversation_ingest_preserves_traceability_contract tests/test_http_server.py::TestHTTPServer::test_04e_benchmark_conversation_ingest_direct_evidence_shape -q`
- `uv run python -m py_compile benchmarks/adapters/conversation.py benchmarks/adapters/locomo.py benchmarks/adapters/beam.py benchmarks/unified_eval.py benchmarks/oc_client.py src/opencortex/http/models.py src/opencortex/http/server.py src/opencortex/context/manager.py`

## Non-Goals
- Do not remove internal experiments.
- Do not claim pressure results are comparable to public LongMemEval reports.
- Do not require a full BEAM download to validate adapter behavior.
