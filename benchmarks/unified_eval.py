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

_project_root = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _project_root)
sys.path.insert(0, str(Path(_project_root) / "src"))

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
from benchmarks.scoring import (
    exact_match,
    f1_score,
    jscore_judge,
    llm_judge_score,
    score_qa,
    supporting_fact_f1,
)


# ---------------------------------------------------------------------------
# Dataset-adaptive answer prompt templates
# ---------------------------------------------------------------------------
# LoCoMo / default: concise 5-6 word answers (Mem0-aligned)
ANSWER_PROMPT = (
    "Based on the above context, answer in 5-6 words.\n\n"
    "Question: {question}\nAnswer:"
)
ANSWER_PROMPT_CAT5 = (
    "Based on the above context, answer the question.\n\n"
    "Question: {question}\nShort answer:"
)

# HotPotQA: short factoid answers (EM-friendly)
ANSWER_PROMPT_HOTPOTQA = (
    "Based on the above context, answer the question with a short phrase "
    "(a few words). Be precise — use exact names, numbers, or yes/no.\n\n"
    "Question: {question}\nAnswer:"
)

# PersonaMem: preference / attribute answers (longer, natural language)
ANSWER_PROMPT_PERSONAMEM = (
    "Based on the above context about a user's preferences and attributes, "
    "answer the question naturally. Include relevant details.\n\n"
    "Question: {question}\nAnswer:"
)

# QASPER: academic paper QA (varied answer types: yes/no, extractive, free-form)
ANSWER_PROMPT_QASPER = (
    "Based on the above context from a research paper, answer the question. "
    "If it is a yes/no question, answer 'yes' or 'no'. "
    "Otherwise, answer concisely using information from the paper.\n\n"
    "Question: {question}\nAnswer:"
)

_DATASET_PROMPTS: Dict[str, str] = {
    "hotpotqa": ANSWER_PROMPT_HOTPOTQA,
    "personamem": ANSWER_PROMPT_PERSONAMEM,
    "qasper": ANSWER_PROMPT_QASPER,
}


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


def _get_adapter(mode: str, dataset: str = ""):
    """Import and return the adapter for the given mode/dataset.

    Dataset-specific adapters take priority over mode-based defaults.
    """
    # Dataset-specific adapters
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


def _build_prompt(context: str, question: str, category: str = "", dataset: str = "") -> str:
    """Build LLM prompt from retrieved context and question.

    Uses dataset-specific prompt when available, falls back to LoCoMo defaults.
    """
    if dataset in _DATASET_PROMPTS:
        tpl = _DATASET_PROMPTS[dataset]
    elif category == "5":
        tpl = ANSWER_PROMPT_CAT5
    else:
        tpl = ANSWER_PROMPT
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
        adapter = _get_adapter(mode, args.dataset)
        dataset_path = args.data or _default_dataset_path(mode, args.dataset)
        if not dataset_path:
            raise ValueError("--data is required (or use --mode all for default datasets)")
        adapter.load_dataset(dataset_path)

        # Phase 1: Ingest
        if not args.skip_ingest:
            log("Ingesting dataset...")
            ingest_result = await adapter.ingest(oc, max_conv=args.max_conv, max_qa=args.max_qa)
            log(f"  Ingested {ingest_result.ingested_items}/{ingest_result.total_items}"
                f" ({len(ingest_result.errors)} errors)")
            if ingest_result.errors:
                for err in ingest_result.errors[:5]:
                    log(f"  ERROR: {err}")
            await asyncio.sleep(1)  # let embeddings settle

        # Phase 2: Build QA items (max_conv limits QA to ingested conversations only)
        qa_items = adapter.build_qa_items(max_qa=args.max_qa, max_conv=args.max_conv)
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
                results: List[Dict] = []
                if not args.baseline_only:
                    try:
                        results, latency_ms = await adapter.retrieve(oc, qa_item, args.top_k)
                    except Exception as e:
                        log(f"  Retrieve error: {e}")
                        results = []
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
                    oc_prompt = _build_prompt(oc_context, qa_item.question, qa_item.category, args.dataset)
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
                    bl_prompt = _build_prompt(truncated_context, qa_item.question, qa_item.category, args.dataset)
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

                # HotPotQA-specific: EM, SP F1, Joint F1
                if getattr(adapter, "is_hotpotqa", False):
                    if oc_prediction:
                        record["oc_em"] = exact_match(oc_prediction, qa_item.answer)
                    if bl_prediction:
                        record["bl_em"] = exact_match(bl_prediction, qa_item.answer)
                    # SP F1: retrieved titles vs gold titles
                    gold_titles = set(qa_item.meta.get("gold_titles", []))
                    retrieved_titles = {
                        r.get("abstract", "") for r in results
                        if isinstance(r, dict) and r.get("abstract")
                    }
                    sp = supporting_fact_f1(retrieved_titles, gold_titles)
                    record["sp_f1"] = sp
                    if "oc_f1" in record:
                        record["joint_f1"] = record["oc_f1"] * sp

                # LLM-as-Judge (optional, legacy 3-point scale)
                if args.enable_llm_judge:
                    if oc_prediction:
                        record["oc_judge"] = await llm_judge_score(
                            oc_prediction, qa_item.answer, qa_item.question, llm.complete
                        )
                    if bl_prediction:
                        record["bl_judge"] = await llm_judge_score(
                            bl_prediction, qa_item.answer, qa_item.question, llm.complete
                        )

                # J-score (always-on for Cat 1-4, Mem0-aligned binary judge)
                if not args.disable_jscore and cat != 5:
                    if oc_prediction:
                        record["oc_jscore"] = await jscore_judge(
                            oc_prediction, qa_item.answer, qa_item.question, llm.complete
                        )
                    if bl_prediction:
                        record["bl_jscore"] = await jscore_judge(
                            bl_prediction, qa_item.answer, qa_item.question, llm.complete
                        )

                done_count += 1
                if done_count % 25 == 0 or done_count == len(qa_items):
                    log(f"  Progress: {done_count}/{len(qa_items)}")

                return record

        records = await asyncio.gather(
            *[eval_one(item) for item in qa_items], return_exceptions=True,
        )
        # Filter out exceptions, log them
        clean_records = []
        for i, r in enumerate(records):
            if isinstance(r, Exception):
                log(f"  QA #{i} failed: {r}")
                clean_records.append({
                    "question": qa_items[i].question,
                    "answer": qa_items[i].answer,
                    "category": qa_items[i].category,
                    "error": str(r),
                })
            else:
                clean_records.append(r)
        records = clean_records

        # Phase 4: Compute metrics
        # Retrieval
        retrieval_metrics = compute_retrieval_metrics(
            [r for r in records if r.get("retrieved_uris") is not None],
            ks=[1, 3, 5],
        )

        # QA Accuracy — Category 5 (adversarial) excluded from overall per LoCoMo protocol
        EXCLUDE_CATS = {"5"}
        oc_f1s = [r["oc_f1"] for r in records if "oc_f1" in r and r.get("category") not in EXCLUDE_CATS]
        bl_f1s = [r["bl_f1"] for r in records if "bl_f1" in r and r.get("category") not in EXCLUDE_CATS]

        # Per-category F1 (all categories including Cat 5)
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
            accuracy["f1"]["excluded_categories"] = list(EXCLUDE_CATS)
        if bl_f1s:
            bl_overall = sum(bl_f1s) / len(bl_f1s)
            accuracy["baseline_f1"] = round(bl_overall, 4)
            accuracy["baseline_by_category"] = {
                cat: {"f1": round(sum(s) / len(s), 4), "n": len(s)}
                for cat, s in bl_by_cat.items()
            }
            if oc_f1s:
                accuracy["delta_f1"] = f"{oc_overall - bl_overall:+.4f}"

        # HotPotQA aggregate metrics (EM, SP F1, Joint F1)
        oc_ems = [r["oc_em"] for r in records if "oc_em" in r]
        bl_ems = [r["bl_em"] for r in records if "bl_em" in r]
        sp_f1s = [r["sp_f1"] for r in records if "sp_f1" in r]
        joint_f1s = [r["joint_f1"] for r in records if "joint_f1" in r]
        if oc_ems:
            accuracy["oc_em"] = round(sum(oc_ems) / len(oc_ems), 4)
        if bl_ems:
            accuracy["bl_em"] = round(sum(bl_ems) / len(bl_ems), 4)
        if sp_f1s:
            accuracy["sp_f1"] = round(sum(sp_f1s) / len(sp_f1s), 4)
        if joint_f1s:
            accuracy["joint_f1"] = round(sum(joint_f1s) / len(joint_f1s), 4)

        # LLM Judge (legacy)
        if args.enable_llm_judge:
            oc_judges = [r["oc_judge"] for r in records if "oc_judge" in r]
            if oc_judges:
                accuracy["llm_judge"] = {
                    "overall": round(sum(oc_judges) / len(oc_judges), 4),
                }

        # J-score aggregation (Cat 1-4 only, micro-average)
        if not args.disable_jscore:
            oc_jscores_by_cat: Dict[str, List[float]] = {}
            bl_jscores_by_cat: Dict[str, List[float]] = {}
            for r in records:
                cat = r.get("category", "unknown")
                if cat in EXCLUDE_CATS:
                    continue
                if "oc_jscore" in r:
                    oc_jscores_by_cat.setdefault(cat, []).append(r["oc_jscore"])
                if "bl_jscore" in r:
                    bl_jscores_by_cat.setdefault(cat, []).append(r["bl_jscore"])

            all_oc_j = [s for scores in oc_jscores_by_cat.values() for s in scores]
            all_bl_j = [s for scores in bl_jscores_by_cat.values() for s in scores]

            jscore_data: Dict[str, Any] = {}
            if all_oc_j:
                oc_j_overall = sum(all_oc_j) / len(all_oc_j)
                jscore_data["overall"] = round(oc_j_overall, 4)
                jscore_data["by_category"] = {
                    cat: {"jscore": round(sum(s) / len(s), 4), "n": len(s)}
                    for cat, s in oc_jscores_by_cat.items()
                }
            if all_bl_j:
                bl_j_overall = sum(all_bl_j) / len(all_bl_j)
                jscore_data["baseline_overall"] = round(bl_j_overall, 4)
                jscore_data["baseline_by_category"] = {
                    cat: {"jscore": round(sum(s) / len(s), 4), "n": len(s)}
                    for cat, s in bl_jscores_by_cat.items()
                }
                if all_oc_j:
                    jscore_data["delta"] = f"{oc_j_overall - bl_j_overall:+.4f}"
            if jscore_data:
                accuracy["jscore"] = jscore_data

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
            "jscore_enabled": not args.disable_jscore,
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
    p.add_argument("--dataset", default="", help="Dataset name (personamem, locomo, longmemeval, qasper, longbench, cmrc, hotpotqa)")
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
    p.add_argument("--enable-llm-judge", action="store_true", help="Enable legacy LLM-as-Judge scoring (3-point scale)")
    p.add_argument("--disable-jscore", action="store_true", help="Disable J-score (Mem0-aligned binary LLM judge)")

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
