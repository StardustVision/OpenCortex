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
│   De-duplicate paragraphs by title (first-occurrence wins; log warning on content mismatch)
│   Build title → sentences mapping
│   Build question_id → original_context mapping (for baseline context lookup)
│   Assign synthetic question IDs (index-based) since HotPotQA has no unique IDs
│
├── ingest(oc)
│   For each unique paragraph title:
│     oc.store(
│       abstract=title,
│       content=sentences_joined,
│       context_type="resource",
│       meta={}  # memory mode pass-through, no LLM overhead
│     )
│   Use asyncio.Semaphore(20) for concurrent ingestion (~60k paragraphs)
│   Build title → URI mapping for evaluation
│
├── build_qa_items(**kwargs)   # accepts max_qa from kwargs, per base class pattern
│   For each question:
│     Extract gold titles from supporting_facts
│     Map gold titles → expected_uris
│     category = question["type"] ("comparison" | "bridge")
│     difficulty = question["level"]
│     meta["question_id"] = synthetic ID (for baseline context lookup)
│     meta["gold_titles"] = set of supporting fact titles (for SP F1)
│
├── retrieve(oc, qa_item, top_k)
│   oc.search(query=question, context_type="resource", limit=top_k)
│
└── get_baseline_context(qa_item)
    Look up qa_item.meta["question_id"] → original 10 paragraphs, concatenated
```

**Ingest granularity**: One record per Wikipedia paragraph title. Sentences within a paragraph are joined into a single content block. Uses **memory mode** (pass-through, no LLM chunking) since paragraphs are short (2-5 sentences). This avoids the document-mode LLM overhead that would be prohibitive at ~60k items.

**De-duplication**: Multiple questions share the same paragraphs. First-occurrence wins; if a later question has a different sentence list for the same title, log a warning and skip. Expected ~60k unique titles for dev set.

**Concurrent ingestion**: `asyncio.Semaphore(20)` limits concurrent `oc.store()` calls. At ~50ms/call, ~60k items ≈ 3 minutes.

**Constraint**: `--skip-ingest` breaks SP F1 and retrieval metrics because the title→URI mapping is built during ingestion. SP F1 requires ingestion in the same run.

### Scoring Extensions: `benchmarks/scoring.py`

Add:
- `exact_match(prediction, ground_truth) -> float` — Normalized exact match (lowercase, strip articles/punctuation). Standard HotPotQA metric.
- `supporting_fact_f1(retrieved_titles, gold_titles) -> float` — Set-based F1 over document titles. Measures whether retrieval found the right evidence documents.

### CLI Routing: `benchmarks/unified_eval.py`

The current `_get_adapter(mode)` dispatches by mode only. Must change to `_get_adapter(mode, dataset)` to support dataset-specific adapters within the same mode:

```python
def _get_adapter(mode: str, dataset: str = ""):
    # Dataset-specific adapters take priority
    if dataset == "hotpotqa":
        from benchmarks.adapters.hotpotqa import HotPotQAAdapter
        return HotPotQAAdapter()
    # Default mode-based routing
    if mode == "memory":
        from benchmarks.adapters.memory import MemoryAdapter
        return MemoryAdapter()
    elif mode == "conversation":
        from benchmarks.adapters.conversation import ConversationAdapter
        return ConversationAdapter()
    elif mode == "document":
        from benchmarks.adapters.document import DocumentAdapter
        return DocumentAdapter()
    raise ValueError(f"Unknown mode: {mode}")
```

Update `--dataset` help text to include `hotpotqa`. Update the caller to pass `dataset` argument.

### Scoring Integration in `eval_one`

HotPotQA-specific metrics (EM, SP F1, Joint F1) are computed inside `eval_one` when the adapter is `HotPotQAAdapter`. The per-query record gains extra fields:

```python
# Inside eval_one, after standard F1 computation:
if hasattr(adapter, 'is_hotpotqa') and adapter.is_hotpotqa:
    from benchmarks.scoring import exact_match, supporting_fact_f1
    record["oc_em"] = exact_match(oc_pred, qa_item.answer)
    record["bl_em"] = exact_match(bl_pred, qa_item.answer)
    # SP F1: compare retrieved doc titles vs gold titles
    retrieved_titles = {r.get("abstract", "") for r in oc_results}
    gold_titles = qa_item.meta.get("gold_titles", set())
    record["sp_f1"] = supporting_fact_f1(retrieved_titles, gold_titles)
    record["joint_f1"] = record["oc_f1"] * record["sp_f1"]
```

These fields are aggregated in the report's accuracy section when present.

### Adapter Import Pattern

Follow existing pattern: import directly in `unified_eval.py` (not via `__init__.py`), consistent with DocumentAdapter/MemoryAdapter/ConversationAdapter.

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
| `benchmarks/scoring.py` | **Modify** — add EM + SP F1 | ~30 |
| `benchmarks/unified_eval.py` | **Modify** — `_get_adapter(mode, dataset)` routing + hotpotqa dataset + EM/SP/Joint in eval_one | ~40 |

**Unchanged**: base.py, document.py, memory.py, conversation.py, report.py, metrics.py, llm_client.py, oc_client.py, adapters/__init__.py

---

## Output

3 JSON reports:
1. `eval_document_qasper.json`
2. `eval_memory_personamem.json`
3. `eval_document_hotpotqa.json`

Plus a comparison summary table in the benchmark report directory.
