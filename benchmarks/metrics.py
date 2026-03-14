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
