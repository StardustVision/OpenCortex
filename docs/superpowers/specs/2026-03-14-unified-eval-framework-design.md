# Unified Evaluation Framework Design

## Goal

Build a unified evaluation framework for OpenCortex covering all three ingestion modes (memory, conversation, document) with three result dimensions: QA accuracy, context token reduction, and recall latency.

## Background

OpenCortex currently has two separate eval tools:

- `eval/locomo_eval.py` — conversation mode only (LoCoMo dataset, F1 scoring, token comparison, no latency)
- `tests/benchmark/runner.py` — memory mode retrieval-only (50 memories, recall@k/precision@k/MRR, no LLM QA)

Neither tool covers document mode. Neither tracks recall latency. The framework needs unification to produce consistent, comparable results across all three modes.

## Datasets

| Mode | Dataset | Source | Size | Languages |
|------|---------|--------|------|-----------|
| Memory | PersonaMem v2 | [HuggingFace](https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2) | Multi-session dialogs + persona QA | EN |
| Conversation | LoCoMo | [GitHub](https://github.com/snap-research/locomo) | 10 conversations, ~300 turns each, 5 QA categories | EN |
| Conversation | LongMemEval | [GitHub](https://github.com/xiaowu0162/LongMemEval) | 500 QA, 115k-1.5M tokens | EN |
| Document | QASPER | [HuggingFace](https://huggingface.co/datasets/allenai/qasper) | 5,049 QA over 1,585 NLP papers | EN |
| Document | LongBench | [HuggingFace](https://huggingface.co/datasets/zai-org/LongBench) | 21 datasets, bilingual | EN + ZH |
| Document | CMRC 2018 | [GitHub](https://github.com/ymcui/cmrc2018) | 20k span-extraction QA | ZH |

Datasets are downloaded to `eval/datasets/<name>/` (gitignored).

## Architecture

### File Structure

```
eval/
  unified_eval.py          # Unified CLI entry point
  scoring.py               # F1 + LLM-as-Judge dual-track scoring
  metrics.py               # Latency stats (p50/p95/p99) + token reduction
  report.py                # Unified report generation (JSON + terminal table)
  oc_client.py             # OCClient HTTP client (extracted from locomo_eval.py)
  llm_client.py            # LLMClient (extracted from locomo_eval.py)
  adapters/
    base.py                # EvalAdapter ABC
    memory.py              # PersonaMem v2 adapter
    conversation.py        # LoCoMo / LongMemEval adapter
    document.py            # QASPER / LongBench / CMRC adapter
  datasets/                # Dataset storage (gitignored)
  reports/                 # Output reports
```

### Existing File Changes

- `eval/locomo_eval.py` — retained but marked deprecated; core logic migrated to `adapters/conversation.py`, `oc_client.py`, `llm_client.py`, and `scoring.py`
- `tests/benchmark/runner.py` — retained as-is (P0 retrieval-only benchmark, different purpose)
- `src/opencortex/eval/memory_eval.py` — retained as-is (used by `tests/benchmark/runner.py`). The new `eval/metrics.py` is independent and does NOT import from this module; the two serve different purposes (retrieval metrics vs QA+latency+token metrics).

### Unified Pipeline

```
Dataset → Adapter.ingest() → Adapter.build_qa_items() →
  ├─ OC path:  search/recall → build prompt → LLM answer → scoring
  ├─ Baseline: full context → build prompt → LLM answer → scoring
  └─ Latency:  each search/recall call timed
→ Report (accuracy + token_reduction + latency)
```

### EvalAdapter ABC

```python
@dataclass
class QAItem:
    question: str
    answer: str
    category: str = ""
    difficulty: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

@dataclass
class IngestResult:
    total_items: int
    ingested_items: int
    errors: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

class EvalAdapter(ABC):
    def __init__(self):
        self._dataset: Any = None  # Loaded dataset, set by load_dataset()

    def load_dataset(self, dataset_path: str, **kwargs) -> None:
        """Load and cache the dataset. Called once before ingest/build_qa_items.
        Subclasses store parsed data in self._dataset for use by all methods."""
        ...

    @abstractmethod
    async def ingest(self, oc: OCClient, **kwargs) -> IngestResult:
        """Ingest loaded dataset into OpenCortex using mode-appropriate API calls."""
        ...

    @abstractmethod
    def build_qa_items(self, **kwargs) -> List[QAItem]:
        """Return QA items from loaded dataset for evaluation."""
        ...

    @abstractmethod
    def get_baseline_context(self, qa_item: QAItem) -> str:
        """Return full context for baseline LLM evaluation (no retrieval).
        Uses self._dataset to look up source documents/conversations."""
        ...

    @abstractmethod
    async def retrieve(self, oc: OCClient, qa_item: QAItem, top_k: int) -> Tuple[List[Dict], float]:
        """Retrieve relevant memories/chunks. Returns (results, latency_ms)."""
        ...
```

The adapter holds the loaded dataset as instance state. `load_dataset()` is called once at startup; subsequent methods (`get_baseline_context`, etc.) access `self._dataset` to look up source documents, conversation histories, or persona facts as needed. This avoids stuffing full document text into every `QAItem.meta`.

## OCClient Interface (oc_client.py)

Extracted from `locomo_eval.py` with extended signature to support all three modes:

```python
class OCClient:
    def __init__(self, base: str, token: str, timeout: float = 120.0, retries: int = 3):
        ...

    async def store(
        self,
        abstract: str,
        content: str = "",
        category: str = "",
        context_type: str = "memory",
        meta: Optional[Dict[str, Any]] = None,
        dedup: bool = False,
    ) -> Dict:
        """Store a memory/document. Supports meta for ingest_mode override."""
        ...

    async def search(self, query: str, limit: int = 10, category: str = "",
                     detail_level: str = "l2") -> List[Dict]:
        ...

    async def context_recall(self, session_id: str, query: str,
                              turn_id: str = "t0", limit: int = 10) -> Dict:
        ...

    async def context_commit(self, session_id: str, turn_id: str,
                              messages: List[Dict[str, str]]) -> Dict:
        ...

    async def context_end(self, session_id: str) -> Dict:
        ...

    async def close(self):
        ...
```

Key changes from the original `locomo_eval.py` OCClient:
- `store()` gains `meta` and `context_type` parameters (were hardcoded before)
- All methods preserve the retry + error handling logic from the original

## LLMClient Interface (llm_client.py)

Extracted from `locomo_eval.py` preserving all existing logic:

```python
class LLMClient:
    def __init__(self, base: str, key: str, model: str,
                 timeout: float = 60.0, api_style: str = "auto",
                 no_thinking: bool = False):
        ...

    async def complete(self, prompt: str, max_tokens: int = 512, retries: int = 3) -> str:
        """OpenAI/Anthropic-compatible completion with retry and thinking-strip."""
        ...
```

Preserves: `_strip_thinking()` logic for reasoning models, `_resolve_api_style()` auto-detection, retry with exponential backoff on 429/5xx.

## Adapter Details

### Memory Adapter (PersonaMem v2)

**Dataset format** (HuggingFace `bowen-upenn/PersonaMem-v2`):
```json
{
  "user_profile": {"name": "...", "preferences": [...], "facts": [...]},
  "conversations": [
    {
      "session_id": "s1",
      "messages": [
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "..."}
      ]
    }
  ],
  "persona_attributes": [
    {"attribute": "prefers dark roast coffee", "source_session": "s1", "category": "preference"}
  ],
  "questions": [
    {"question": "What kind of coffee does the user prefer?", "answer": "dark roast", "category": "preference"}
  ]
}
```

**Ingest**: Extract `persona_attributes` from the dataset. Each attribute is stored via `oc.store(abstract=attribute["attribute"], category=attribute["category"], context_type="memory")`. If attributes are not pre-extracted, the adapter parses conversations and extracts factual statements about the user.

**QA**: The `questions` array provides QA pairs. Categories typically include: preference, biographical, relational, behavioral.

**Baseline context**: All persona attributes concatenated as a fact list.

**Retrieve**: `oc.search(question, limit=top_k)` — direct memory search, no session context.

### Conversation Adapter (LoCoMo / LongMemEval)

**Ingest**: Simulates real MCP conversation flow per session:
1. `context_recall()` at session start (prepare phase)
2. `context_commit()` per turn pair (commit phase, triggers immediate + merge layers)
3. `context_end()` to flush Observer/TraceSplitter

Reuses the proven ingest logic from `locomo_eval.py`.

**QA**: 5 categories for LoCoMo (single-hop, temporal, commonsense, multi-hop, adversarial). LongMemEval has 5 different categories (information extraction, multi-session reasoning, knowledge updates, temporal reasoning, abstention).

**Baseline context**: Full conversation text, truncated to 30k chars.

**Retrieve**: `oc.context_recall(session_id, question, top_k)` — session-aware retrieval via Context API prepare phase.

**LongMemEval ingestion flow**: LongMemEval uses user-assistant conversations (not user-user like LoCoMo). Each conversation has `sessions[]` containing `messages[]` with `role` ("user"/"assistant") fields. Ingestion follows the same 3-phase MCP flow but mapping is simpler: roles map directly to context_commit (no speaker→role translation needed). LongMemEval_S has ~40 sessions per conversation (~115k tokens total); LongMemEval_M scales to ~1.5M tokens. The adapter processes sessions sequentially like LoCoMo.

**Dataset detection**: Adapter detects dataset type (LoCoMo vs LongMemEval) from JSON structure and switches parsing logic accordingly. LoCoMo has `conversation.session_N` structure with `speaker` fields; LongMemEval has `sessions[].messages[]` with `role` fields.

### Document Adapter (QASPER / LongBench / CMRC)

**Ingest**: Each document is ingested via document mode:
```python
oc.store(
    content=doc["full_text"],
    abstract=doc["title"],
    meta={"ingest_mode": "document", "source_path": f"{doc_id}.md"},
)
```
This triggers `_add_document()` → MarkdownParser → multi-chunk hierarchy.

**QA**: QASPER has answer types (extractive, yes/no, free-form, unanswerable). LongBench has multiple-choice. CMRC has span-extraction. The adapter normalizes these to a common `QAItem` format.

**Baseline context**: Full document text for the source document of each QA.

**Retrieve**: `oc.search(question, limit=top_k)` — direct search over document chunks.

**Dataset detection**: Adapter detects dataset type from JSON structure or `--dataset` flag. QASPER has `full_text` + `qas` fields; LongBench has `input` + `answers` fields; CMRC has `context` + `answers` with `answer_start`.

## Scoring (scoring.py)

### F1 Token Overlap (default, always computed)

Migrated from `locomo_eval.py` `score_qa()`:

```python
def f1_score(prediction: str, ground_truth: str) -> float:
    """Normalized F1 token overlap."""
    # 1. Normalize: lowercase, remove articles, strip punctuation
    # 2. Tokenize by whitespace
    # 3. Compute common tokens via Counter intersection
    # 4. precision = common / pred_len, recall = common / gt_len
    # 5. F1 = 2 * precision * recall / (precision + recall)
```

Category-specific logic preserved:
- Category 1 (single-hop): multi-answer F1 via comma-separated alternatives
- Category 3 (commonsense): use first semicolon-separated alternative
- Category 5 (adversarial): check for refusal phrases

### LLM-as-Judge (optional, `--enable-llm-judge`)

```python
async def llm_judge_score(
    prediction: str,
    ground_truth: str,
    question: str,
    llm: LLMClient,
) -> float:
    """LLM semantic equivalence judgment. Returns 0.0 / 0.5 / 1.0."""
    prompt = (
        "You are an evaluation judge. Determine if the prediction correctly "
        "answers the question based on the ground truth.\n\n"
        f"Question: {question}\n"
        f"Ground truth: {ground_truth}\n"
        f"Prediction: {prediction}\n\n"
        "Score: 1.0 if correct, 0.5 if partially correct, 0.0 if wrong.\n"
        "Output only the number."
    )
    response = await llm.complete(prompt, max_tokens=8)
    return _parse_judge_score(response)  # parse float, default 0.0
```

Both scores are computed when `--enable-llm-judge` is set. F1 is always computed.

## Metrics (metrics.py)

### Token Reduction

```python
def compute_token_metrics(records: List[Dict]) -> Dict:
    oc_tokens = [r["oc_prompt_tokens"] for r in records]
    bl_tokens = [r["baseline_prompt_tokens"] for r in records]
    return {
        "baseline_avg_tokens": mean(bl_tokens),
        "oc_avg_tokens": mean(oc_tokens),
        "baseline_total_tokens": sum(bl_tokens),
        "oc_total_tokens": sum(oc_tokens),
        "reduction_pct": round((1 - sum(oc_tokens) / sum(bl_tokens)) * 100, 1),
    }
```

Token counting uses `estimate_tokens()` from `src/opencortex/parse/base.py` (CJK-aware: CJK chars × 0.7 + other chars × 0.3).

Each QA records two prompts:
- **OC prompt** = retrieved memories/chunks + question template
- **Baseline prompt** = full context (all memories / full conversation / full document) + question template

### Latency

```python
def compute_latency_metrics(latencies_ms: List[float]) -> Dict:
    sorted_lat = sorted(latencies_ms)
    return {
        "p50_ms": round(_percentile(sorted_lat, 50), 1),
        "p95_ms": round(_percentile(sorted_lat, 95), 1),
        "p99_ms": round(_percentile(sorted_lat, 99), 1),
        "mean_ms": round(mean(latencies_ms), 1),
        "min_ms": round(min(latencies_ms), 1),
        "max_ms": round(max(latencies_ms), 1),
        "count": len(latencies_ms),
    }
```

Latency is measured client-side around each `search()` or `context_recall()` call using `time.perf_counter()`. Only recall/search latency is tracked (excludes LLM inference time).

No external dependencies (numpy, etc.) — percentile computed with simple sorted-list indexing.

## Report (report.py)

### JSON Output

```json
{
  "mode": "conversation",
  "dataset": "locomo",
  "accuracy": {
    "f1": {
      "overall": 0.72,
      "by_category": {
        "single-hop": {"f1": 0.68, "n": 100},
        "temporal": {"f1": 0.42, "n": 80}
      }
    },
    "llm_judge": {
      "overall": 0.78,
      "by_category": {}
    },
    "baseline_f1": 0.68,
    "delta_f1": "+0.04"
  },
  "token_reduction": {
    "baseline_avg_tokens": 12500,
    "oc_avg_tokens": 1800,
    "reduction_pct": 85.6
  },
  "latency": {
    "p50_ms": 125,
    "p95_ms": 310,
    "p99_ms": 480,
    "mean_ms": 158,
    "count": 500
  },
  "metadata": {
    "timestamp": "2026-03-14T14:32:01Z",
    "run_id": "eval_abc123",
    "llm_model": "gpt-4o",
    "server": "http://127.0.0.1:8921",
    "dataset_path": "eval/datasets/locomo10.json",
    "top_k": 10,
    "concurrency": 5,
    "total_qa": 500,
    "enable_llm_judge": false
  },
  "per_query": [...]
}
```

### Terminal Table

```
========================================================
                    QA Accuracy (F1)
--------------------------------------------------------
Category           Baseline    OpenCortex     Delta
--------------------------------------------------------
single-hop           0.52         0.68       +0.16
temporal             0.31         0.42       +0.11
...
--------------------------------------------------------
Overall              0.48         0.59       +0.11
========================================================

--- Token Reduction ---
  Baseline avg:  12,500 tokens
  OpenCortex avg: 1,800 tokens
  Reduction:      85.6%

--- Recall Latency ---
  p50: 125ms   p95: 310ms   p99: 480ms
```

## CLI Interface (unified_eval.py)

### Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--mode` | memory / conversation / document / all | Required |
| `--dataset` | Dataset name (personamem, locomo, longmemeval, qasper, longbench, cmrc) | Auto by mode |
| `--data` | Dataset path override | — |
| `--server` | OpenCortex server URL | http://127.0.0.1:8921 |
| `--token` | JWT Bearer token | — |
| `--llm-base` | LLM API base URL | Required |
| `--llm-key` | LLM API key | Required |
| `--llm-model` | LLM model name | Required |
| `--llm-api-style` | auto / openai / anthropic | auto |
| `--top-k` | Retrieval limit | 10 |
| `--concurrency` | Concurrent QA evaluations | 5 |
| `--enable-llm-judge` | Enable LLM-as-Judge scoring | False |
| `--skip-ingest` | Skip ingestion (reuse existing data) | False |
| `--oc-only` | Skip baseline evaluation | False |
| `--baseline-only` | Skip OC evaluation | False |
| `--max-qa` | Limit QA count (for quick tests) | 0 (all) |
| `--max-conv` | Limit conversation count | 0 (all) |
| `--output` | Report output directory | eval/reports/ |
| `--run-id` | Reuse tenant from previous run (for --skip-ingest) | — |
| `--data-root` | Server data_root for JWT generation | ./data |
| `--no-thinking` | Disable LLM reasoning/thinking mode | False |
| `--seed` | Random seed | 42 |

### Usage Examples

```bash
# Conversation mode with LoCoMo
uv run python eval/unified_eval.py \
  --mode conversation --dataset locomo \
  --data eval/locomo10.json \
  --server http://127.0.0.1:8921 --token <jwt> \
  --llm-base https://api.example.com/v1 --llm-key <key> --llm-model gpt-4o

# Document mode with QASPER + LLM judge
uv run python eval/unified_eval.py \
  --mode document --dataset qasper \
  --enable-llm-judge \
  --llm-base ... --llm-key ... --llm-model ...

# All modes
uv run python eval/unified_eval.py --mode all ...

# Quick test (5 QA only)
uv run python eval/unified_eval.py --mode memory --max-qa 5 ...

# Reuse previous ingestion (skip-ingest with run-id)
uv run python eval/unified_eval.py --mode conversation --skip-ingest --run-id eval_conversation_a1b2c3d4 ...
```

## Data Isolation

Each eval run creates an isolated tenant via JWT to prevent cross-run pollution (same pattern as `tests/benchmark/runner.py`):

```python
run_id = f"eval_{mode}_{uuid4().hex[:8]}"  # e.g. "eval_conversation_a1b2c3d4"
jwt_token = generate_token(run_id, "eval_runner", ensure_secret(data_root))
```

This means:
- Each run writes to its own tenant namespace — no interference between runs.
- `--skip-ingest` reuses the **same run_id** from a previous run (passed via `--run-id` flag or read from the previous report's `metadata.run_id`). Without a matching run_id, skip-ingest searches an empty tenant.
- `--mode all` runs each mode sequentially with a **separate run_id per mode** (e.g. `eval_memory_xxx`, `eval_conversation_yyy`, `eval_document_zzz`). Each mode produces its own report file. A summary report aggregates all three.

The `--data-root` parameter specifies where to find `auth_secret.key` for JWT generation (same as `tests/benchmark/runner.py`).

## `--mode all` Behavior

When `--mode all` is specified:
1. Runs memory, conversation, document modes **sequentially** (not parallel).
2. Uses the **default dataset** for each mode: personamem for memory, locomo for conversation, qasper for document. The `--dataset` flag is ignored with `--mode all`.
3. Each mode gets its own tenant, report file, and terminal output.
4. After all three modes complete, a summary report (`eval/reports/all-<timestamp>.json`) is written with per-mode results side by side.

## Dependencies

No new Python dependencies. The framework uses only:
- `httpx` (already in project)
- `asyncio`, `json`, `time`, `argparse`, `re`, `string`, `random` (stdlib)
- `estimate_tokens()` from `src/opencortex/parse/base.py`

Users download datasets manually (see Scope Exclusions). The `estimate_tokens` function is imported directly from `src/opencortex/parse/base.py`; add `src/` to `sys.path` as done in `tests/benchmark/runner.py`.

## Testing Strategy

Unit tests for core modules:
- `tests/test_eval_scoring.py` — F1 scoring edge cases, LLM judge parsing
- `tests/test_eval_metrics.py` — percentile calculation, token reduction math

No E2E tests (requires external server + LLM API). Manual validation via `--max-qa 5`.

## Scope Exclusions

- No CI integration (external dependencies make this impractical)
- No automatic dataset download CLI (`--download` deferred to future). Users manually download datasets to `eval/datasets/<name>/` following instructions in each adapter's docstring.
- No server-side latency breakdown (embed/search/rerank phases) — client-side only
- `tests/benchmark/runner.py` remains unchanged (separate P0 retrieval benchmark)
- No cross-run comparison tooling (compare two reports manually via JSON)
