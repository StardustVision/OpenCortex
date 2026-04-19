"""
Unified report generation: JSON output + terminal table.

Industry-aligned metric hierarchy:
1. LLM-Judge Accuracy (PRIMARY)
2. Content Recall (RETRIEVAL QUALITY)
3. Token-F1 (QA QUALITY)
4. Token Efficiency (COST)
5. Latency (PERFORMANCE)
6. URI Recall (REFERENCE)
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
    """Print human-readable terminal table with industry-aligned metric order."""
    mode = report.get("mode", "unknown")
    dataset = report.get("dataset", "unknown")
    print(f"\n{'=' * 60}")
    print(f"  OpenCortex Eval — {mode} ({dataset})")
    print(f"{'=' * 60}")

    # ── 1. LLM-Judge Accuracy (PRIMARY) ──
    accuracy_data = report.get("accuracy", {})
    jscore_data = accuracy_data.get("jscore", {})
    if jscore_data and jscore_data.get("overall") is not None:
        print(f"\n{'=' * 60}")
        print(f"{'LLM-Judge Accuracy — Primary Metric':^60}")
        print(f"{'-' * 60}")
        bl_j = jscore_data.get("baseline_overall")
        header = f"{'Category':<22} "
        if bl_j is not None:
            header += f"{'Baseline':>10} {'OpenCortex':>12} {'Delta':>8}"
        else:
            header += f"{'OpenCortex':>12} {'N':>6}"
        print(header)
        print(f"{'-' * 60}")

        oc_j_by_cat = jscore_data.get("by_category", {})
        bl_j_by_cat = jscore_data.get("baseline_by_category", {})
        for cat in sorted(set(list(oc_j_by_cat.keys()) + list(bl_j_by_cat.keys()))):
            oc_cat = oc_j_by_cat.get(cat, {})
            bl_cat = bl_j_by_cat.get(cat, {})
            oc_val = oc_cat.get("jscore", 0)
            n = oc_cat.get("n", bl_cat.get("n", 0))
            if bl_j is not None:
                bl_val = bl_cat.get("jscore", "-")
                if isinstance(bl_val, (int, float)):
                    delta = f"{oc_val - bl_val:+.4f}"
                else:
                    delta = "-"
                print(f"{cat:<22} {str(bl_val):>10} {oc_val:>12.4f} {delta:>8}")
            else:
                print(f"{cat:<22} {oc_val:>12.4f} {n:>6}")

        print(f"{'-' * 60}")
        overall_j = jscore_data.get("overall", 0)
        if bl_j is not None:
            delta_j = f"{overall_j - bl_j:+.4f}"
            print(f"{'Overall (excl. 5)':<22} {bl_j:>10.4f} {overall_j:>12.4f} {delta_j:>8}")
        else:
            print(f"{'Overall (excl. 5)':<22} {overall_j:>12.4f}")
        print(f"{'=' * 60}")

    # ── 2. Content Recall (RETRIEVAL QUALITY) ──
    retrieval = report.get("retrieval", {})
    cr = retrieval.get("content_recall", {})
    if cr and cr.get("evaluated_count", 0) > 0:
        print(f"\n{'─' * 60}")
        print(f"  Content Recall (evidence-based, n={cr['evaluated_count']})")
        print(f"{'─' * 60}")
        print(f"  Overall: {cr.get('content_recall', 0):.3f}")
        cr_by_cat = cr.get("by_category", {})
        for cat, cat_data in sorted(cr_by_cat.items()):
            print(f"  {cat:<30} {cat_data.get('content_recall', 0):.3f}  (n={int(cat_data.get('count', 0))})")

    # ── 3. Token-F1 (QA QUALITY) ──
    f1_data = accuracy_data.get("f1", {})
    if f1_data:
        print(f"\n{'─' * 60}")
        print(f"  QA Accuracy (Token-F1)")
        print(f"{'─' * 60}")
        bl_f1 = accuracy_data.get("baseline_f1")
        excluded = set(f1_data.get("excluded_categories", []))
        by_cat = f1_data.get("by_category", {})
        for cat, cat_data in sorted(by_cat.items()):
            cat_f1 = cat_data.get("f1", 0)
            n = cat_data.get("n", 0)
            label = f"{cat} *" if cat in excluded else cat
            if bl_f1 is not None:
                bl_cat = accuracy_data.get("baseline_by_category", {}).get(cat, {}).get("f1", "-")
                if isinstance(bl_cat, float):
                    delta = f"{cat_f1 - bl_cat:+.4f}"
                else:
                    delta = "-"
                print(f"  {label:<28} BL:{str(bl_cat):>6}  OC:{cat_f1:>6.4f}  {delta:>8}")
            else:
                print(f"  {label:<28} {cat_f1:>12.4f}  (n={n})")

        overall_f1 = f1_data.get("overall", 0)
        excl_note = f" (excl. {','.join(sorted(excluded))})" if excluded else ""
        if bl_f1 is not None:
            delta = f"{overall_f1 - bl_f1:+.4f}"
            print(f"  {'Overall' + excl_note:<28} BL:{bl_f1:>6.4f}  OC:{overall_f1:>6.4f}  {delta:>8}")
        else:
            print(f"  {'Overall' + excl_note:<28} {overall_f1:>12.4f}")
        if excluded:
            print(f"  * Category {','.join(sorted(excluded))} excluded from overall")

    # ── 4. Token Efficiency (COST) ──
    token = report.get("token_reduction", {})
    if token and token.get("baseline_total_tokens", 0) > 0:
        budget = report.get("metadata", {}).get("max_context_tokens", "?")
        print(f"\n{'─' * 60}")
        print(f"  Token Efficiency (budget: {budget} tokens)")
        print(f"{'─' * 60}")
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

    # ── 5. Latency (PERFORMANCE) ──
    latency = report.get("latency", {})
    if latency and latency.get("count", 0) > 0:
        print(f"\n{'─' * 60}")
        print(f"  Recall Latency (n={latency['count']})")
        print(f"{'─' * 60}")
        print(f"  p50: {latency.get('p50_ms', 0):.0f}ms   "
              f"p95: {latency.get('p95_ms', 0):.0f}ms   "
              f"p99: {latency.get('p99_ms', 0):.0f}ms")

    # ── 6. URI Recall (REFERENCE) ──
    if retrieval and retrieval.get("evaluated_count", 0) > 0:
        print(f"\n{'─' * 60}")
        print(f"  URI Recall (reference, n={retrieval['evaluated_count']})")
        print(f"{'─' * 60}")
        r1 = retrieval.get("recall@1", 0)
        r3 = retrieval.get("recall@3", 0)
        r5 = retrieval.get("recall@5", 0)
        mrr = retrieval.get("mrr", 0)
        print(f"  Recall@1: {r1:.3f}  Recall@3: {r3:.3f}  Recall@5: {r5:.3f}  MRR: {mrr:.3f}")

        by_cat = retrieval.get("by_category", {})
        if by_cat:
            for cat, metrics in sorted(by_cat.items()):
                cr1 = metrics.get("recall@1", 0)
                cr3 = metrics.get("recall@3", 0)
                cr5 = metrics.get("recall@5", 0)
                print(f"  {cat:<22} R@1:{cr1:.3f}  R@3:{cr3:.3f}  R@5:{cr5:.3f}")

    print()
