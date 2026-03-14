# Unified Evaluation Framework Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a unified evaluation framework for OpenCortex covering all three ingestion modes (memory, conversation, document) with four result dimensions: retrieval quality, QA accuracy, context token reduction, and recall latency.

**Architecture:** Extract OCClient and LLMClient from `benchmarks/locomo_eval.py`, create pure-function scoring and metrics modules (TDD), define an EvalAdapter ABC with three mode-specific implementations, and wire everything through a unified CLI entry point that produces structured JSON + terminal table reports.

**Tech Stack:** Python 3.10+, httpx (async HTTP client), asyncio, `estimate_tokens()` from `src/opencortex/parse/base.py`, ASGITransport for contract tests.

**Spec:** `docs/superpowers/specs/2026-03-14-unified-eval-framework-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|----------------|
| `benchmarks/scoring.py` | F1 token overlap + LLM-as-Judge scoring |
| `benchmarks/metrics.py` | Retrieval quality (recall@k, precision@k, MRR), latency (p50/p95/p99), token reduction |
| `benchmarks/oc_client.py` | OCClient HTTP client (extracted from `locomo_eval.py` with `meta`, `context_type` params) |
| `benchmarks/llm_client.py` | LLMClient (extracted from `locomo_eval.py`, preserves retry/thinking-strip) |
| `benchmarks/report.py` | JSON report builder + terminal table printer |
| `benchmarks/unified_eval.py` | CLI entry point, orchestration loop |
| `benchmarks/adapters/__init__.py` | Package init |
| `benchmarks/adapters/base.py` | EvalAdapter ABC, QAItem, IngestResult dataclasses |
| `benchmarks/adapters/memory.py` | PersonaMem v2 adapter |
| `benchmarks/adapters/conversation.py` | LoCoMo / LongMemEval adapter |
| `benchmarks/adapters/document.py` | QASPER / LongBench / CMRC adapter |
| `tests/test_eval_scoring.py` | F1 scoring unit tests |
| `tests/test_eval_metrics.py` | Metrics unit tests (retrieval, latency, token reduction) |
| `tests/test_eval_contract.py` | ASGITransport contract tests (OCClient ↔ server) |

### Modified Files

| File | Change |
|------|--------|
| `benchmarks/locomo_eval.py` | Add deprecation notice at top of docstring |

---

## Chunk 1: Scoring Module (TDD)

### Task 1: F1 Scoring + LLM Judge Parsing

**Files:**
- Create: `benchmarks/scoring.py`
- Test: `tests/test_eval_scoring.py`

- [ ] **Step 1: Write failing tests for F1 scoring**

Create `tests/test_eval_scoring.py` with test cases:

```python
"""Unit tests for benchmarks/scoring.py — F1 token overlap + LLM judge parsing."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.scoring import f1_score, score_qa, _normalize, _parse_judge_score


class TestNormalize(unittest.TestCase):
    def test_lowercase_and_strip_articles(self):
        self.assertEqual(_normalize("The quick Brown fox"), "quick brown fox")

    def test_remove_punctuation(self):
        self.assertEqual(_normalize("hello, world!"), "hello world")

    def test_comma_removal(self):
        self.assertEqual(_normalize("1,000 items"), "1000 items")

    def test_empty_string(self):
        self.assertEqual(_normalize(""), "")


class TestF1Score(unittest.TestCase):
    def test_exact_match(self):
        self.assertAlmostEqual(f1_score("dark roast coffee", "dark roast coffee"), 1.0)

    def test_partial_overlap(self):
        score = f1_score("dark roast", "dark roast coffee")
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1.0)

    def test_no_overlap(self):
        self.assertAlmostEqual(f1_score("completely different", "dark roast coffee"), 0.0)

    def test_empty_prediction(self):
        self.assertAlmostEqual(f1_score("", "dark roast coffee"), 0.0)

    def test_case_insensitive(self):
        self.assertAlmostEqual(f1_score("Dark Roast Coffee", "dark roast coffee"), 1.0)

    def test_article_only_inputs(self):
        """Both inputs normalize to empty after article removal → 0.0."""
        self.assertAlmostEqual(f1_score("the", "the"), 0.0)

    def test_both_empty(self):
        self.assertAlmostEqual(f1_score("", ""), 0.0)


class TestScoreQA(unittest.TestCase):
    def test_category_1_multi_answer(self):
        """Single-hop: comma-separated multi-answer F1."""
        score = score_qa("coffee, tea", "coffee, tea", category=1)
        self.assertAlmostEqual(score, 1.0)

    def test_category_3_semicolon_first(self):
        """Commonsense: use first semicolon alternative."""
        score = score_qa("happy", "happy; joyful; glad", category=3)
        self.assertAlmostEqual(score, 1.0)

    def test_category_5_adversarial_refusal(self):
        """Adversarial: refusal phrases → 1.0."""
        self.assertAlmostEqual(score_qa("No information available", "anything", category=5), 1.0)
        self.assertAlmostEqual(score_qa("Not mentioned in text", "anything", category=5), 1.0)

    def test_category_5_adversarial_no_refusal(self):
        """Adversarial: no refusal → 0.0."""
        self.assertAlmostEqual(score_qa("The answer is 42", "anything", category=5), 0.0)

    def test_category_2_temporal(self):
        """Temporal: standard F1."""
        score = score_qa("January 2024", "January 2024", category=2)
        self.assertAlmostEqual(score, 1.0)

    def test_category_4_multihop(self):
        """Multi-hop: standard F1."""
        score = score_qa("dark roast", "dark roast coffee", category=4)
        self.assertGreater(score, 0.5)


class TestParseJudgeScore(unittest.TestCase):
    def test_parse_1_0(self):
        self.assertAlmostEqual(_parse_judge_score("1.0"), 1.0)

    def test_parse_0_5(self):
        self.assertAlmostEqual(_parse_judge_score("0.5"), 0.5)

    def test_parse_0_0(self):
        self.assertAlmostEqual(_parse_judge_score("0.0"), 0.0)

    def test_parse_with_whitespace(self):
        self.assertAlmostEqual(_parse_judge_score("  1.0  \n"), 1.0)

    def test_parse_garbage_returns_0(self):
        self.assertAlmostEqual(_parse_judge_score("not a number"), 0.0)

    def test_parse_out_of_range_clamps(self):
        self.assertAlmostEqual(_parse_judge_score("2.5"), 0.0)
        self.assertAlmostEqual(_parse_judge_score("-1.0"), 0.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_eval_scoring -v`
Expected: ImportError — `eval.scoring` does not exist yet

- [ ] **Step 3: Implement scoring.py**

Create `benchmarks/scoring.py`:

```python
"""
Unified evaluation scoring: F1 token overlap + LLM-as-Judge.

Migrated from benchmarks/locomo_eval.py with generalized category handling.
"""

import re
import string
from collections import Counter
from typing import Any


def _normalize(s: str) -> str:
    """Normalize text for F1 comparison: lowercase, remove articles/punctuation."""
    s = str(s).replace(",", "")
    s = re.sub(r"\b(a|an|the|and)\b", " ", s.lower())
    s = "".join(ch for ch in s if ch not in string.punctuation)
    return " ".join(s.split())


def f1_score(prediction: str, ground_truth: str) -> float:
    """Normalized F1 token overlap between prediction and ground truth."""
    p_tok = _normalize(prediction).split()
    g_tok = _normalize(ground_truth).split()
    if not p_tok or not g_tok:
        return 0.0
    common = Counter(p_tok) & Counter(g_tok)
    n = sum(common.values())
    if n == 0:
        return 0.0
    prec = n / len(p_tok)
    rec = n / len(g_tok)
    return (2 * prec * rec) / (prec + rec)


def _f1_multi(pred: str, gt: str) -> float:
    """Multi-answer F1 for comma-separated alternatives."""
    preds = [p.strip() for p in pred.split(",")]
    gts = [g.strip() for g in str(gt).split(",")]
    if not gts:
        return 0.0
    return sum(max(f1_score(p, g) for p in preds) for g in gts) / len(gts)


def score_qa(prediction: str, answer: Any, category: int) -> float:
    """Return F1 score for a single QA pair with category-specific logic.

    Category logic (from LoCoMo):
        1 (single-hop): multi-answer F1 via comma-separated alternatives
        2 (temporal): standard F1
        3 (commonsense): use first semicolon-separated alternative
        4 (multi-hop): standard F1
        5 (adversarial): check for refusal phrases
    """
    pred = str(prediction).strip()
    ans = str(answer).strip()

    if category == 5:
        low = pred.lower()
        return 1.0 if ("no information" in low or "not mentioned" in low) else 0.0

    if category == 3:
        ans = ans.split(";")[0].strip()

    if category == 1:
        return _f1_multi(pred, ans)

    return f1_score(pred, ans)


def _parse_judge_score(response: str) -> float:
    """Parse LLM judge response to 0.0 / 0.5 / 1.0 score."""
    text = response.strip()
    try:
        value = float(text)
    except ValueError:
        return 0.0
    if value < 0.0 or value > 1.0:
        return 0.0
    return value


async def llm_judge_score(
    prediction: str,
    ground_truth: str,
    question: str,
    llm_complete_fn,
) -> float:
    """LLM semantic equivalence judgment. Returns 0.0 / 0.5 / 1.0.

    Args:
        llm_complete_fn: async callable(prompt, max_tokens) -> str
    """
    prompt = (
        "You are an evaluation judge. Determine if the prediction correctly "
        "answers the question based on the ground truth.\n\n"
        f"Question: {question}\n"
        f"Ground truth: {ground_truth}\n"
        f"Prediction: {prediction}\n\n"
        "Score: 1.0 if correct, 0.5 if partially correct, 0.0 if wrong.\n"
        "Output only the number."
    )
    response = await llm_complete_fn(prompt, 8)
    return _parse_judge_score(response)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_eval_scoring -v`
Expected: All 18 tests PASS

- [ ] **Step 5: Commit**

```bash
git add benchmarks/scoring.py tests/test_eval_scoring.py
git commit -m "feat(eval): add scoring module with F1 + LLM judge"
```

---

### Task 2: Metrics Module (Retrieval + Latency + Token Reduction)

**Files:**
- Create: `benchmarks/metrics.py`
- Test: `tests/test_eval_metrics.py`

- [ ] **Step 1: Write failing tests for metrics**

Create `tests/test_eval_metrics.py`:

```python
"""Unit tests for benchmarks/metrics.py — retrieval, latency, and token reduction metrics."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.metrics import (
    compute_retrieval_metrics,
    compute_latency_metrics,
    compute_token_metrics,
    truncate_to_budget,
    _percentile,
)


class TestPercentile(unittest.TestCase):
    def test_p50_odd(self):
        self.assertAlmostEqual(_percentile([1, 2, 3, 4, 5], 50), 3.0)

    def test_p95_small(self):
        data = list(range(1, 101))  # 1..100
        self.assertAlmostEqual(_percentile(data, 95), 95.05, places=1)

    def test_single_element(self):
        self.assertAlmostEqual(_percentile([42.0], 50), 42.0)

    def test_p99(self):
        data = list(range(1, 101))
        self.assertGreater(_percentile(data, 99), 98.0)


class TestRetrievalMetrics(unittest.TestCase):
    def test_perfect_recall(self):
        records = [
            {"retrieved_uris": ["a", "b", "c"], "expected_uris": ["a", "b"]},
        ]
        m = compute_retrieval_metrics(records, ks=[1, 3])
        self.assertAlmostEqual(m["recall@3"], 1.0)

    def test_zero_recall(self):
        records = [
            {"retrieved_uris": ["x", "y", "z"], "expected_uris": ["a", "b"]},
        ]
        m = compute_retrieval_metrics(records, ks=[1, 3])
        self.assertAlmostEqual(m["recall@1"], 0.0)
        self.assertAlmostEqual(m["mrr"], 0.0)

    def test_mrr_first_hit_at_rank_2(self):
        records = [
            {"retrieved_uris": ["x", "a", "b"], "expected_uris": ["a"]},
        ]
        m = compute_retrieval_metrics(records, ks=[3])
        self.assertAlmostEqual(m["mrr"], 0.5)

    def test_skip_empty_expected(self):
        records = [
            {"retrieved_uris": ["a"], "expected_uris": ["a"]},
            {"retrieved_uris": ["b"], "expected_uris": []},
        ]
        m = compute_retrieval_metrics(records, ks=[1])
        self.assertEqual(m["evaluated_count"], 1)
        self.assertEqual(m["skipped_no_ground_truth"], 1)

    def test_by_category(self):
        records = [
            {"retrieved_uris": ["a"], "expected_uris": ["a"], "category": "easy"},
            {"retrieved_uris": ["x"], "expected_uris": ["a"], "category": "hard"},
        ]
        m = compute_retrieval_metrics(records, ks=[1])
        self.assertAlmostEqual(m["by_category"]["easy"]["recall@1"], 1.0)
        self.assertAlmostEqual(m["by_category"]["hard"]["recall@1"], 0.0)


class TestTokenMetrics(unittest.TestCase):
    def test_reduction(self):
        records = [
            {"oc_prompt_tokens": 200, "baseline_prompt_tokens": 1000},
            {"oc_prompt_tokens": 300, "baseline_prompt_tokens": 1000},
        ]
        m = compute_token_metrics(records)
        self.assertAlmostEqual(m["reduction_pct"], 75.0)
        self.assertEqual(m["oc_total_tokens"], 500)
        self.assertEqual(m["baseline_total_tokens"], 2000)

    def test_no_reduction(self):
        records = [
            {"oc_prompt_tokens": 1000, "baseline_prompt_tokens": 1000},
        ]
        m = compute_token_metrics(records)
        self.assertAlmostEqual(m["reduction_pct"], 0.0)

    def test_empty(self):
        m = compute_token_metrics([])
        self.assertAlmostEqual(m["reduction_pct"], 0.0)


class TestLatencyMetrics(unittest.TestCase):
    def test_basic(self):
        lats = [100.0, 200.0, 300.0, 400.0, 500.0]
        m = compute_latency_metrics(lats)
        self.assertAlmostEqual(m["p50_ms"], 300.0)
        self.assertEqual(m["count"], 5)
        self.assertAlmostEqual(m["mean_ms"], 300.0)

    def test_single(self):
        m = compute_latency_metrics([42.0])
        self.assertAlmostEqual(m["p50_ms"], 42.0)
        self.assertAlmostEqual(m["p99_ms"], 42.0)


class TestTruncateToBudget(unittest.TestCase):
    def test_short_text_unchanged(self):
        text = "hello world"
        self.assertEqual(truncate_to_budget(text, 1000), text)

    def test_long_text_truncated(self):
        text = "a" * 10000  # ~3000 tokens (0.3 per char)
        result = truncate_to_budget(text, 100)
        self.assertLess(len(result), len(text))

    def test_empty_text(self):
        self.assertEqual(truncate_to_budget("", 1000), "")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_eval_metrics -v`
Expected: ImportError — `eval.metrics` does not exist yet

- [ ] **Step 3: Implement metrics.py**

Create `benchmarks/metrics.py`:

```python
"""
Unified evaluation metrics: retrieval quality, latency, and token reduction.

Retrieval metrics migrated from src/opencortex/eval/memory_eval.py.
No external dependencies (no numpy).
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

# Import estimate_tokens from the OpenCortex parser module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from opencortex.parse.base import estimate_tokens


def _percentile(sorted_data: List[float], pct: float) -> float:
    """Compute percentile from pre-sorted list (no numpy)."""
    if not sorted_data:
        return 0.0
    n = len(sorted_data)
    if n == 1:
        return sorted_data[0]
    idx = (pct / 100.0) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_data[lo] + frac * (sorted_data[hi] - sorted_data[lo])


def _normalize_uri(uri: str) -> str:
    return uri.strip().rstrip("/")


def _single_retrieval_metrics(
    retrieved: List[str], expected: List[str], ks: List[int]
) -> Dict[str, float]:
    """Compute retrieval metrics for a single query."""
    expected_set = {_normalize_uri(u) for u in expected if u.strip()}
    retrieved_norm = [_normalize_uri(u) for u in retrieved if u.strip()]

    out: Dict[str, float] = {}
    for k in ks:
        topk = retrieved_norm[:k]
        hit_count = len(expected_set.intersection(topk))
        out[f"recall@{k}"] = hit_count / len(expected_set) if expected_set else 0.0
        out[f"precision@{k}"] = hit_count / k
        out[f"hit_rate@{k}"] = 1.0 if hit_count > 0 else 0.0

    rank = None
    for idx, uri in enumerate(retrieved_norm, start=1):
        if uri in expected_set:
            rank = idx
            break
    out["mrr"] = 1.0 / rank if rank else 0.0
    return out


def compute_retrieval_metrics(
    records: List[Dict[str, Any]], ks: List[int] = [1, 3, 5]
) -> Dict[str, Any]:
    """Recall@k, Precision@k, MRR, Hit Rate@k over all records with ground truth URIs.

    Each record must have:
        - retrieved_uris: List[str]
        - expected_uris: List[str]
        - category (optional): str for per-category breakdown
    """
    scored: List[Dict[str, float]] = []
    skipped = 0

    by_category: Dict[str, List[Dict[str, float]]] = {}

    for record in records:
        expected = record.get("expected_uris", [])
        if not expected:
            skipped += 1
            continue
        retrieved = record.get("retrieved_uris", [])
        metrics = _single_retrieval_metrics(retrieved, expected, ks)
        scored.append(metrics)

        cat = str(record.get("category", "unknown")).strip() or "unknown"
        by_category.setdefault(cat, []).append(metrics)

    # Aggregate overall
    result: Dict[str, Any] = {}
    if scored:
        metric_keys = list(scored[0].keys())
        for key in metric_keys:
            result[key] = round(sum(m[key] for m in scored) / len(scored), 4)
    else:
        for k in ks:
            result[f"recall@{k}"] = 0.0
            result[f"precision@{k}"] = 0.0
            result[f"hit_rate@{k}"] = 0.0
        result["mrr"] = 0.0

    result["evaluated_count"] = len(scored)
    result["skipped_no_ground_truth"] = skipped

    # Per-category aggregation
    cat_result: Dict[str, Dict[str, float]] = {}
    for cat, cat_scored in by_category.items():
        cat_agg: Dict[str, float] = {}
        if cat_scored:
            for key in cat_scored[0]:
                cat_agg[key] = round(sum(m[key] for m in cat_scored) / len(cat_scored), 4)
        cat_agg["count"] = float(len(cat_scored))
        cat_result[cat] = cat_agg
    result["by_category"] = cat_result

    return result


def compute_token_metrics(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute token reduction metrics.

    Each record must have:
        - oc_prompt_tokens: int
        - baseline_prompt_tokens: int
    """
    if not records:
        return {
            "baseline_avg_tokens": 0,
            "oc_avg_tokens": 0,
            "baseline_total_tokens": 0,
            "oc_total_tokens": 0,
            "reduction_pct": 0.0,
        }

    oc_tokens = [r["oc_prompt_tokens"] for r in records]
    bl_tokens = [r["baseline_prompt_tokens"] for r in records]
    total_oc = sum(oc_tokens)
    total_bl = sum(bl_tokens)

    return {
        "baseline_avg_tokens": round(total_bl / len(bl_tokens)),
        "oc_avg_tokens": round(total_oc / len(oc_tokens)),
        "baseline_total_tokens": total_bl,
        "oc_total_tokens": total_oc,
        "reduction_pct": round((1 - total_oc / total_bl) * 100, 1) if total_bl > 0 else 0.0,
    }


def compute_latency_metrics(latencies_ms: List[float]) -> Dict[str, Any]:
    """Compute latency percentile metrics from raw latency measurements.

    No external dependencies (no numpy). Percentile via sorted-list indexing.
    """
    if not latencies_ms:
        return {
            "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0,
            "mean_ms": 0.0, "min_ms": 0.0, "max_ms": 0.0, "count": 0,
        }

    sorted_lat = sorted(latencies_ms)
    return {
        "p50_ms": round(_percentile(sorted_lat, 50), 1),
        "p95_ms": round(_percentile(sorted_lat, 95), 1),
        "p99_ms": round(_percentile(sorted_lat, 99), 1),
        "mean_ms": round(sum(latencies_ms) / len(latencies_ms), 1),
        "min_ms": round(min(latencies_ms), 1),
        "max_ms": round(max(latencies_ms), 1),
        "count": len(latencies_ms),
    }


def truncate_to_budget(text: str, max_tokens: int) -> str:
    """Truncate text to fit within token budget using estimate_tokens().

    Truncates at character boundaries, keeping the beginning of the text.
    Uses binary search for efficient cutoff finding.
    """
    if not text or estimate_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_eval_metrics -v`
Expected: All 14 tests PASS

- [ ] **Step 5: Commit**

```bash
git add benchmarks/metrics.py tests/test_eval_metrics.py
git commit -m "feat(eval): add metrics module with retrieval, latency, token reduction"
```

---

## Chunk 2: Client Infrastructure

### Task 3: OCClient Extraction

**Files:**
- Create: `benchmarks/oc_client.py`
- Reference: `benchmarks/locomo_eval.py:330-473` (source for extraction)

- [ ] **Step 1: Create oc_client.py by extracting from locomo_eval.py**

Extract `OCClient` from `benchmarks/locomo_eval.py` (lines 330-473). Add `meta` and `context_type` parameters to `store()` and `search()` per spec:

```python
"""
OpenCortex evaluation HTTP client.

Extracted from benchmarks/locomo_eval.py with extended parameters:
- store() gains meta and context_type parameters
- search() gains context_type parameter

All retry + error handling logic preserved from the original.
"""

import asyncio
from typing import Any, Dict, List, Optional

import httpx


def _is_retryable_http_error(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return False


class OCClient:
    def __init__(
        self,
        base: str,
        token: str,
        timeout: float = 120.0,
        retries: int = 3,
        retry_delay: float = 2.0,
    ):
        self._base = base.rstrip("/")
        self._hdrs = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(timeout=timeout)
        self._retries = retries
        self._retry_delay = retry_delay

    async def close(self):
        await self._client.aclose()

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
        payload: Dict[str, Any] = {
            "abstract": abstract,
            "content": content,
            "category": category,
            "context_type": context_type,
            "dedup": dedup,
        }
        if meta:
            payload["meta"] = meta
        return await self._post("/api/v1/memory/store", payload)

    async def search(
        self,
        query: str,
        limit: int = 10,
        category: str = "",
        detail_level: str = "l2",
        context_type: Optional[str] = None,
    ) -> List[Dict]:
        """Search memories. context_type filters results (e.g. 'resource' for documents)."""
        payload: Dict[str, Any] = {
            "query": query,
            "limit": limit,
            "detail_level": detail_level,
        }
        if category:
            payload["category"] = category
        if context_type:
            payload["context_type"] = context_type
        result = await self._post("/api/v1/memory/search", payload)
        return result.get("results", [])

    async def context_recall(
        self,
        session_id: str,
        query: str,
        turn_id: str = "t0",
        limit: int = 10,
    ) -> Dict:
        """MCP recall: context phase=prepare with messages containing the query."""
        return await self._post("/api/v1/context", {
            "session_id": session_id,
            "phase": "prepare",
            "turn_id": turn_id,
            "messages": [{"role": "user", "content": query}],
            "config": {"max_items": limit, "detail_level": "l2"},
        })

    async def context_commit(
        self,
        session_id: str,
        turn_id: str,
        messages: List[Dict[str, str]],
    ) -> Dict:
        """MCP commit: write messages via conversation mode (immediate + merge)."""
        return await self._post("/api/v1/context", {
            "session_id": session_id,
            "phase": "commit",
            "turn_id": turn_id,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        })

    async def context_end(self, session_id: str) -> Dict:
        """MCP end: flush session → Alpha pipeline."""
        return await self._post("/api/v1/context", {
            "session_id": session_id,
            "phase": "end",
        })

    async def _post(self, path: str, payload: Dict) -> Dict:
        """POST with retry logic (retryable on 429/5xx and transport errors)."""
        url = f"{self._base}{path}"
        last_error: Optional[Exception] = None
        for attempt in range(1, self._retries + 1):
            try:
                r = await self._client.post(url, json=payload, headers=self._hdrs)
                r.raise_for_status()
                return r.json()
            except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= self._retries or not _is_retryable_http_error(exc):
                    raise
                await asyncio.sleep(self._retry_delay * attempt)
        if last_error:
            raise last_error
        return {}
```

- [ ] **Step 2: Verify import works**

Run: `uv run python -c "from benchmarks.oc_client import OCClient; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add benchmarks/oc_client.py
git commit -m "feat(eval): extract OCClient with meta + context_type support"
```

---

### Task 4: LLMClient Extraction

**Files:**
- Create: `benchmarks/llm_client.py`
- Reference: `benchmarks/locomo_eval.py:220-323` (source for extraction)

- [ ] **Step 1: Create llm_client.py by extracting from locomo_eval.py**

Extract `LLMClient` from `benchmarks/locomo_eval.py` (lines 220-323), preserving all retry, thinking-strip, and API style logic:

```python
"""
LLM client for evaluation (OpenAI/Anthropic-compatible).

Extracted from benchmarks/locomo_eval.py preserving all existing logic:
- _strip_thinking() for reasoning models
- _resolve_api_style() auto-detection
- Retry with exponential backoff on 429/5xx
"""

import asyncio
import re
from typing import Any, Dict
from urllib.parse import urlparse

import httpx


class LLMClient:
    def __init__(
        self,
        base: str,
        key: str,
        model: str,
        timeout: float = 60.0,
        api_style: str = "auto",
        no_thinking: bool = False,
    ):
        self._base = base.rstrip("/")
        self._key = key
        self._model = model
        self._api_style = self._resolve_api_style(api_style)
        self._no_thinking = no_thinking
        self._client = httpx.AsyncClient(timeout=timeout)

    async def complete(self, prompt: str, max_tokens: int = 512, retries: int = 3) -> str:
        """Send completion request with retry on transient errors."""
        url = self._build_request_url()
        payload = self._build_payload(prompt, max_tokens)
        for attempt in range(1, retries + 1):
            try:
                resp = await self._client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._key}"},
                )
                resp.raise_for_status()
                data = resp.json()
                return self._extract_text(data)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt >= retries:
                    raise
                await asyncio.sleep(2 * attempt)
            except httpx.HTTPStatusError as e:
                if attempt >= retries or e.response.status_code not in (429, 500, 502, 503):
                    raise
                await asyncio.sleep(3 * attempt)
        return ""

    async def close(self):
        await self._client.aclose()

    def _resolve_api_style(self, api_style: str) -> str:
        if api_style in {"openai", "anthropic"}:
            return api_style
        host = urlparse(self._base).netloc.lower()
        if "anthropic" in host:
            return "anthropic"
        return "openai"

    def _build_request_url(self) -> str:
        if self._api_style == "anthropic":
            return f"{self._base}/messages"
        return f"{self._base}/chat/completions"

    def _build_payload(self, prompt: str, max_tokens: int) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        if self._no_thinking:
            payload["thinking"] = {"type": "disabled"}
            payload["temperature"] = 0.7
        return payload

    @staticmethod
    def _strip_thinking(text: str) -> str:
        """Remove <think>...</think> reasoning blocks, return only the final answer."""
        stripped = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
        if stripped.strip():
            return stripped.strip()
        if "<think>" in text:
            parts = text.split("</think>")
            if len(parts) > 1:
                return parts[-1].strip()
        return text.strip()

    def _extract_text(self, data: Dict[str, Any]) -> str:
        if self._api_style == "anthropic":
            content = data.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return self._strip_thinking(str(block.get("text", "")))
                if content and isinstance(content[0], dict):
                    return self._strip_thinking(str(content[0].get("text", "")))
            raise KeyError("Anthropic response missing content text")

        choices = data.get("choices", [])
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message", {})
            if isinstance(message, dict):
                content = message.get("content", "")
                if not content:
                    content = message.get("reasoning_content", "")
                return self._strip_thinking(str(content))
        raise KeyError("OpenAI-compatible response missing choices[0].message.content")
```

- [ ] **Step 2: Verify import works**

Run: `uv run python -c "from benchmarks.llm_client import LLMClient; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add benchmarks/llm_client.py
git commit -m "feat(eval): extract LLMClient with thinking-strip + retry"
```

---

## Chunk 3: Adapter Framework + Memory Adapter

### Task 5: EvalAdapter ABC + Dataclasses

**Files:**
- Create: `benchmarks/adapters/__init__.py`
- Create: `benchmarks/adapters/base.py`

- [ ] **Step 1: Create adapters package init**

Create `benchmarks/adapters/__init__.py`:

```python
"""Evaluation adapters for different ingestion modes."""
```

- [ ] **Step 2: Create base.py with EvalAdapter ABC, QAItem, IngestResult**

Create `benchmarks/adapters/base.py`:

```python
"""
EvalAdapter abstract base class and common dataclasses.

Each adapter handles one ingestion mode (memory/conversation/document)
and provides methods for dataset loading, ingestion, QA extraction,
baseline context, and retrieval.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

# Forward reference — the OCClient type is imported by concrete adapters
# to avoid circular imports, we just type-hint as Any here
# Concrete adapters import OCClient directly


@dataclass
class QAItem:
    """A single QA evaluation item."""

    question: str
    answer: str
    category: str = ""
    difficulty: str = ""
    expected_ids: List[str] = field(default_factory=list)
    expected_uris: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestResult:
    """Result of ingesting a dataset into OpenCortex."""

    total_items: int
    ingested_items: int
    errors: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


class EvalAdapter(ABC):
    """Abstract base class for mode-specific evaluation adapters.

    Lifecycle:
        1. load_dataset(path) — load and cache dataset
        2. ingest(oc) — write data to OpenCortex
        3. build_qa_items() — extract QA pairs for evaluation
        4. For each QA item:
           a. retrieve(oc, item, top_k) — search OpenCortex
           b. get_baseline_context(item) — get full context for baseline
    """

    def __init__(self):
        self._dataset: Any = None

    def load_dataset(self, dataset_path: str, **kwargs) -> None:
        """Load and cache the dataset. Called once before ingest/build_qa_items.

        Subclasses store parsed data in self._dataset for use by all methods.
        """
        ...

    @abstractmethod
    async def ingest(self, oc: Any, **kwargs) -> IngestResult:
        """Ingest loaded dataset into OpenCortex using mode-appropriate API calls."""
        ...

    @abstractmethod
    def build_qa_items(self, **kwargs) -> List[QAItem]:
        """Return QA items from loaded dataset for evaluation."""
        ...

    @abstractmethod
    def get_baseline_context(self, qa_item: QAItem) -> str:
        """Return full context for baseline LLM evaluation (no retrieval).

        Uses self._dataset to look up source documents/conversations.
        """
        ...

    @abstractmethod
    async def retrieve(self, oc: Any, qa_item: QAItem, top_k: int) -> Tuple[List[Dict], float]:
        """Retrieve relevant memories/chunks. Returns (results, latency_ms).

        Each result dict must contain 'uri' for retrieval quality measurement.
        """
        ...
```

- [ ] **Step 3: Verify imports work**

Run: `uv run python -c "from benchmarks.adapters.base import EvalAdapter, QAItem, IngestResult; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add benchmarks/adapters/__init__.py benchmarks/adapters/base.py
git commit -m "feat(eval): add EvalAdapter ABC + QAItem/IngestResult dataclasses"
```

---

### Task 6: Memory Adapter (PersonaMem v2)

**Files:**
- Create: `benchmarks/adapters/memory.py`

- [ ] **Step 1: Implement memory adapter**

Create `benchmarks/adapters/memory.py`:

```python
"""
PersonaMem v2 adapter for memory-mode evaluation.

Dataset: https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2

Ingest: Stores pre-extracted persona_attributes via oc.store().
QA: Uses dataset's questions array.
Baseline: All persona attributes concatenated as a fact list.
Retrieve: Direct oc.search() (no session context).
"""

import json
import time
from typing import Any, Dict, List, Tuple

from benchmarks.adapters.base import EvalAdapter, IngestResult, QAItem


class MemoryAdapter(EvalAdapter):
    """PersonaMem v2 evaluation adapter."""

    def load_dataset(self, dataset_path: str, **kwargs) -> None:
        with open(dataset_path, encoding="utf-8") as f:
            self._dataset = json.load(f)

        # Validate required fields
        if not self._dataset.get("persona_attributes"):
            raise ValueError(
                "PersonaMem v2 dataset must contain non-empty 'persona_attributes'. "
                "The memory adapter requires pre-extracted structured attributes — "
                "no runtime extraction is performed."
            )

    async def ingest(self, oc: Any, **kwargs) -> IngestResult:
        """Store each persona attribute as a memory via oc.store()."""
        attributes = self._dataset["persona_attributes"]
        errors: List[str] = []
        id_to_uri: Dict[str, str] = {}

        for i, attr in enumerate(attributes):
            attr_text = attr.get("attribute", "")
            category = attr.get("category", "")
            attr_id = attr.get("id", str(i))
            try:
                result = await oc.store(
                    abstract=attr_text,
                    content=attr_text,
                    category=category,
                    context_type="memory",
                )
                uri = result.get("uri", "")
                if uri:
                    id_to_uri[attr_id] = uri
            except Exception as e:
                errors.append(f"attribute {attr_id}: {e}")

        # Store id→uri mapping for QA item URI resolution
        self._id_to_uri = id_to_uri

        return IngestResult(
            total_items=len(attributes),
            ingested_items=len(id_to_uri),
            errors=errors,
            meta={"id_to_uri": id_to_uri},
        )

    def build_qa_items(self, **kwargs) -> List[QAItem]:
        """Build QA items from dataset questions array."""
        questions = self._dataset.get("questions", [])
        max_qa = kwargs.get("max_qa", 0)
        if max_qa > 0:
            questions = questions[:max_qa]

        items: List[QAItem] = []
        id_to_uri = getattr(self, "_id_to_uri", {})

        for q in questions:
            expected_ids = q.get("expected_ids", [])
            expected_uris = [id_to_uri[eid] for eid in expected_ids if eid in id_to_uri]

            items.append(QAItem(
                question=q["question"],
                answer=str(q.get("answer", "")),
                category=q.get("category", ""),
                difficulty=q.get("difficulty", ""),
                expected_ids=expected_ids,
                expected_uris=expected_uris,
                meta=q.get("meta", {}),
            ))
        return items

    def get_baseline_context(self, qa_item: QAItem) -> str:
        """All persona attributes concatenated as a fact list."""
        attributes = self._dataset.get("persona_attributes", [])
        lines = [f"- {attr['attribute']}" for attr in attributes if attr.get("attribute")]
        return "Known facts about the user:\n" + "\n".join(lines)

    async def retrieve(self, oc: Any, qa_item: QAItem, top_k: int) -> Tuple[List[Dict], float]:
        """Direct memory search (no session context)."""
        t0 = time.perf_counter()
        results = await oc.search(query=qa_item.question, limit=top_k)
        latency_ms = (time.perf_counter() - t0) * 1000
        return results, latency_ms
```

- [ ] **Step 2: Verify import works**

Run: `uv run python -c "from benchmarks.adapters.memory import MemoryAdapter; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add benchmarks/adapters/memory.py
git commit -m "feat(eval): add PersonaMem v2 memory adapter"
```

---

## Chunk 4: Conversation + Document Adapters

### Task 7: Conversation Adapter (LoCoMo / LongMemEval)

**Files:**
- Create: `benchmarks/adapters/conversation.py`
- Reference: `benchmarks/locomo_eval.py:132-166` (session parsing), `benchmarks/locomo_eval.py:480-558` (ingest flow)

- [ ] **Step 1: Implement conversation adapter**

Create `benchmarks/adapters/conversation.py`:

```python
"""
Conversation adapter for LoCoMo and LongMemEval datasets.

Ingest: Simulates real MCP conversation flow per session:
  1. context_recall() at session start
  2. context_commit() per turn pair
  3. context_end() to flush Observer/TraceSplitter

Dataset detection: auto-detects LoCoMo vs LongMemEval from JSON structure.
  - LoCoMo: conversation.session_N structure with speaker fields
  - LongMemEval: sessions[].messages[] with role fields
"""

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from benchmarks.adapters.base import EvalAdapter, IngestResult, QAItem


def _parse_locomo_sessions(conv: Dict) -> List[Dict]:
    """Parse LoCoMo sessions sorted chronologically."""
    raw = conv["conversation"]
    nums = sorted(
        int(k.split("_")[1])
        for k in raw
        if k.startswith("session_") and "date_time" not in k
    )
    sessions = []
    for n in nums:
        key = f"session_{n}"
        if key not in raw:
            continue
        sessions.append({
            "session_num": n,
            "date_time": raw.get(f"session_{n}_date_time", ""),
            "turns": raw[key],
        })
    return sessions


def _fmt_locomo_session(session: Dict) -> str:
    lines = [f"[{session['date_time']}]"]
    for t in session["turns"]:
        text = t["text"]
        if "blip_caption" in t:
            text += f" [image: {t['blip_caption']}]"
        lines.append(f"{t['speaker']}: {text}")
    return "\n".join(lines)


def _get_locomo_speakers(sessions: List[Dict]) -> List[str]:
    seen: Dict[str, int] = {}
    for s in sessions:
        for t in s["turns"]:
            seen[t["speaker"]] = seen.get(t["speaker"], 0) + 1
    return sorted(seen, key=lambda x: -seen[x])


def _get_qa_answer(qa: Dict) -> str:
    if "answer" in qa:
        return str(qa["answer"])
    if "adversarial_answer" in qa:
        return str(qa["adversarial_answer"])
    return ""


class ConversationAdapter(EvalAdapter):
    """LoCoMo / LongMemEval evaluation adapter."""

    def load_dataset(self, dataset_path: str, **kwargs) -> None:
        with open(dataset_path, encoding="utf-8") as f:
            raw = json.load(f)

        if isinstance(raw, list):
            self._dataset = raw
        elif isinstance(raw, dict):
            self._dataset = [raw]
        else:
            raise ValueError(f"Unexpected dataset format: {type(raw)}")

        # Detect dataset type from first entry
        first = self._dataset[0]
        if "conversation" in first and any(
            k.startswith("session_") for k in first.get("conversation", {})
        ):
            self._dataset_type = "locomo"
        elif "sessions" in first:
            self._dataset_type = "longmemeval"
        else:
            raise ValueError(
                "Cannot detect dataset type. Expected LoCoMo (conversation.session_N) "
                "or LongMemEval (sessions[].messages[])."
            )

    async def ingest(self, oc: Any, **kwargs) -> IngestResult:
        """Ingest conversations via MCP conversation flow."""
        max_conv = kwargs.get("max_conv", 0)
        conversations = self._dataset
        if max_conv > 0:
            conversations = conversations[:max_conv]

        total = 0
        ingested = 0
        errors: List[str] = []

        for conv in conversations:
            conv_id = conv.get("sample_id", str(conversations.index(conv)))
            if self._dataset_type == "locomo":
                sessions = _parse_locomo_sessions(conv)
            else:
                sessions = self._parse_longmemeval_sessions(conv)

            total += len(sessions)

            for i, session in enumerate(sessions):
                session_id = f"eval-{conv_id}-s{session['session_num']}"
                try:
                    await self._ingest_session(oc, session, session_id, conv_id)
                    ingested += 1
                except Exception as e:
                    errors.append(f"conv={conv_id} session={session['session_num']}: {e}")

        return IngestResult(total_items=total, ingested_items=ingested, errors=errors)

    async def _ingest_session(
        self, oc: Any, session: Dict, session_id: str, conv_id: str
    ) -> None:
        """Ingest a single session via 3-phase MCP flow."""
        turns = session.get("turns", [])
        date = session.get("date_time", "")

        # 1. Prepare phase (recall at session start)
        first_text = ""
        if turns:
            first_text = (turns[0].get("text", "") or turns[0].get("content", ""))[:120]
        try:
            await oc.context_recall(session_id, f"{date} {first_text}", turn_id="t0", limit=3)
        except Exception:
            pass  # non-fatal on first session

        # 2. Build and commit message pairs
        if self._dataset_type == "locomo":
            msg_list = self._build_locomo_messages(turns, date)
        else:
            msg_list = self._build_longmemeval_messages(turns)

        turn_idx = 0
        for j in range(0, len(msg_list) - 1, 2):
            pair = msg_list[j:j + 2]
            roles = {m["role"] for m in pair}
            if "user" not in roles or "assistant" not in roles:
                continue
            turn_idx += 1
            await oc.context_commit(
                session_id=session_id,
                turn_id=f"t{turn_idx}",
                messages=pair,
            )

        # 3. End phase
        await oc.context_end(session_id)

    def _build_locomo_messages(self, turns: List[Dict], date: str) -> List[Dict[str, str]]:
        """Build message list from LoCoMo turns (speaker-based role mapping)."""
        msg_list: List[Dict[str, str]] = []
        first_speaker = turns[0]["speaker"] if turns else ""
        for t in turns:
            role = "user" if t["speaker"] == first_speaker else "assistant"
            text = t["text"]
            if "blip_caption" in t:
                text += f" [image: {t['blip_caption']}]"
            msg_list.append({"role": role, "content": f"[{date}] {t['speaker']}: {text}"})
        return msg_list

    def _build_longmemeval_messages(self, turns: List[Dict]) -> List[Dict[str, str]]:
        """Build message list from LongMemEval turns (role field maps directly)."""
        return [
            {"role": t["role"], "content": t["content"]}
            for t in turns
            if t.get("role") and t.get("content")
        ]

    def _parse_longmemeval_sessions(self, conv: Dict) -> List[Dict]:
        """Parse LongMemEval sessions structure."""
        sessions = []
        for i, sess in enumerate(conv.get("sessions", [])):
            sessions.append({
                "session_num": i + 1,
                "date_time": sess.get("date", ""),
                "turns": sess.get("messages", []),
            })
        return sessions

    def build_qa_items(self, **kwargs) -> List[QAItem]:
        """Build QA items from dataset."""
        max_qa = kwargs.get("max_qa", 0)
        items: List[QAItem] = []

        for conv in self._dataset:
            if self._dataset_type == "locomo":
                qa_list = conv.get("qa", [])
                for q in qa_list:
                    items.append(QAItem(
                        question=q["question"],
                        answer=_get_qa_answer(q),
                        category=str(q.get("category", "")),
                        difficulty=q.get("difficulty", ""),
                        meta={"conv_id": conv.get("sample_id", ""), "dataset": "locomo"},
                    ))
            else:
                # LongMemEval: questions at top level
                qa_list = conv.get("questions", [])
                for q in qa_list:
                    items.append(QAItem(
                        question=q["question"],
                        answer=str(q.get("answer", "")),
                        category=q.get("category", ""),
                        difficulty=q.get("difficulty", ""),
                        meta={"conv_id": conv.get("id", ""), "dataset": "longmemeval"},
                    ))

        if max_qa > 0:
            items = items[:max_qa]
        return items

    def get_baseline_context(self, qa_item: QAItem) -> str:
        """Full conversation text for baseline evaluation."""
        conv_id = qa_item.meta.get("conv_id", "")
        for conv in self._dataset:
            cid = conv.get("sample_id", conv.get("id", ""))
            if str(cid) != str(conv_id) and conv_id:
                continue

            if self._dataset_type == "locomo":
                sessions = _parse_locomo_sessions(conv)
                speakers = _get_locomo_speakers(sessions)
                header = f"Conversation between {' and '.join(speakers)} over multiple sessions.\n\n"
                return header + "\n\n".join(_fmt_locomo_session(s) for s in sessions)
            else:
                parts = []
                for sess in conv.get("sessions", []):
                    for msg in sess.get("messages", []):
                        parts.append(f"{msg['role']}: {msg['content']}")
                return "\n".join(parts)

        return ""

    async def retrieve(self, oc: Any, qa_item: QAItem, top_k: int) -> Tuple[List[Dict], float]:
        """Session-aware retrieval via Context API prepare phase."""
        conv_id = qa_item.meta.get("conv_id", "eval")
        session_id = f"eval-{conv_id}-eval"
        t0 = time.perf_counter()
        try:
            result = await oc.context_recall(session_id, qa_item.question, limit=top_k)
            memories = result.get("memory", [])
        except Exception:
            # Fallback to direct search
            memories = await oc.search(query=qa_item.question, limit=top_k)
        latency_ms = (time.perf_counter() - t0) * 1000
        return memories, latency_ms
```

- [ ] **Step 2: Verify import works**

Run: `uv run python -c "from benchmarks.adapters.conversation import ConversationAdapter; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add benchmarks/adapters/conversation.py
git commit -m "feat(eval): add conversation adapter (LoCoMo + LongMemEval)"
```

---

### Task 8: Document Adapter (QASPER / LongBench / CMRC)

**Files:**
- Create: `benchmarks/adapters/document.py`

- [ ] **Step 1: Implement document adapter**

Create `benchmarks/adapters/document.py`:

```python
"""
Document adapter for QASPER, LongBench, and CMRC 2018 datasets.

Ingest: Each document stored via document mode (meta.ingest_mode="document").
QA: Normalized to common QAItem format from dataset-specific structures.
Baseline: Source document text.
Retrieve: oc.search(context_type="resource") to filter document chunks only.
"""

import json
import time
from typing import Any, Dict, List, Tuple

from benchmarks.adapters.base import EvalAdapter, IngestResult, QAItem


def _detect_document_dataset(data: Any) -> str:
    """Detect dataset type from JSON structure."""
    if isinstance(data, dict):
        # QASPER: dict keyed by paper ID
        first_key = next(iter(data), "")
        first_val = data.get(first_key, {})
        if isinstance(first_val, dict) and "full_text" in first_val:
            return "qasper"
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            if "context" in first and "answers" in first and "answer_start" in first.get("answers", {}):
                return "cmrc"
            if "input" in first and "answers" in first:
                return "longbench"
    raise ValueError(
        "Cannot detect document dataset type. Expected QASPER (dict with full_text), "
        "LongBench (list with input+answers), or CMRC (list with context+answers.answer_start)."
    )


class DocumentAdapter(EvalAdapter):
    """QASPER / LongBench / CMRC 2018 evaluation adapter."""

    def load_dataset(self, dataset_path: str, **kwargs) -> None:
        with open(dataset_path, encoding="utf-8") as f:
            raw = json.load(f)

        dataset_type = kwargs.get("dataset_type", "")
        if not dataset_type:
            dataset_type = _detect_document_dataset(raw)

        self._dataset_type = dataset_type
        self._raw = raw
        self._dataset = self._normalize_dataset(raw, dataset_type)

    def _normalize_dataset(self, raw: Any, dtype: str) -> List[Dict]:
        """Normalize dataset to common format: [{doc_id, title, full_text, qas}]."""
        if dtype == "qasper":
            return self._normalize_qasper(raw)
        elif dtype == "longbench":
            return self._normalize_longbench(raw)
        elif dtype == "cmrc":
            return self._normalize_cmrc(raw)
        raise ValueError(f"Unknown document dataset type: {dtype}")

    def _normalize_qasper(self, raw: Dict) -> List[Dict]:
        docs = []
        for paper_id, paper in raw.items():
            full_text = ""
            for section in paper.get("full_text", []):
                heading = section.get("section_name", "")
                paragraphs = section.get("paragraphs", [])
                if heading:
                    full_text += f"\n## {heading}\n\n"
                full_text += "\n".join(paragraphs) + "\n"

            qas = []
            for qa_entry in paper.get("qas", []):
                question = qa_entry.get("question", "")
                # QASPER answers: list of annotator answers
                for ans_obj in qa_entry.get("answers", []):
                    answer_obj = ans_obj.get("answer", {})
                    if answer_obj.get("unanswerable", False):
                        answer = "unanswerable"
                    elif answer_obj.get("yes_no") is not None:
                        answer = "yes" if answer_obj["yes_no"] else "no"
                    elif answer_obj.get("extractive_spans"):
                        answer = " ".join(answer_obj["extractive_spans"])
                    elif answer_obj.get("free_form_answer"):
                        answer = answer_obj["free_form_answer"]
                    else:
                        continue
                    qas.append({"question": question, "answer": answer, "category": "qasper"})
                    break  # Use first annotator answer

            docs.append({
                "doc_id": paper_id,
                "title": paper.get("title", paper_id),
                "full_text": full_text.strip(),
                "qas": qas,
            })
        return docs

    def _normalize_longbench(self, raw: List[Dict]) -> List[Dict]:
        docs = []
        for i, item in enumerate(raw):
            doc_id = item.get("id", str(i))
            qas = []
            answers = item.get("answers", [])
            if isinstance(answers, list):
                answer = answers[0] if answers else ""
            else:
                answer = str(answers)
            qas.append({
                "question": item.get("input", ""),
                "answer": str(answer),
                "category": item.get("type", "longbench"),
            })
            docs.append({
                "doc_id": doc_id,
                "title": item.get("title", f"doc_{doc_id}"),
                "full_text": item.get("context", ""),
                "qas": qas,
            })
        return docs

    def _normalize_cmrc(self, raw: Any) -> List[Dict]:
        # CMRC: {"data": [{"paragraphs": [{"context": ..., "qas": [...]}]}]}
        paragraphs = []
        if isinstance(raw, dict) and "data" in raw:
            for article in raw["data"]:
                for para in article.get("paragraphs", []):
                    paragraphs.append(para)
        elif isinstance(raw, list):
            paragraphs = raw

        docs = []
        for i, para in enumerate(paragraphs):
            context = para.get("context", "")
            qas = []
            for qa in para.get("qas", []):
                answers = qa.get("answers", [])
                answer = answers[0].get("text", "") if answers else ""
                qas.append({
                    "question": qa.get("question", ""),
                    "answer": answer,
                    "category": "cmrc",
                })
            docs.append({
                "doc_id": para.get("id", str(i)),
                "title": f"paragraph_{i}",
                "full_text": context,
                "qas": qas,
            })
        return docs

    async def ingest(self, oc: Any, **kwargs) -> IngestResult:
        """Ingest documents via document mode (meta.ingest_mode='document')."""
        errors: List[str] = []
        ingested = 0

        for doc in self._dataset:
            doc_id = doc["doc_id"]
            try:
                await oc.store(
                    abstract=doc["title"],
                    content=doc["full_text"],
                    context_type="resource",
                    meta={
                        "ingest_mode": "document",
                        "source_path": f"{doc_id}.md",
                    },
                )
                ingested += 1
            except Exception as e:
                errors.append(f"doc={doc_id}: {e}")

        return IngestResult(
            total_items=len(self._dataset),
            ingested_items=ingested,
            errors=errors,
        )

    def build_qa_items(self, **kwargs) -> List[QAItem]:
        max_qa = kwargs.get("max_qa", 0)
        items: List[QAItem] = []

        for doc in self._dataset:
            for qa in doc.get("qas", []):
                items.append(QAItem(
                    question=qa["question"],
                    answer=str(qa.get("answer", "")),
                    category=qa.get("category", ""),
                    meta={"doc_id": doc["doc_id"], "dataset": self._dataset_type},
                ))

        if max_qa > 0:
            items = items[:max_qa]
        return items

    def get_baseline_context(self, qa_item: QAItem) -> str:
        """Source document text for baseline evaluation."""
        doc_id = qa_item.meta.get("doc_id", "")
        for doc in self._dataset:
            if doc["doc_id"] == doc_id:
                return doc["full_text"]
        return ""

    async def retrieve(self, oc: Any, qa_item: QAItem, top_k: int) -> Tuple[List[Dict], float]:
        """Search document chunks with context_type='resource' filter."""
        t0 = time.perf_counter()
        results = await oc.search(
            query=qa_item.question,
            limit=top_k,
            context_type="resource",
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        return results, latency_ms
```

- [ ] **Step 2: Verify import works**

Run: `uv run python -c "from benchmarks.adapters.document import DocumentAdapter; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add benchmarks/adapters/document.py
git commit -m "feat(eval): add document adapter (QASPER + LongBench + CMRC)"
```

---

## Chunk 5: Report + CLI + Contract Tests

### Task 9: Report Module (JSON + Terminal Table)

**Files:**
- Create: `benchmarks/report.py`

- [ ] **Step 1: Implement report.py**

Create `benchmarks/report.py`:

```python
"""
Unified report generation: JSON output + terminal table.

Produces structured reports from per-query eval records with:
- Retrieval quality (recall@k, precision@k, MRR)
- QA accuracy (F1, LLM-judge, baseline comparison)
- Token reduction (OC vs baseline)
- Recall latency (p50/p95/p99)
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def build_report(
    mode: str,
    dataset: str,
    retrieval_metrics: Dict[str, Any],
    accuracy: Dict[str, Any],
    token_metrics: Dict[str, Any],
    latency_metrics: Dict[str, Any],
    metadata: Dict[str, Any],
    per_query: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """Build the unified JSON report structure."""
    report: Dict[str, Any] = {
        "mode": mode,
        "dataset": dataset,
        "retrieval": retrieval_metrics,
        "accuracy": accuracy,
        "token_reduction": token_metrics,
        "latency": latency_metrics,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **metadata,
        },
    }
    if per_query is not None:
        report["per_query"] = per_query
    return report


def save_report(report: Dict[str, Any], output_dir: str, run_id: str) -> str:
    """Save report to JSON file. Returns the file path."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{report['mode']}-{run_id}.json"
    filepath = out_dir / filename
    filepath.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return str(filepath)


def print_report(report: Dict[str, Any]) -> None:
    """Print human-readable terminal table."""
    mode = report.get("mode", "unknown")
    dataset = report.get("dataset", "unknown")
    print(f"\n{'=' * 60}")
    print(f"  OpenCortex Unified Eval — {mode} mode ({dataset})")
    print(f"{'=' * 60}")

    # Retrieval quality
    retrieval = report.get("retrieval", {})
    if retrieval and retrieval.get("evaluated_count", 0) > 0:
        print(f"\n--- Retrieval Quality (n={retrieval['evaluated_count']}) ---")
        header = f"{'':>12} {'Recall@1':>10} {'Recall@3':>10} {'Recall@5':>10} {'MRR':>8}"
        print(header)
        r1 = retrieval.get("recall@1", 0)
        r3 = retrieval.get("recall@3", 0)
        r5 = retrieval.get("recall@5", 0)
        mrr = retrieval.get("mrr", 0)
        print(f"{'Overall':>12} {r1:>10.3f} {r3:>10.3f} {r5:>10.3f} {mrr:>8.3f}")

        by_cat = retrieval.get("by_category", {})
        for cat, metrics in sorted(by_cat.items()):
            cr1 = metrics.get("recall@1", 0)
            cr3 = metrics.get("recall@3", 0)
            cr5 = metrics.get("recall@5", 0)
            cmrr = metrics.get("mrr", 0)
            print(f"{cat:>12} {cr1:>10.3f} {cr3:>10.3f} {cr5:>10.3f} {cmrr:>8.3f}")

    # QA Accuracy
    accuracy_data = report.get("accuracy", {})
    f1_data = accuracy_data.get("f1", {})
    if f1_data:
        print(f"\n{'=' * 60}")
        print(f"{'QA Accuracy (F1)':^60}")
        print(f"{'-' * 60}")
        bl_f1 = accuracy_data.get("baseline_f1")
        header = f"{'Category':<22} "
        if bl_f1 is not None:
            header += f"{'Baseline':>10} {'OpenCortex':>12} {'Delta':>8}"
        else:
            header += f"{'OpenCortex':>12} {'N':>6}"
        print(header)
        print(f"{'-' * 60}")

        by_cat = f1_data.get("by_category", {})
        for cat, cat_data in sorted(by_cat.items()):
            cat_f1 = cat_data.get("f1", 0)
            n = cat_data.get("n", 0)
            if bl_f1 is not None:
                bl_cat = accuracy_data.get("baseline_by_category", {}).get(cat, {}).get("f1", "-")
                if isinstance(bl_cat, float):
                    delta = f"{cat_f1 - bl_cat:+.4f}"
                else:
                    delta = "-"
                print(f"{cat:<22} {str(bl_cat):>10} {cat_f1:>12.4f} {delta:>8}")
            else:
                print(f"{cat:<22} {cat_f1:>12.4f} {n:>6}")

        print(f"{'-' * 60}")
        overall_f1 = f1_data.get("overall", 0)
        if bl_f1 is not None:
            delta = f"{overall_f1 - bl_f1:+.4f}"
            print(f"{'Overall':<22} {bl_f1:>10.4f} {overall_f1:>12.4f} {delta:>8}")
        else:
            print(f"{'Overall':<22} {overall_f1:>12.4f}")
        print(f"{'=' * 60}")

    # Token reduction
    token = report.get("token_reduction", {})
    if token and token.get("baseline_total_tokens", 0) > 0:
        budget = report.get("metadata", {}).get("max_context_tokens", "?")
        print(f"\n--- Token Reduction (budget: {budget} tokens) ---")
        bl_avg = token.get("baseline_avg_tokens", 0)
        oc_avg = token.get("oc_avg_tokens", 0)
        pct = token.get("reduction_pct", 0)
        raw_avg = token.get("raw_baseline_avg_tokens")
        if raw_avg and raw_avg > bl_avg:
            print(f"  Baseline avg:  {bl_avg:,} tokens (raw: {raw_avg:,}, truncated)")
        else:
            print(f"  Baseline avg:  {bl_avg:,} tokens")
        print(f"  OpenCortex avg: {oc_avg:,} tokens")
        print(f"  Reduction:      {pct:.1f}%")

    # Latency
    latency = report.get("latency", {})
    if latency and latency.get("count", 0) > 0:
        print(f"\n--- Recall Latency (n={latency['count']}) ---")
        print(f"  p50: {latency.get('p50_ms', 0):.0f}ms   "
              f"p95: {latency.get('p95_ms', 0):.0f}ms   "
              f"p99: {latency.get('p99_ms', 0):.0f}ms")

    print()
```

- [ ] **Step 2: Verify import works**

Run: `uv run python -c "from benchmarks.report import build_report, print_report, save_report; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add benchmarks/report.py
git commit -m "feat(eval): add report module with JSON + terminal table output"
```

---

### Task 10: Unified CLI Entry Point

**Files:**
- Create: `benchmarks/unified_eval.py`

- [ ] **Step 1: Implement unified_eval.py**

Create `benchmarks/unified_eval.py`:

```python
#!/usr/bin/env python3
"""
OpenCortex Unified Evaluation Framework.

Covers all three ingestion modes (memory, conversation, document) with
four result dimensions: retrieval quality, QA accuracy, token reduction,
and recall latency.

Usage:
    # Conversation mode with LoCoMo
    uv run python benchmarks/unified_eval.py \
        --mode conversation --dataset locomo \
        --data benchmarks/locomo10.json \
        --server http://127.0.0.1:8921 --token <jwt> \
        --llm-base https://api.example.com/v1 --llm-key <key> --llm-model gpt-4o

    # Document mode with QASPER + LLM judge
    uv run python benchmarks/unified_eval.py \
        --mode document --dataset qasper \
        --enable-llm-judge \
        --llm-base ... --llm-key ... --llm-model ...

    # All modes
    uv run python benchmarks/unified_eval.py --mode all ...

    # Quick test (5 QA only)
    uv run python benchmarks/unified_eval.py --mode memory --max-qa 5 ...
"""

import argparse
import asyncio
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opencortex.auth.token import ensure_secret, generate_token
from opencortex.parse.base import estimate_tokens

from benchmarks.llm_client import LLMClient
from benchmarks.metrics import (
    compute_latency_metrics,
    compute_retrieval_metrics,
    compute_token_metrics,
    truncate_to_budget,
)
from benchmarks.oc_client import OCClient
from benchmarks.report import build_report, print_report, save_report
from benchmarks.scoring import f1_score, llm_judge_score, score_qa


# Answer prompt templates (from locomo_eval.py)
ANSWER_PROMPT = (
    "Based on the above context, answer with a short phrase. "
    "Use exact words from the context when possible.\n\n"
    "Question: {question}\nShort answer:"
)
ANSWER_PROMPT_CAT5 = (
    "Based on the above context, answer the question.\n\n"
    "Question: {question}\nShort answer:"
)


# Default dataset paths for --mode all (one per mode)
_DEFAULT_DATASETS = {
    "memory": ("personamem", "benchmarks/datasets/personamem/data.json"),
    "conversation": ("locomo", "benchmarks/locomo10.json"),
    "document": ("qasper", "benchmarks/datasets/qasper/data.json"),
}


def _default_dataset_path(mode: str, dataset_name: str = "") -> str:
    """Resolve default dataset path for a mode (used when --data is not set)."""
    if dataset_name:
        # Named dataset → look in benchmarks/datasets/<name>/
        return f"benchmarks/datasets/{dataset_name}/data.json"
    if mode in _DEFAULT_DATASETS:
        return _DEFAULT_DATASETS[mode][1]
    return ""


def _get_adapter(mode: str):
    """Import and return the adapter for the given mode."""
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


def _build_prompt(context: str, question: str, category: str = "") -> str:
    """Build LLM prompt from retrieved context and question."""
    tpl = ANSWER_PROMPT_CAT5 if category == "5" else ANSWER_PROMPT
    return f"Relevant context:\n{context}\n\n{tpl.format(question=question)}"


async def run_mode(
    mode: str,
    args,
    log,
) -> Dict[str, Any]:
    """Run evaluation for a single mode. Returns the report dict."""
    # Tenant isolation
    run_id = args.run_id or f"eval_{mode}_{uuid4().hex[:8]}"
    data_root = args.data_root
    jwt_token = args.token or generate_token(run_id, "eval_runner", ensure_secret(data_root))

    log(f"Mode: {mode} | Run ID: {run_id}")

    # Init clients
    oc = OCClient(args.server, jwt_token, timeout=120.0)
    llm = LLMClient(
        args.llm_base, args.llm_key, args.llm_model,
        api_style=args.llm_api_style, no_thinking=args.no_thinking,
    )

    try:
        # Load adapter + dataset
        adapter = _get_adapter(mode)
        dataset_path = args.data or _default_dataset_path(mode, args.dataset)
        if not dataset_path:
            raise ValueError("--data is required (or use --mode all for default datasets)")
        adapter.load_dataset(dataset_path)

        # Phase 1: Ingest
        if not args.skip_ingest:
            log("Ingesting dataset...")
            ingest_result = await adapter.ingest(oc, max_conv=args.max_conv)
            log(f"  Ingested {ingest_result.ingested_items}/{ingest_result.total_items}"
                f" ({len(ingest_result.errors)} errors)")
            if ingest_result.errors:
                for err in ingest_result.errors[:5]:
                    log(f"  ERROR: {err}")
            await asyncio.sleep(1)  # let embeddings settle

        # Phase 2: Build QA items
        qa_items = adapter.build_qa_items(max_qa=args.max_qa)
        log(f"Evaluating {len(qa_items)} QA items (top_k={args.top_k})...")

        # Phase 3: Evaluate
        rng = random.Random(args.seed)
        sem = asyncio.Semaphore(args.concurrency)
        records: List[Dict[str, Any]] = []
        done_count = 0

        async def eval_one(qa_item):
            nonlocal done_count
            async with sem:
                record: Dict[str, Any] = {
                    "question": qa_item.question,
                    "answer": qa_item.answer,
                    "category": qa_item.category,
                    "expected_uris": qa_item.expected_uris,
                }

                # Retrieve via OC
                oc_context = ""
                retrieved_uris: List[str] = []
                latency_ms = 0.0
                if not args.baseline_only:
                    results, latency_ms = await adapter.retrieve(oc, qa_item, args.top_k)
                    record["latency_ms"] = latency_ms
                    retrieved_uris = [
                        r.get("uri", "") for r in results
                        if isinstance(r, dict) and r.get("uri")
                    ]
                    record["retrieved_uris"] = retrieved_uris

                    # Build OC context from results
                    ctx_parts = []
                    for r in results:
                        if isinstance(r, dict):
                            ctx_parts.append(
                                r.get("content") or r.get("overview") or r.get("abstract", "")
                            )
                        elif isinstance(r, str):
                            ctx_parts.append(r)
                    oc_context = "\n\n---\n\n".join(ctx_parts) if ctx_parts else "(no results)"

                # OC path: LLM answer
                oc_prediction = ""
                oc_prompt = ""
                if not args.baseline_only and oc_context:
                    oc_prompt = _build_prompt(oc_context, qa_item.question, qa_item.category)
                    try:
                        oc_prediction = await llm.complete(oc_prompt, max_tokens=512)
                    except Exception as e:
                        log(f"  LLM error (OC): {e}")
                    record["oc_prediction"] = oc_prediction
                    record["oc_prompt_tokens"] = estimate_tokens(oc_prompt)

                # Baseline path: LLM answer
                bl_prediction = ""
                bl_prompt = ""
                if not args.oc_only:
                    raw_context = adapter.get_baseline_context(qa_item)
                    truncated_context = truncate_to_budget(raw_context, args.max_context_tokens)
                    bl_prompt = _build_prompt(truncated_context, qa_item.question, qa_item.category)
                    try:
                        bl_prediction = await llm.complete(bl_prompt, max_tokens=512)
                    except Exception as e:
                        log(f"  LLM error (BL): {e}")
                    record["bl_prediction"] = bl_prediction
                    record["baseline_prompt_tokens"] = estimate_tokens(bl_prompt)
                    record["raw_baseline_tokens"] = estimate_tokens(raw_context)

                # Scoring
                cat = int(qa_item.category) if qa_item.category.isdigit() else 0
                if oc_prediction:
                    record["oc_f1"] = score_qa(oc_prediction, qa_item.answer, cat)
                if bl_prediction:
                    record["bl_f1"] = score_qa(bl_prediction, qa_item.answer, cat)

                # LLM-as-Judge (optional)
                if args.enable_llm_judge:
                    if oc_prediction:
                        record["oc_judge"] = await llm_judge_score(
                            oc_prediction, qa_item.answer, qa_item.question, llm.complete
                        )
                    if bl_prediction:
                        record["bl_judge"] = await llm_judge_score(
                            bl_prediction, qa_item.answer, qa_item.question, llm.complete
                        )

                done_count += 1
                if done_count % 25 == 0 or done_count == len(qa_items):
                    log(f"  Progress: {done_count}/{len(qa_items)}")

                return record

        records = await asyncio.gather(*[eval_one(item) for item in qa_items])
        records = list(records)

        # Phase 4: Compute metrics
        # Retrieval
        retrieval_metrics = compute_retrieval_metrics(
            [r for r in records if r.get("retrieved_uris") is not None],
            ks=[1, 3, 5],
        )

        # QA Accuracy
        oc_f1s = [r["oc_f1"] for r in records if "oc_f1" in r]
        bl_f1s = [r["bl_f1"] for r in records if "bl_f1" in r]

        # Per-category F1
        oc_by_cat: Dict[str, List[float]] = {}
        bl_by_cat: Dict[str, List[float]] = {}
        for r in records:
            cat = r.get("category", "unknown")
            if "oc_f1" in r:
                oc_by_cat.setdefault(cat, []).append(r["oc_f1"])
            if "bl_f1" in r:
                bl_by_cat.setdefault(cat, []).append(r["bl_f1"])

        accuracy: Dict[str, Any] = {}
        if oc_f1s:
            oc_overall = sum(oc_f1s) / len(oc_f1s)
            oc_cat_agg = {
                cat: {"f1": round(sum(s) / len(s), 4), "n": len(s)}
                for cat, s in oc_by_cat.items()
            }
            accuracy["f1"] = {"overall": round(oc_overall, 4), "by_category": oc_cat_agg}
        if bl_f1s:
            bl_overall = sum(bl_f1s) / len(bl_f1s)
            accuracy["baseline_f1"] = round(bl_overall, 4)
            accuracy["baseline_by_category"] = {
                cat: {"f1": round(sum(s) / len(s), 4), "n": len(s)}
                for cat, s in bl_by_cat.items()
            }
            if oc_f1s:
                accuracy["delta_f1"] = f"{oc_overall - bl_overall:+.4f}"

        # LLM Judge
        if args.enable_llm_judge:
            oc_judges = [r["oc_judge"] for r in records if "oc_judge" in r]
            if oc_judges:
                accuracy["llm_judge"] = {
                    "overall": round(sum(oc_judges) / len(oc_judges), 4),
                }

        # Token reduction
        token_records = [
            r for r in records
            if "oc_prompt_tokens" in r and "baseline_prompt_tokens" in r
        ]
        token_metrics = compute_token_metrics(token_records)
        # Add raw baseline info
        raw_tokens = [r.get("raw_baseline_tokens", 0) for r in records if "raw_baseline_tokens" in r]
        if raw_tokens:
            token_metrics["raw_baseline_avg_tokens"] = round(sum(raw_tokens) / len(raw_tokens))
            token_metrics["truncation_applied"] = any(
                r.get("raw_baseline_tokens", 0) > r.get("baseline_prompt_tokens", 0)
                for r in records
            )
            token_metrics["max_context_tokens"] = args.max_context_tokens

        # Latency
        latencies = [r["latency_ms"] for r in records if "latency_ms" in r]
        latency_metrics = compute_latency_metrics(latencies)

        # Phase 5: Build report
        metadata = {
            "run_id": run_id,
            "llm_model": args.llm_model,
            "server": args.server,
            "dataset_path": dataset_path,
            "top_k": args.top_k,
            "max_context_tokens": args.max_context_tokens,
            "concurrency": args.concurrency,
            "total_qa": len(qa_items),
            "enable_llm_judge": args.enable_llm_judge,
            "seed": args.seed,
        }

        report = build_report(
            mode=mode,
            dataset=args.dataset or mode,
            retrieval_metrics=retrieval_metrics,
            accuracy=accuracy,
            token_metrics=token_metrics,
            latency_metrics=latency_metrics,
            metadata=metadata,
            per_query=records,
        )

        # Print + save
        print_report(report)
        output_dir = args.output or "docs/benchmark"
        filepath = save_report(report, output_dir, run_id)
        log(f"Report saved to {filepath}")

        return report

    finally:
        await oc.close()
        await llm.close()


async def run(args):
    """Main entry point."""
    def log(msg):
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)

    modes = ["memory", "conversation", "document"] if args.mode == "all" else [args.mode]
    reports = []

    for mode in modes:
        report = await run_mode(mode, args, log)
        reports.append(report)

    if len(reports) > 1:
        # Write summary report for --mode all
        summary = {
            "mode": "all",
            "modes": {r["mode"]: {
                "retrieval": r.get("retrieval", {}),
                "accuracy": r.get("accuracy", {}),
                "token_reduction": r.get("token_reduction", {}),
                "latency": r.get("latency", {}),
            } for r in reports},
            "metadata": {
                "timestamp": reports[-1]["metadata"]["timestamp"],
                "modes_run": [r["mode"] for r in reports],
            },
        }
        output_dir = args.output or "docs/benchmark"
        summary_path = save_report(
            {"mode": "all", **summary},
            output_dir,
            f"all_{uuid4().hex[:8]}",
        )
        log(f"Summary report saved to {summary_path}")
        log("All modes complete.")


def main():
    p = argparse.ArgumentParser(description="OpenCortex Unified Evaluation Framework")

    # Mode + dataset
    p.add_argument("--mode", required=True, choices=["memory", "conversation", "document", "all"])
    p.add_argument("--dataset", default="", help="Dataset name (personamem, locomo, longmemeval, qasper, longbench, cmrc)")
    p.add_argument("--data", default="", help="Dataset file path")

    # Server
    p.add_argument("--server", default="http://127.0.0.1:8921", help="OpenCortex server URL")
    p.add_argument("--token", default="", help="JWT Bearer token (auto-generated if empty)")
    p.add_argument("--data-root", default="./data", help="Server data_root for JWT generation")

    # LLM
    p.add_argument("--llm-base", required=True, help="LLM API base URL")
    p.add_argument("--llm-key", required=True, help="LLM API key")
    p.add_argument("--llm-model", required=True, help="LLM model name")
    p.add_argument("--llm-api-style", default="auto", choices=["auto", "openai", "anthropic"])
    p.add_argument("--no-thinking", action="store_true", help="Disable LLM reasoning/thinking")

    # Eval params
    p.add_argument("--top-k", type=int, default=10, help="Retrieval limit")
    p.add_argument("--max-context-tokens", type=int, default=32000, help="Baseline prompt budget")
    p.add_argument("--concurrency", type=int, default=5, help="Concurrent QA evaluations")
    p.add_argument("--enable-llm-judge", action="store_true", help="Enable LLM-as-Judge scoring")

    # Run control
    p.add_argument("--skip-ingest", action="store_true", help="Skip ingestion (reuse existing data)")
    p.add_argument("--oc-only", action="store_true", help="Skip baseline evaluation")
    p.add_argument("--baseline-only", action="store_true", help="Skip OC evaluation")
    p.add_argument("--max-qa", type=int, default=0, help="Limit QA count (0=all)")
    p.add_argument("--max-conv", type=int, default=0, help="Limit conversation count (0=all)")
    p.add_argument("--output", default="", help="Report output directory")
    p.add_argument("--run-id", default="", help="Reuse tenant from previous run")
    p.add_argument("--seed", type=int, default=42, help="Random seed")

    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify import works**

Run: `uv run python -c "from benchmarks.unified_eval import main; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add benchmarks/unified_eval.py
git commit -m "feat(eval): add unified CLI entry point"
```

---

### Task 11: Contract Tests (ASGITransport)

**Files:**
- Create: `tests/test_eval_contract.py`
- Reference: `tests/test_http_server.py` (ASGITransport pattern)

- [ ] **Step 1: Create contract tests**

Create `tests/test_eval_contract.py` following the same MockEmbedder + ASGITransport pattern as `tests/test_http_server.py`:

```python
"""
Contract tests for OCClient ↔ HTTP server protocol.

Uses httpx.AsyncClient + ASGITransport against the FastAPI app (same pattern
as tests/test_http_server.py — manual orchestrator setup, bypasses JWT auth).
Verifies that OCClient produces correct HTTP payloads and the server returns
expected shapes.

No external server or LLM API required.
"""

import asyncio
import math
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from typing import Any, Dict, List
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from httpx import ASGITransport

from opencortex.config import CortexConfig, init_config
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.storage.storage_interface import StorageInterface


# Reuse the same MockEmbedder pattern from tests/test_http_server.py
class MockEmbedder(DenseEmbedderBase):
    DIMENSION = 4

    def __init__(self):
        super().__init__(model_name="mock-embedder-v1")

    def embed(self, text: str) -> EmbedResult:
        return EmbedResult(dense_vector=self._text_to_vector(text))

    def get_dimension(self) -> int:
        return self.DIMENSION

    @staticmethod
    def _text_to_vector(text: str) -> List[float]:
        h = hash(text) & 0xFFFFFFFF
        raw = [
            ((h >> 0) & 0xFF) / 255.0,
            ((h >> 8) & 0xFF) / 255.0,
            ((h >> 16) & 0xFF) / 255.0,
            ((h >> 24) & 0xFF) / 255.0,
        ]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]


# Same pattern as tests/test_http_server.py _test_app_context()
@asynccontextmanager
async def _test_app_context():
    """Create a FastAPI app wired to in-memory test backends."""
    from fastapi import FastAPI
    import opencortex.http.server as http_server
    from tests.test_http_server import InMemoryStorage

    temp_dir = tempfile.mkdtemp(prefix="eval_contract_")
    config = CortexConfig(
        data_root=temp_dir,
        embedding_dimension=MockEmbedder.DIMENSION,
    )
    init_config(config)

    storage = InMemoryStorage()
    embedder = MockEmbedder()
    orch = MemoryOrchestrator(config=config, storage=storage, embedder=embedder)
    await orch.init()
    http_server._orchestrator = orch

    app = FastAPI()
    http_server._register_routes(app)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        try:
            yield client
        finally:
            await orch.close()
            http_server._orchestrator = None
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestEvalContract(unittest.TestCase):
    """Contract tests: OCClient ↔ server payload shape verification."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_store_with_meta(self):
        """OCClient.store(meta={"ingest_mode": "document"}) produces correct payload."""
        async def _test():
            async with _test_app_context() as client:
                resp = await client.post(
                    "/api/v1/memory/store",
                    json={
                        "abstract": "Test document title",
                        "content": "# Heading\n\nParagraph content here.",
                        "context_type": "resource",
                        "meta": {"ingest_mode": "document", "source_path": "test.md"},
                        "dedup": False,
                    },
                    headers={
                        "X-Tenant-ID": f"test_{uuid4().hex[:8]}",
                        "X-User-ID": "tester",
                    },
                )
                self.assertIn(resp.status_code, (200, 201), f"Store failed: {resp.text}")
                data = resp.json()
                self.assertIn("uri", data)

        self._run(_test())

    def test_search_with_context_type(self):
        """OCClient.search(context_type="resource") sends correct filter."""
        async def _test():
            async with _test_app_context() as client:
                tenant = f"test_{uuid4().hex[:8]}"
                headers = {"X-Tenant-ID": tenant, "X-User-ID": "tester"}

                # Store a memory
                await client.post(
                    "/api/v1/memory/store",
                    json={
                        "abstract": "test memory",
                        "content": "content",
                        "context_type": "memory",
                        "dedup": False,
                    },
                    headers=headers,
                )

                # Search with context_type filter
                resp = await client.post(
                    "/api/v1/memory/search",
                    json={
                        "query": "test",
                        "limit": 5,
                        "detail_level": "l2",
                        "context_type": "resource",
                    },
                    headers=headers,
                )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("results", data)
                self.assertIsInstance(data["results"], list)

        self._run(_test())

    def test_context_recall_response_shape(self):
        """OCClient.context_recall() response contains expected fields."""
        async def _test():
            async with _test_app_context() as client:
                tenant = f"test_{uuid4().hex[:8]}"
                headers = {"X-Tenant-ID": tenant, "X-User-ID": "tester"}

                resp = await client.post(
                    "/api/v1/context",
                    json={
                        "session_id": "test-session",
                        "phase": "prepare",
                        "turn_id": "t0",
                        "messages": [{"role": "user", "content": "hello"}],
                        "config": {"max_items": 5, "detail_level": "l2"},
                    },
                    headers=headers,
                )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                # Context API prepare returns memory and session_id
                self.assertIn("memory", data)
                self.assertIn("session_id", data)

        self._run(_test())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run contract tests**

Run: `uv run python -m unittest tests.test_eval_contract -v`
Expected: 3 tests PASS (may require server app fixes — adapt assertions to actual response shape)

- [ ] **Step 3: Commit**

```bash
git add tests/test_eval_contract.py
git commit -m "test(eval): add OCClient ↔ server contract tests"
```

---

### Task 12: Mark locomo_eval.py Deprecated + Final Verification

**Files:**
- Modify: `benchmarks/locomo_eval.py:1-5` (add deprecation notice)

- [ ] **Step 1: Add deprecation notice to locomo_eval.py**

Add deprecation warning to the top of the docstring in `benchmarks/locomo_eval.py`:

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
[DEPRECATED] LoCoMo Benchmark Evaluation for OpenCortex

⚠️  This module is superseded by the unified evaluation framework:
    benchmarks/unified_eval.py
Core logic has been migrated to:
    - benchmarks/oc_client.py (OCClient)
    - benchmarks/llm_client.py (LLMClient)
    - benchmarks/scoring.py (F1 scoring)
    - benchmarks/adapters/conversation.py (conversation adapter)
This file is retained for backward compatibility only.

...existing docstring continues...
```

- [ ] **Step 2: Run all unit tests**

Run: `uv run python -m unittest tests.test_eval_scoring tests.test_eval_metrics -v`
Expected: All tests PASS

- [ ] **Step 3: Run contract tests**

Run: `uv run python -m unittest tests.test_eval_contract -v`
Expected: All 3 tests PASS

- [ ] **Step 4: Verify all imports work end-to-end**

Run: `uv run python -c "from benchmarks.unified_eval import main; from benchmarks.adapters.memory import MemoryAdapter; from benchmarks.adapters.conversation import ConversationAdapter; from benchmarks.adapters.document import DocumentAdapter; from benchmarks.report import build_report; print('All imports OK')"`
Expected: `All imports OK`

- [ ] **Step 5: Commit**

```bash
git add benchmarks/locomo_eval.py
git commit -m "docs: mark locomo_eval.py as deprecated"
```

---

## Summary

| Chunk | Tasks | Files Created | Tests |
|-------|-------|---------------|-------|
| 1: Scoring Module | 1-2 | `benchmarks/scoring.py`, `benchmarks/metrics.py` | `tests/test_eval_scoring.py` (18), `tests/test_eval_metrics.py` (14) |
| 2: Client Infrastructure | 3-4 | `benchmarks/oc_client.py`, `benchmarks/llm_client.py` | — |
| 3: Adapter Framework | 5-6 | `benchmarks/adapters/base.py`, `benchmarks/adapters/memory.py` | — |
| 4: Mode Adapters | 7-8 | `benchmarks/adapters/conversation.py`, `benchmarks/adapters/document.py` | — |
| 5: Report + CLI + Tests | 9-12 | `benchmarks/report.py`, `benchmarks/unified_eval.py`, `tests/test_eval_contract.py` | `tests/test_eval_contract.py` (3) |

**Total: 12 tasks, 14 new files, 35 test cases, 12 commits**
