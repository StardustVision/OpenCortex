#!/bin/bash
# OpenCortex Full Benchmark Suite — GPT-4o-mini, recall only
# Production path (context_recall) only. No search comparison.
# Each run creates isolated bench_ collection + eval_* CortexFS tenant.
# Cleanup runs after each test to prevent disk bloat.
#
# Usage: bash benchmarks/run_full_eval.sh

set -e

SERVER="http://127.0.0.1:8921"
LLM_BASE="http://sub.netdevops.lenovo.com/v1"
LLM_KEY="sk-8083330eca581a5069c5437e7ec0963dd63b12251ab7a9acb9abe50140d39fb8"
LLM_MODEL="gpt-4o-mini"
CONCURRENCY=3
DATA_ROOT="${DATA_ROOT:-./data}"

cleanup_eval_data() {
    echo "  [cleanup] Removing eval CortexFS tenants..."
    local count=0
    for d in "${DATA_ROOT}"/eval_*; do
        if [ -d "$d" ]; then
            du -sh "$d" 2>/dev/null
            rm -rf "$d"
            count=$((count + 1))
        fi
    done
    echo "  [cleanup] Removed $count eval tenant dirs"

    echo "  [cleanup] Checking for stale Qdrant collections..."
    local qdrant_dir="${DATA_ROOT}/qdrant_storage/collections"
    if [ -d "$qdrant_dir" ]; then
        for d in "${qdrant_dir}"/bench_*; do
            if [ -d "$d" ]; then
                echo "    Removing stale collection: $(basename "$d")"
                rm -rf "$d"
            fi
        done
    fi

    echo "  [cleanup] Done. Disk usage: $(du -sh "${DATA_ROOT}" 2>/dev/null | cut -f1)"
}

run() {
    local label="$1"
    shift
    echo ""
    echo "================================================================"
    echo "  [$(date '+%Y-%m-%d %H:%M:%S')] START: $label"
    echo "================================================================"
    uv run python benchmarks/unified_eval.py "$@"
    local rc=$?
    echo "  [$(date '+%Y-%m-%d %H:%M:%S')] DONE: $label (exit=$rc)"

    cleanup_eval_data

    return $rc
}

# ---- 1. LoCoMo (conversation) ----
run "LoCoMo 1986 QA — recall" \
    --mode conversation \
    --data benchmarks/locomo10.json \
    --server "$SERVER" \
    --llm-base "$LLM_BASE" --llm-key "$LLM_KEY" --llm-model "$LLM_MODEL" \
    --retrieve-method recall \
    --concurrency "$CONCURRENCY"

# ---- 2. PersonaMem (memory) ----
run "PersonaMem 2061 QA — recall" \
    --mode memory \
    --data benchmarks/datasets/personamem/data.json \
    --server "$SERVER" \
    --llm-base "$LLM_BASE" --llm-key "$LLM_KEY" --llm-model "$LLM_MODEL" \
    --retrieve-method recall \
    --concurrency "$CONCURRENCY"

# ---- 3. QASPER (document) ----
run "QASPER 1005 QA — recall" \
    --mode document \
    --data benchmarks/datasets/qasper/qasper-dev-v0.2.json \
    --server "$SERVER" \
    --llm-base "$LLM_BASE" --llm-key "$LLM_KEY" --llm-model "$LLM_MODEL" \
    --retrieve-method recall \
    --concurrency "$CONCURRENCY"

# ---- 4. HotPotQA (document) ----
run "HotPotQA 7405 QA — recall" \
    --mode document \
    --dataset hotpotqa \
    --data benchmarks/datasets/hotpotqa/hotpot_dev_distractor_v1.json \
    --server "$SERVER" \
    --llm-base "$LLM_BASE" --llm-key "$LLM_KEY" --llm-model "$LLM_MODEL" \
    --retrieve-method recall \
    --concurrency "$CONCURRENCY"

# ---- 5. LongMemEval (conversation) ----
run "LongMemEval 500 QA — recall" \
    --mode conversation \
    --data benchmarks/datasets/longmemeval/longmemeval_s_cleaned.json \
    --server "$SERVER" \
    --llm-base "$LLM_BASE" --llm-key "$LLM_KEY" --llm-model "$LLM_MODEL" \
    --retrieve-method recall \
    --concurrency "$CONCURRENCY"

echo ""
echo "================================================================"
echo "  ALL BENCHMARKS COMPLETE — $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================================"
echo "  Reports saved to: docs/benchmark/"
