"""
Unified evaluation metrics: retrieval quality, latency, token reduction,
NDCG@k, and bootstrap confidence intervals.

No external dependencies (no numpy).
"""

import math
import random
import re
from typing import Any, Dict, List, Sequence, Tuple


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
    records: List[Dict[str, Any]], ks: Sequence[int] = (1, 3, 5)
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


def _normalize_content(s: str) -> str:
    """Lowercase, strip punctuation for soft matching."""
    return re.sub(r"[^\w\s]", "", str(s).lower())


_CONTENT_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "to", "of", "in", "on", "at", "for",
    "from", "with", "did", "does", "do", "is", "was", "were", "be", "been",
    "has", "have", "had", "will", "would", "could", "should", "can", "may",
    "it", "its", "i", "me", "my", "we", "us", "our", "you", "your", "he",
    "she", "they", "them", "their", "this", "that", "these", "those",
    "what", "when", "where", "who", "why", "how", "which", "so", "very",
    "just", "also", "then", "than", "up", "out", "about", "into", "over",
})


def _content_tokens(text: str) -> set[str]:
    """Extract meaningful tokens (no stopwords, no short words) for matching."""
    normalized = _normalize_content(text)
    return {t for t in normalized.split() if t not in _CONTENT_STOPWORDS and len(t) >= 3}


def compute_content_recall(
    records: List[Dict[str, Any]],
    soft_threshold: float = 0.5,
    min_key_tokens: int = 2,
) -> Dict[str, Any]:
    """Compute evidence-based content recall using key-entity overlap.

    Each record must have:
        - retrieved_content: List[str] — text content of retrieved chunks
        - evidence_texts (in meta): List[str] — resolved evidence turn text

    Matches by checking whether key entities from evidence text appear in
    retrieved content. Uses stopword-filtered token overlap rather than
    raw token overlap, since LLM-derived overviews rephrase raw turns.
    """
    scored: List[float] = []
    by_category: Dict[str, List[float]] = {}

    for record in records:
        evidence_texts = record.get("meta", {}).get("evidence_texts", [])
        if not evidence_texts:
            continue
        retrieved = record.get("retrieved_content", [])
        if not retrieved:
            scored.append(0.0)
            cat = str(record.get("category", "unknown"))
            by_category.setdefault(cat, []).append(0.0)
            continue

        combined = " ".join(retrieved)
        combined_keys = _content_tokens(combined)
        hit_count = 0

        for evidence in evidence_texts:
            ev_keys = _content_tokens(evidence)
            if len(ev_keys) < min_key_tokens:
                # For short evidence, check raw substring
                if evidence in combined:
                    hit_count += 1
                continue
            overlap = len(ev_keys & combined_keys)
            if overlap / len(ev_keys) >= soft_threshold:
                hit_count += 1

        recall = hit_count / len(evidence_texts)
        scored.append(recall)
        cat = str(record.get("category", "unknown"))
        by_category.setdefault(cat, []).append(recall)

    result: Dict[str, Any] = {}
    result["content_recall"] = round(sum(scored) / len(scored), 4) if scored else 0.0
    result["evaluated_count"] = len(scored)

    cat_result: Dict[str, Dict[str, float]] = {}
    for cat, vals in by_category.items():
        cat_result[cat] = {
            "content_recall": round(sum(vals) / len(vals), 4),
            "count": float(len(vals)),
        }
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

    Requires src/ on sys.path (caller responsibility, e.g. unified_eval.py).
    """
    from opencortex.parse.base import estimate_tokens

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


# ---------------------------------------------------------------------------
# NDCG@k (Normalized Discounted Cumulative Gain)
# ---------------------------------------------------------------------------

def _dcg_at_k(relevances: List[float], k: int) -> float:
    """Compute DCG@k."""
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances[:k]))


def ndcg_at_k(retrieved: List[str], expected: List[str], k: int = 5) -> float:
    """Compute NDCG@k for a single query.

    Binary relevance: 1.0 if item in expected, 0.0 otherwise.
    """
    if not expected:
        return 0.0
    expected_set = {_normalize_uri(u) for u in expected if u.strip()}
    retrieved_norm = [_normalize_uri(u) for u in retrieved if u.strip()]
    relevances = [1.0 if u in expected_set else 0.0 for u in retrieved_norm]

    actual = _dcg_at_k(relevances, k)
    ideal_rels = sorted(relevances, reverse=True)
    ideal = _dcg_at_k(ideal_rels, k)
    return actual / ideal if ideal > 0 else 0.0


def compute_ndcg(records: List[Dict[str, Any]], k: int = 5) -> Dict[str, Any]:
    """Compute NDCG@k overall and per-category.

    Each record must have:
        - retrieved_uris: List[str]
        - expected_uris: List[str]
        - category (optional): str
    """
    scored: List[float] = []
    by_category: Dict[str, List[float]] = {}

    for record in records:
        expected = record.get("expected_uris", [])
        if not expected:
            continue
        retrieved = record.get("retrieved_uris", [])
        val = ndcg_at_k(retrieved, expected, k)
        scored.append(val)
        cat = str(record.get("category", "unknown")).strip() or "unknown"
        by_category.setdefault(cat, []).append(val)

    result: Dict[str, Any] = {}
    key = f"ndcg@{k}"
    result[key] = round(sum(scored) / len(scored), 4) if scored else 0.0
    result["count"] = len(scored)

    cat_result: Dict[str, Dict[str, float]] = {}
    for cat, vals in by_category.items():
        cat_result[cat] = {
            key: round(sum(vals) / len(vals), 4) if vals else 0.0,
            "count": float(len(vals)),
        }
    result["by_category"] = cat_result
    return result


# ---------------------------------------------------------------------------
# Bootstrap confidence intervals
# ---------------------------------------------------------------------------

def bootstrap_ci(
    values: List[float],
    confidence: float = 0.95,
    n_bootstrap: int = 1000,
    seed: int = 42,
) -> Tuple[float, float]:
    """Compute bootstrap confidence interval for the mean of values.

    Returns (lower_bound, upper_bound) at the given confidence level.
    """
    if not values:
        return (0.0, 0.0)
    n = len(values)
    rng = random.Random(seed)
    means = []
    for _ in range(n_bootstrap):
        sample = [values[rng.randint(0, n - 1)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    alpha = (1 - confidence) / 2
    lo = means[int(alpha * n_bootstrap)]
    hi = means[int((1 - alpha) * n_bootstrap)]
    return (round(lo, 4), round(hi, 4))


def compute_retrieval_metrics_with_ci(
    records: List[Dict[str, Any]],
    ks: Sequence[int] = (1, 3, 5),
    n_bootstrap: int = 1000,
) -> Dict[str, Any]:
    """Like compute_retrieval_metrics but adds 95% CI for each metric."""
    base = compute_retrieval_metrics(records, ks)
    # Collect per-query metric values
    per_query: Dict[str, List[float]] = {}
    for record in records:
        expected = record.get("expected_uris", [])
        if not expected:
            continue
        retrieved = record.get("retrieved_uris", [])
        m = _single_retrieval_metrics(retrieved, expected, ks)
        for key, val in m.items():
            per_query.setdefault(key, []).append(val)
    # Add CIs
    ci_result: Dict[str, Any] = {}
    for key, vals in per_query.items():
        lo, hi = bootstrap_ci(vals, n_bootstrap=n_bootstrap)
        ci_result[key + "_ci"] = {"lower": lo, "upper": hi}
    base["confidence_intervals"] = ci_result
    return base
