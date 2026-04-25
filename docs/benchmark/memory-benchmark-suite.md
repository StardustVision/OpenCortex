# Memory Benchmark Suite

OpenCortex memory benchmarks are split into three layers. The layers use related datasets, but they answer different questions and should not be mixed in a single headline score.

## Layer 1: Public Comparison (`mainstream-search`)

Use this layer to compare with public memory-framework reports such as Mem0, Zep, Supermemory, and Hindsight-style results.

Shape:

1. Ingest the dataset haystack as searchable evidence.
2. Retrieve with scoped `memory/search`.
3. Build answer context from top-k evidence.
4. Generate an answer with the configured answer model.
5. Score the answer with the LLM judge.

Example LongMemEval command:

```bash
uv run python benchmarks/unified_eval.py \
  --mode conversation \
  --dataset longmemeval \
  --data benchmarks/datasets/longmemeval/longmemeval_s_cleaned.json \
  --benchmark-flavor mainstream-search \
  --ingest-method store \
  --retrieve-method search \
  --top-k 200 \
  --retrieval-cutoffs 10,20,50,200 \
  --concurrency 2 \
  --llm-base "$LLM_BASE" \
  --llm-key "$LLM_KEY" \
  --llm-model gpt-5.2
```

## Layer 2: Production Recall (`recall-eval`)

Use this layer to evaluate OpenCortex production recall. It should use the same ingest, answerer, and judge as `mainstream-search`, but swap retrieval to `context_recall`.

This answers whether production recall can reach useful answer accuracy with less context, lower top-k, better scope control, or cleaner context than raw search.

Example LongMemEval command:

```bash
uv run python benchmarks/unified_eval.py \
  --mode conversation \
  --dataset longmemeval \
  --data benchmarks/datasets/longmemeval/longmemeval_s_cleaned.json \
  --benchmark-flavor recall-eval \
  --ingest-method store \
  --retrieve-method recall \
  --top-k 10 \
  --retrieval-cutoffs 5,10,20 \
  --concurrency 2 \
  --llm-base "$LLM_BASE" \
  --llm-key "$LLM_KEY" \
  --llm-model gpt-5.2
```

## Layer 3: Pressure (`pressure`)

Use this layer for BEAM-like or production-trace pressure runs. It measures whether ingest, retrieval, latency, and answer quality hold up as memory size grows.

Pressure results should be reported by bucket or tier, for example `100k`, `500k`, `1m`, and `10m` token tiers when the dataset provides them.

Example BEAM smoke command:

```bash
uv run python benchmarks/unified_eval.py \
  --mode conversation \
  --dataset beam \
  --data benchmarks/datasets/beam/sample.json \
  --benchmark-flavor pressure \
  --beam-tier 100k \
  --retrieve-method recall \
  --top-k 20 \
  --concurrency 2 \
  --llm-base "$LLM_BASE" \
  --llm-key "$LLM_KEY" \
  --llm-model gpt-5.2
```

## Reading Reports

Important metadata fields:

- `benchmark_layer`: `public_comparison`, `production_recall`, `pressure`, or `internal`.
- `benchmark_flavor`: exact flavor selected by CLI or `auto` resolution.
- `ingest_shape`: direct evidence, merged recomposition, or dataset-specific ingest shape.
- `retrieve_method`: effective retrieval path used by the adapter.
- `retrieval_cutoffs`: cutoffs used for diagnostic retrieval metrics.
- `top_k`: effective retrieval top-k used to build answer context.
- `per_type` / `beam_tier`: sample controls when present.

Use `mainstream-search` for public comparisons. Use `recall-eval` to explain OpenCortex recall value. Use `pressure` to find scale and degradation limits.
