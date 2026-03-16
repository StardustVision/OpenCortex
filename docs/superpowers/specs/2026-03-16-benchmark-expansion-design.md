# Benchmark Expansion: QASPER + PersonaMem + HotPotQA

**Date**: 2026-03-16
**Status**: Approved

## Context

OpenCortex has a unified evaluation framework (`benchmarks/unified_eval.py`) with three adapters:
- **ConversationAdapter**: LoCoMo (10 conversations, 1986 QA) — actively tested, J-Score 0.668
- **DocumentAdapter**: QASPER / LongBench / CMRC — implemented but not yet run
- **MemoryAdapter**: PersonaMem v2 — implemented but not yet run

Cognee benchmarks on HotPotQA (multi-hop reasoning over Wikipedia) and claims top performance vs GraphRAG / LightRAG. OpenCortex needs HotPotQA coverage to enable direct comparison.

## Goals

1. Run existing QASPER + PersonaMem adapters to establish document/memory baselines (zero development)
2. Build a HotPotQA adapter with multi-hop retrieval metrics for Cognee comparison
3. Produce 3 benchmark reports covering all three evaluation modes

## Non-Goals

- Re-ingestion of LoCoMo data (already benchmarked)
- Changes to the core retrieval pipeline
- Matching Cognee's knowledge-graph architecture

---

## Phase 1: Run Existing Adapters (Zero Development)

### QASPER (Document Mode)

**Dataset**: Allen AI QASPER — academic paper QA with 4 answer types (yes/no, extractive, free-form, unanswerable).

**Data path**: `benchmarks/datasets/qasper/data.json`

**Run command**:
```bash
uv run python benchmarks/unified_eval.py \
  --mode document --dataset qasper --data benchmarks/datasets/qasper/data.json \
  --server http://127.0.0.1:8921 \
  --llm-base <base> --llm-key <key> --llm-model <model> \
  --no-thinking --top-k 10 --run-id eval_document_qasper
```

### PersonaMem v2 (Memory Mode)

**Dataset**: HuggingFace `bowen-upenn/PersonaMem-v2` — persona attribute QA.

**Data path**: `benchmarks/datasets/personamem/data.json`

**Run command**:
```bash
uv run python benchmarks/unified_eval.py \
  --mode memory --dataset personamem --data benchmarks/datasets/personamem/data.json \
  --server http://127.0.0.1:8921 \
  --llm-base <base> --llm-key <key> --llm-model <model> \
  --no-thinking --top-k 10 --run-id eval_memory_personamem
```

**Prerequisite**: Download datasets to `benchmarks/datasets/` directory.

---

## Phase 2: HotPotQA Adapter

### Data Format

HotPotQA distractor setting (`hotpot_dev_distractor_v1.json`):

```json
{
  "question": "Were Scott Derrickson and Ed Wood of the same nationality?",
  "answer": "yes",
  "type": "comparison",
  "level": "hard",
  "supporting_facts": [
    ["Scott Derrickson", 0],
    ["Ed Wood", 0]
  ],
  "context": [
    ["Scott Derrickson", ["Scott Derrickson (born...", "He is best known..."]],
    ["Ed Wood", ["Edward Davis Wood Jr. (born...", "He is often cited..."]]
  ]
}
```

Each question has 10 context paragraphs (2 gold + 8 distractors). `supporting_facts` identifies the gold evidence as `[title, sentence_index]` pairs.

### New File: `benchmarks/adapters/hotpotqa.py`

```
HotPotQAAdapter(EvalAdapter)
├── load_dataset(path)
│   Load JSON array, validate structure
│   De-duplicate paragraphs by title across all questions
│   Build title → sentences mapping
│
├── ingest(oc)
│   For each unique paragraph title:
│     oc.store(
│       abstract=title,
│       content=sentences_joined,
│       context_type="resource",
│       meta={ingest_mode: "document", source_path: f"{title}.md"}
│     )
│   Build title → URI mapping for evaluation
│
├── build_qa_items(max_qa)
│   For each question:
│     Extract gold titles from supporting_facts
│     Map gold titles → expected_uris
│     category = question["type"] ("comparison" | "bridge")
│     difficulty = question["level"]
│
├── retrieve(oc, qa_item, top_k)
│   oc.search(query=question, context_type="resource", limit=top_k)
│
└── get_baseline_context(qa_item)
    Return all 10 paragraphs concatenated (distractor setting)
```

**Ingest granularity**: One document per Wikipedia paragraph title. Sentences within a paragraph are joined into a single content block. This matches how Cognee ingests Wikipedia passages.

**De-duplication**: Multiple questions share the same paragraphs. Ingest only unique titles (expected ~60k unique for dev set).

### Scoring Extensions: `benchmarks/scoring.py`

Add:
- `exact_match(prediction, ground_truth) -> float` — Normalized exact match (lowercase, strip articles/punctuation). Standard HotPotQA metric.
- `supporting_fact_f1(retrieved_titles, gold_titles) -> float` — Set-based F1 over document titles. Measures whether retrieval found the right evidence documents.

### CLI Registration: `benchmarks/unified_eval.py`

- Add `hotpotqa` to dataset choices
- Route `--dataset hotpotqa` to `HotPotQAAdapter`
- No new `--mode` needed; HotPotQA runs under `--mode document`

### Adapter Registration: `benchmarks/adapters/__init__.py`

Export `HotPotQAAdapter` from the adapters package.

---

## Evaluation Metrics

### Per-Adapter Metrics

| Metric | QASPER | PersonaMem | HotPotQA |
|--------|--------|------------|----------|
| Answer F1 | Yes | Yes | Yes |
| Exact Match | No | No | **Yes** |
| J-Score | Yes | Yes | Yes |
| SP F1 | No | No | **Yes** |
| Joint F1 | No | No | **Yes** |
| Recall@k | Yes | Yes | Yes |
| Precision@k | Yes | Yes | Yes |
| Hit Rate@k | Yes | Yes | Yes |
| MRR | Yes | Yes | Yes |
| Latency p50/p95 | Yes | Yes | Yes |
| Token Reduction | Yes | Yes | Yes |

### Cognee Comparison

Cognee reports Answer F1 and EM on HotPotQA dev set. Direct comparison columns:

| System | Answer F1 | EM | Notes |
|--------|-----------|-----|-------|
| Cognee | (from paper) | (from paper) | GPT-4o |
| OpenCortex | (our result) | (our result) | Qwen3-235B |

LLM difference must be noted. For fair comparison, optionally re-run with GPT-4o if available.

---

## File Changes Summary

| File | Change | Lines |
|------|--------|-------|
| `benchmarks/adapters/hotpotqa.py` | **New** — HotPotQAAdapter | ~200 |
| `benchmarks/adapters/__init__.py` | **Modify** — export HotPotQAAdapter | ~5 |
| `benchmarks/scoring.py` | **Modify** — add EM + SP F1 | ~30 |
| `benchmarks/unified_eval.py` | **Modify** — register hotpotqa dataset | ~15 |

**Unchanged**: base.py, document.py, memory.py, conversation.py, report.py, metrics.py, llm_client.py, oc_client.py

---

## Output

3 JSON reports:
1. `eval_document_qasper.json`
2. `eval_memory_personamem.json`
3. `eval_document_hotpotqa.json`

Plus a comparison summary table in the benchmark report directory.
