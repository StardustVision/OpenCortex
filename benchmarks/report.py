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
