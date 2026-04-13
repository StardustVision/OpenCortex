"""Analyze eval results from JSON reports.

Usage:
    python benchmarks/analyze_eval.py docs/benchmark/conversation-eval_conversation_v6full.json
"""

import json
import sys
from collections import defaultdict
from typing import Dict, List


CAT_NAMES = {
    # LoCoMo categories
    "1": "Single-hop factual",
    "2": "Multi-hop factual",
    "3": "Temporal",
    "4": "Open-domain",
    "5": "Adversarial (excluded)",
    # LongMemEval question types
    "single-session-user": "Single-Session (User)",
    "single-session-assistant": "Single-Session (Assistant)",
    "single-session-preference": "Single-Session (Preference)",
    "multi-session": "Multi-Session",
    "temporal-reasoning": "Temporal Reasoning",
    "knowledge-update": "Knowledge Update",
}


def analyze(report_path: str) -> None:
    with open(report_path) as f:
        report = json.load(f)

    pq = report.get("per_query", [])
    if not pq:
        print("No per_query data found")
        return

    acc = report.get("accuracy", {})
    ret = report.get("retrieval", {})
    meta = report.get("metadata", {})
    mode = report.get("mode", "")

    print(f"=== Report: {report_path} ===")
    print(f"Run ID: {meta.get('run_id', '?')}")
    print(f"Total QA: {meta.get('total_qa', len(pq))}")
    print(f"Top K: {meta.get('top_k', '?')}")
    print()

    # Overall metrics
    f1_data = acc.get("f1", {})
    print(f"OC F1 (overall, ex-Cat5): {f1_data.get('overall', '?')}")
    print(f"Baseline F1: {acc.get('baseline_f1', '?')}")
    print(f"Delta: {acc.get('delta_f1', '?')}")
    print()

    # J-Score (primary metric)
    jscore_data = acc.get("jscore", {})
    if jscore_data:
        print("=== J-Score (LLM-as-Judge, Primary) ===")
        print(f"  OC J-Score (overall, ex-Cat5): {jscore_data.get('overall', '?')}")
        print(f"  Baseline J-Score: {jscore_data.get('baseline_overall', '?')}")
        print(f"  Delta: {jscore_data.get('delta', '?')}")
        oc_j_by_cat = jscore_data.get("by_category", {})
        bl_j_by_cat = jscore_data.get("baseline_by_category", {})
        for cat in sorted(set(list(oc_j_by_cat.keys()) + list(bl_j_by_cat.keys()))):
            oc_j = oc_j_by_cat.get(cat, {}).get("jscore", 0)
            bl_j = bl_j_by_cat.get(cat, {}).get("jscore", 0)
            n = oc_j_by_cat.get(cat, {}).get("n", bl_j_by_cat.get(cat, {}).get("n", 0))
            delta = oc_j - bl_j
            name = CAT_NAMES.get(cat, f"Category {cat}")
            print(f"  Cat {cat} ({name}): OC={oc_j:.4f} BL={bl_j:.4f} Δ={delta:+.4f} (n={n})")

        # Per-query J-score wins/ties
        j_total = j_oc_wins = j_bl_wins = j_ties = 0
        for q in pq:
            if "oc_jscore" not in q or "bl_jscore" not in q:
                continue
            j_total += 1
            if q["oc_jscore"] > q["bl_jscore"]:
                j_oc_wins += 1
            elif q["bl_jscore"] > q["oc_jscore"]:
                j_bl_wins += 1
            else:
                j_ties += 1
        if j_total:
            print(f"  Win/Lose/Tie: OC={j_oc_wins} BL={j_bl_wins} Tie={j_ties} (n={j_total})")
        print()

    # Per-category F1
    print("=== Per-Category F1 ===")
    oc_by_cat = f1_data.get("by_category", {})
    bl_by_cat = acc.get("baseline_by_category", {})
    for cat in sorted(set(list(oc_by_cat.keys()) + list(bl_by_cat.keys()))):
        oc = oc_by_cat.get(cat, {})
        bl = bl_by_cat.get(cat, {})
        name = CAT_NAMES.get(cat, f"Category {cat}")
        oc_f1 = oc.get("f1", 0)
        bl_f1 = bl.get("f1", 0)
        n = oc.get("n", bl.get("n", 0))
        delta = oc_f1 - bl_f1
        print(f"  Cat {cat} ({name}): OC={oc_f1:.4f} BL={bl_f1:.4f} Δ={delta:+.4f} (n={n})")
    print()

    # Retrieval
    print("=== Retrieval ===")
    for k in [1, 3, 5, 10]:
        key = f"recall@{k}"
        if key in ret:
            print(f"  Recall@{k}: {ret[key]:.4f}")
    print(f"  MRR: {ret.get('mrr', '?')}")
    print(f"  Evaluated: {ret.get('evaluated_count', '?')}")
    print()

    # Retrieval per category
    ret_by_cat = ret.get("by_category", {})
    if ret_by_cat:
        print("=== Retrieval Per-Category ===")
        for cat in sorted(ret_by_cat.keys()):
            data = ret_by_cat[cat]
            name = CAT_NAMES.get(cat, f"Cat {cat}")
            print(f"  Cat {cat} ({name}):")
            print(f"    Recall@5={data.get('recall@5', 0):.4f} "
                  f"HitRate@5={data.get('hit_rate@5', 0):.4f} "
                  f"MRR={data.get('mrr', 0):.4f} "
                  f"n={data.get('count', 0):.0f}")
        print()

    # Prediction analysis
    print("=== Prediction Analysis ===")
    total = 0
    oc_f1_zero = 0
    no_info_count = 0
    oc_wins = 0
    bl_wins = 0
    ties = 0

    for q in pq:
        if "oc_f1" not in q:
            continue
        total += 1
        if q["oc_f1"] == 0:
            oc_f1_zero += 1
        pred = q.get("oc_prediction", "").lower()
        if any(x in pred for x in ["no information", "not mention", "does not",
                                     "no context", "not provided", "not available",
                                     "i don't have", "no relevant"]):
            no_info_count += 1

        if "bl_f1" in q:
            if q["oc_f1"] > q["bl_f1"]:
                oc_wins += 1
            elif q["bl_f1"] > q["oc_f1"]:
                bl_wins += 1
            else:
                ties += 1

    if total:
        print(f"  Total with OC F1: {total}")
        print(f"  OC F1 = 0: {oc_f1_zero} ({oc_f1_zero/total*100:.1f}%)")
        print(f"  OC says 'no info': {no_info_count} ({no_info_count/total*100:.1f}%)")
        print(f"  OC wins: {oc_wins} ({oc_wins/total*100:.1f}%)")
        print(f"  BL wins: {bl_wins} ({bl_wins/total*100:.1f}%)")
        print(f"  Ties: {ties} ({ties/total*100:.1f}%)")
    print()

    # Token metrics
    tok = report.get("token_metrics", {})
    if tok:
        print("=== Token Metrics ===")
        print(f"  OC avg tokens: {tok.get('oc_avg_tokens', '?')}")
        print(f"  Baseline avg tokens: {tok.get('baseline_avg_tokens', '?')}")
        print(f"  Reduction: {tok.get('reduction_pct', '?')}%")
        print(f"  Raw baseline avg: {tok.get('raw_baseline_avg_tokens', '?')}")
    print()

    # Latency
    lat = report.get("latency", {})
    if lat:
        print("=== Latency ===")
        print(f"  P50: {lat.get('p50_ms', '?')}ms")
        print(f"  P95: {lat.get('p95_ms', '?')}ms")
        print(f"  Mean: {lat.get('mean_ms', '?')}ms")

    # Knowledge quality metrics (mode=knowledge)
    if mode == "knowledge":
        print("=== Knowledge Quality Metrics ===")
        print(f"  Recall: {acc.get('knowledge_recall', '?')}")
        print(f"  Precision: {acc.get('knowledge_precision', '?')}")
        print(f"  Type Accuracy: {acc.get('type_accuracy', '?')}")
        print(f"  Hallucination Rate: {acc.get('hallucination_rate', '?')}")
        ci = acc.get("recall_ci", {})
        if ci:
            print(f"  Recall 95% CI: [{ci.get('lower')}, {ci.get('upper')}]")
        print()

        # Per-cluster breakdown
        by_cluster = acc.get("by_cluster", {})
        if by_cluster:
            print("=== Per-Cluster Knowledge Quality ===")
            for cid, data in sorted(by_cluster.items()):
                print(f"  {cid}:")
                print(f"    Recall={data.get('recall', 0):.2f} "
                      f"Precision={data.get('precision', 0):.2f} "
                      f"TypeAcc={data.get('type_accuracy', 0):.2f} "
                      f"(expected={data.get('n_expected', 0)} extracted={data.get('n_extracted', 0)})")
            print()

        # Match details
        if pq:
            print("=== Match Details ===")
            for q in pq:
                cid = q.get("cluster_id", "?")
                matches = q.get("matches", [])
                if not matches:
                    continue
                print(f"  {cid}: {len(matches)} matches")
                for m in matches:
                    if m.get("expected_idx", -1) >= 0:
                        print(f"    ✓ matched expected #{m['expected_idx']}: {m.get('expected', '')[:60]}")
                        if not m.get("type_match"):
                            print(f"      ⚠ type mismatch")
                    else:
                        print(f"    ✗ hallucination: {m.get('extracted', '')[:60]}")
            print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python benchmarks/analyze_eval.py <report.json>")
        sys.exit(1)
    analyze(sys.argv[1])
