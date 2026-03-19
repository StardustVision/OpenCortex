#!/usr/bin/env python3
"""Ablation experiment framework — single-variable sweep over benchmark datasets."""
import argparse
import csv
import json
import sys
from pathlib import Path


def run_ablation(args):
    # Import runner from same directory
    sys.path.insert(0, str(Path(__file__).parent))
    from runner import run_benchmark

    values = [v.strip() for v in args.values.split(",")]
    results = []

    for val in values:
        print(f"\n=== {args.variable} = {val} ===", file=sys.stderr)
        report = run_benchmark(
            base_url=args.base_url,
            data_root=args.data_root,
            dataset_path=args.dataset,
            ks=[5],
            timeout=args.timeout,
        )
        summary = report.get("summary", {})
        results.append({
            "variable": args.variable,
            "value": val,
            "j_score": summary.get("j_score", 0),
            "f1": summary.get("f1", 0),
            "p50_ms": summary.get("latency_p50_ms", 0),
            "rerank_rate": summary.get("rerank_trigger_rate", 0),
        })

    if args.output:
        with open(args.output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["variable", "value", "j_score", "f1", "p50_ms", "rerank_rate"])
            writer.writeheader()
            writer.writerows(results)
        print(f"\nResults saved to {args.output}", file=sys.stderr)
    else:
        json.dump(results, sys.stdout, indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ablation experiment sweep")
    parser.add_argument("--variable", required=True, help="Variable to sweep")
    parser.add_argument("--values", required=True, help="Comma-separated values")
    parser.add_argument("--base-url", default="http://127.0.0.1:8921")
    parser.add_argument("--data-root", default="~/.opencortex")
    parser.add_argument("--dataset", required=True, help="Path to dataset JSON")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--output", help="Output CSV path")
    run_ablation(parser.parse_args())
