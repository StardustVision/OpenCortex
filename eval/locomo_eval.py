#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
LoCoMo Benchmark Evaluation for OpenCortex

Simulates the real MCP flow:
  - For each session (chronological): recall() at session start, then store
  - For each QA question: recall(question) → LLM answer

Groups:
  A (baseline)   — LLM + full conversation context
  B (opencortex) — LLM + OpenCortex recall-retrieved memories

Results saved to benchmark/ directory.

Usage:
    uv run python eval/locomo_eval.py \\
        --data eval/locomo10.json \\
        --server http://10.46.35.24:18921 \\
        --token <jwt> \\
        --llm-base https://ark.cn-beijing.volces.com/api/v3 \\
        --llm-key <key> \\
        --llm-model ep-xxx \\
        --output benchmark
"""

import argparse
import asyncio
import json
import random
import re
import string
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

# ---------------------------------------------------------------------------
# F1 / Metric helpers (ported from LoCoMo evaluation.py, no extra deps)
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    s = str(s).replace(",", "")
    s = re.sub(r"\b(a|an|the|and)\b", " ", s.lower())
    s = "".join(ch for ch in s if ch not in string.punctuation)
    return " ".join(s.split())


def _f1_tokens(pred: str, gt: str) -> float:
    p_tok = _normalize(pred).split()
    g_tok = _normalize(gt).split()
    common = Counter(p_tok) & Counter(g_tok)
    n = sum(common.values())
    if n == 0:
        return 0.0
    prec = n / len(p_tok)
    rec = n / len(g_tok)
    return (2 * prec * rec) / (prec + rec)


def _f1_multi(pred: str, gt: str) -> float:
    """Multi-answer F1 for single-hop: each sub-answer comma-separated."""
    preds = [p.strip() for p in pred.split(",")]
    gts   = [g.strip() for g in str(gt).split(",")]
    return sum(max(_f1_tokens(p, g) for p in preds) for g in gts) / len(gts)


def score_qa(prediction: str, answer: Any, category: int) -> float:
    """Return F1 score for a single QA pair."""
    pred = str(prediction).strip()
    ans  = str(answer).strip()

    if category == 5:  # adversarial — check if model refuses
        low = pred.lower()
        return 1.0 if ("no information" in low or "not mentioned" in low) else 0.0

    if category == 3:  # commonsense — use first alternative
        ans = ans.split(";")[0].strip()

    if category == 1:  # single-hop (multi-answer)
        return _f1_multi(pred, ans)

    return _f1_tokens(pred, ans)  # categories 2 (temporal), 4 (multi-hop)


CAT_NAMES = {
    1: "single-hop",
    2: "temporal",
    3: "commonsense",
    4: "multi-hop",
    5: "adversarial",
}


def aggregate(results: List[Dict]) -> Dict:
    by_cat: Dict[int, List[float]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["f1"])
    all_f1 = [r["f1"] for r in results]
    out: Dict[str, Any] = {
        "total": len(results),
        "overall_f1": round(sum(all_f1) / len(all_f1), 4) if all_f1 else 0.0,
    }
    for cat in sorted(by_cat):
        scores = by_cat[cat]
        out[f"cat{cat}_{CAT_NAMES[cat]}"] = {
            "f1": round(sum(scores) / len(scores), 4),
            "n": len(scores),
        }
    return out


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def parse_sessions(conv: Dict) -> List[Dict]:
    """Return sessions sorted chronologically."""
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


def fmt_session(session: Dict) -> str:
    lines = [f"[{session['date_time']}]"]
    for t in session["turns"]:
        text = t["text"]
        if "blip_caption" in t:
            text += f" [image: {t['blip_caption']}]"
        lines.append(f"{t['speaker']}: {text}")
    return "\n".join(lines)


def fmt_full_conv(sessions: List[Dict], speakers: List[str]) -> str:
    header = f"Conversation between {' and '.join(speakers)} over multiple sessions.\n\n"
    return header + "\n\n".join(fmt_session(s) for s in sessions)


def get_qa_answer(qa: Dict) -> str:
    """Return the answer string for a QA item.
    Category 5 (adversarial) may use 'adversarial_answer' instead of 'answer'.
    """
    if "answer" in qa:
        return str(qa["answer"])
    if "adversarial_answer" in qa:
        return str(qa["adversarial_answer"])
    return ""


# Max chars for full conversation context (to avoid LLM 400 token limit errors)
_MAX_CTX_CHARS = 30_000


def get_speakers(sessions: List[Dict]) -> List[str]:
    seen: Dict[str, int] = {}
    for s in sessions:
        for t in s["turns"]:
            seen[t["speaker"]] = seen.get(t["speaker"], 0) + 1
    return sorted(seen, key=lambda x: -seen[x])


def _default_run_name(model: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", model.strip()).strip("-")
    return cleaned or "locomo-run"


def _is_retryable_http_error(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return False


# ---------------------------------------------------------------------------
# LLM client (OpenAI-compatible)
# ---------------------------------------------------------------------------

ANSWER_PROMPT = (
    "Based on the above context, answer with a short phrase. "
    "Use exact words from the context when possible.\n\n"
    "Question: {question}\nShort answer:"
)
ANSWER_PROMPT_CAT5 = (
    "Based on the above context, answer the question.\n\n"
    "Question: {question}\nShort answer:"
)


class LLMClient:
    def __init__(
        self,
        base: str,
        key: str,
        model: str,
        timeout: float = 60.0,
        api_style: str = "auto",
    ):
        self._base  = base.rstrip("/")
        self._key   = key
        self._model = model
        self._api_style = self._resolve_api_style(api_style)
        self._client = httpx.AsyncClient(timeout=timeout)

    async def complete(self, prompt: str, max_tokens: int = 64) -> str:
        url = self._build_request_url()
        payload = self._build_payload(prompt, max_tokens)
        resp = await self._client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {self._key}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return self._extract_text(data)

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
        if self._api_style == "anthropic":
            return {
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            }
        return {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }

    def _extract_text(self, data: Dict[str, Any]) -> str:
        if self._api_style == "anthropic":
            content = data.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return str(block.get("text", "")).strip()
                if content and isinstance(content[0], dict):
                    return str(content[0].get("text", "")).strip()
            raise KeyError("Anthropic response missing content text")

        choices = data.get("choices", [])
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message", {})
            if isinstance(message, dict):
                return str(message.get("content", "")).strip()
        raise KeyError("OpenAI-compatible response missing choices[0].message.content")


# ---------------------------------------------------------------------------
# OpenCortex HTTP helper
# ---------------------------------------------------------------------------

class OCClient:
    def __init__(
        self,
        base: str,
        token: str,
        timeout: float = 120.0,
        retries: int = 3,
        retry_delay: float = 2.0,
    ):
        self._base   = base.rstrip("/")
        self._hdrs   = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(timeout=timeout)
        self._retries = retries
        self._retry_delay = retry_delay

    async def close(self):
        await self._client.aclose()

    async def store(self, abstract: str, content: str, category: str) -> Dict:
        last_error: Optional[Exception] = None
        for attempt in range(1, self._retries + 1):
            try:
                r = await self._client.post(
                    f"{self._base}/api/v1/memory/store",
                    json={
                        "abstract": abstract,
                        "content": content,
                        "category": category,
                        "context_type": "memory",
                        "dedup": False,
                    },
                    headers=self._hdrs,
                )
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

    async def search(self, query: str, limit: int, category: str) -> List[Dict]:
        last_error: Optional[Exception] = None
        for attempt in range(1, self._retries + 1):
            try:
                r = await self._client.post(
                    f"{self._base}/api/v1/memory/search",
                    json={
                        "query": query,
                        "limit": limit,
                        "detail_level": "l2",
                        "category": category,
                    },
                    headers=self._hdrs,
                )
                r.raise_for_status()
                return r.json().get("results", [])
            except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= self._retries or not _is_retryable_http_error(exc):
                    raise
                await asyncio.sleep(self._retry_delay * attempt)
        if last_error:
            raise last_error
        return []


# ---------------------------------------------------------------------------
# Ingestion — per session (with recall simulation at session start)
# ---------------------------------------------------------------------------

async def ingest_conversation(
    oc: OCClient,
    sessions: List[Dict],
    conv_id: str,
    log,
) -> List[Dict]:
    """
    Simulate MCP flow: for each session
      1. recall(session_start_text) ← what the agent would see at session start
      2. memory_store(full_session_text) ← agent stores the session

    Returns list of {session_num, recalled_at_start, stored_abstract}.
    """
    category = f"locomo_{conv_id}"
    ingestion_log = []

    for i, session in enumerate(sessions):
        date    = session["date_time"]
        turns   = session["turns"]
        session_text = fmt_session(session)

        # 1. Simulate recall at session start (what past context is retrieved)
        first_turn = turns[0]["text"][:120] if turns else ""
        recall_query = f"{date} {first_turn}"
        try:
            recalled = await oc.search(recall_query, limit=3, category=category)
        except Exception:
            recalled = []  # warm-up timeout on first call — non-fatal

        # 2. Build abstract: date + speakers + opening line
        speakers = list(dict.fromkeys(t["speaker"] for t in turns))
        abstract = f"{date} — {', '.join(speakers)}: {first_turn}"

        # 3. Store session as memory
        await oc.store(abstract=abstract, content=session_text, category=category)

        ingestion_log.append({
            "session_num": session["session_num"],
            "date_time": date,
            "recalled_at_start": len(recalled),
            "stored_abstract": abstract,
        })

        if (i + 1) % 10 == 0 or (i + 1) == len(sessions):
            log(f"  [{conv_id}] Ingested {i+1}/{len(sessions)} sessions")

    return ingestion_log


# ---------------------------------------------------------------------------
# QA evaluation — OpenCortex group
# ---------------------------------------------------------------------------

async def eval_oc(
    oc: OCClient,
    llm: LLMClient,
    qa_list: List[Dict],
    conv_id: str,
    top_k: int,
    rng: random.Random,
    delay: float,
    log,
) -> List[Dict]:
    category = f"locomo_{conv_id}"
    results  = []

    for i, qa in enumerate(qa_list):
        cat      = qa["category"]
        question = qa["question"]
        answer   = get_qa_answer(qa)

        # Cat-2 hint; cat-5 option pair
        if cat == 2:
            q_text = question + " Use dates from the conversation."
        elif cat == 5:
            opt_a = answer
            opt_b = "Not mentioned in the conversation"
            if rng.random() < 0.5:
                opt_a, opt_b = opt_b, opt_a
            q_text = f"{question} Choose: (a) {opt_a} (b) {opt_b}."
        else:
            q_text = question

        # Simulate recall(question) at the start of this "turn"
        memories = await oc.search(question, limit=top_k, category=category)

        # Build context from retrieved memories
        ctx_parts = []
        for m in memories:
            ctx_parts.append(m.get("content") or m.get("overview") or m.get("abstract", ""))
        context = "\n\n---\n\n".join(ctx_parts) if ctx_parts else "(no relevant memories found)"

        prompt_tpl = ANSWER_PROMPT_CAT5 if cat == 5 else ANSWER_PROMPT
        prompt = f"Relevant memories:\n{context}\n\n{prompt_tpl.format(question=q_text)}"

        try:
            prediction = await llm.complete(prompt)
        except Exception as e:
            log(f"  [OC] LLM error Q{i}: {e}")
            prediction = ""

        f1 = score_qa(prediction, answer, cat)
        results.append({
            "question": question,
            "answer": str(answer),
            "prediction": prediction,
            "category": cat,
            "f1": round(f1, 4),
            "num_memories_retrieved": len(memories),
        })

        if delay:
            await asyncio.sleep(delay)
        if (i + 1) % 25 == 0:
            log(f"  [OC] {conv_id}: {i+1}/{len(qa_list)} QA done")

    return results


# ---------------------------------------------------------------------------
# QA evaluation — Baseline group
# ---------------------------------------------------------------------------

async def eval_baseline(
    llm: LLMClient,
    qa_list: List[Dict],
    sessions: List[Dict],
    rng: random.Random,
    delay: float,
    log,
) -> List[Dict]:
    speakers = get_speakers(sessions)
    full_ctx = fmt_full_conv(sessions, speakers)[:_MAX_CTX_CHARS]
    results  = []

    for i, qa in enumerate(qa_list):
        cat      = qa["category"]
        question = qa["question"]
        answer   = get_qa_answer(qa)

        if cat == 2:
            q_text = question + " Use dates from the conversation."
        elif cat == 5:
            opt_a = str(answer)
            opt_b = "Not mentioned in the conversation"
            if rng.random() < 0.5:
                opt_a, opt_b = opt_b, opt_a
            q_text = f"{question} Choose: (a) {opt_a} (b) {opt_b}."
        else:
            q_text = question

        prompt_tpl = ANSWER_PROMPT_CAT5 if cat == 5 else ANSWER_PROMPT
        prompt = f"{full_ctx}\n\n{prompt_tpl.format(question=q_text)}"

        try:
            prediction = await llm.complete(prompt)
        except Exception as e:
            log(f"  [BL] LLM error Q{i}: {e}")
            prediction = ""

        f1 = score_qa(prediction, answer, cat)
        results.append({
            "question": question,
            "answer": str(answer),
            "prediction": prediction,
            "category": cat,
            "f1": round(f1, 4),
        })

        if delay:
            await asyncio.sleep(delay)
        if (i + 1) % 25 == 0:
            log(f"  [BL] {i+1}/{len(qa_list)} QA done")

    return results


# ---------------------------------------------------------------------------
# Pretty-print comparison table
# ---------------------------------------------------------------------------

def print_comparison(oc_metrics: Optional[Dict], bl_metrics: Optional[Dict]):
    header = f"{'Category':<22} {'Baseline':>10} {'OpenCortex':>12} {'Delta':>8}"
    print("\n" + "=" * 56)
    print(header)
    print("-" * 56)

    cats = [(1, "single-hop"), (2, "temporal"), (3, "commonsense"),
            (4, "multi-hop"), (5, "adversarial")]

    for cat, name in cats:
        key = f"cat{cat}_{name}"
        bl_f1 = bl_metrics.get(key, {}).get("f1", "-") if bl_metrics else "-"
        oc_f1 = oc_metrics.get(key, {}).get("f1", "-") if oc_metrics else "-"
        n     = (oc_metrics or bl_metrics or {}).get(key, {}).get("n", "")
        label = f"{name} (n={n})"
        if isinstance(bl_f1, float) and isinstance(oc_f1, float):
            delta = f"{oc_f1 - bl_f1:+.4f}"
        else:
            delta = "-"
        print(f"{label:<22} {str(bl_f1):>10} {str(oc_f1):>12} {delta:>8}")

    print("-" * 56)
    bl_o = bl_metrics.get("overall_f1", "-") if bl_metrics else "-"
    oc_o = oc_metrics.get("overall_f1", "-") if oc_metrics else "-"
    if isinstance(bl_o, float) and isinstance(oc_o, float):
        delta = f"{oc_o - bl_o:+.4f}"
    else:
        delta = "-"
    print(f"{'Overall':<22} {str(bl_o):>10} {str(oc_o):>12} {delta:>8}")
    print("=" * 56)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _default_run_name(model: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "-", model).strip("-")


async def run(args):
    run_name = args.run_name or _default_run_name(args.llm_model)
    out_dir = Path(args.output) / run_name
    out_dir.mkdir(parents=True, exist_ok=True)

    def log(msg):
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)

    log(f"Loading {args.data}")
    data = json.loads(Path(args.data).read_text())
    log(f"Loaded {len(data)} conversations")

    # Select conversations
    indices = list(range(len(data)))
    if args.conversations:
        indices = [int(x) for x in args.conversations.split(",")]
    if args.max_conv:
        indices = indices[: args.max_conv]

    llm = LLMClient(
        args.llm_base,
        args.llm_key,
        args.llm_model,
        api_style=args.llm_api_style,
    )
    oc  = OCClient(args.server, args.token) if not args.baseline_only else None

    all_oc_results: List[Dict] = []
    all_bl_results: List[Dict] = []
    conv_summaries: List[Dict] = []

    rng = random.Random(args.seed)

    for idx in indices:
        conv    = data[idx]
        conv_id = conv.get("sample_id", str(idx))
        sessions = parse_sessions(conv)
        qa_list  = conv.get("qa", [])
        if args.max_qa:
            qa_list = qa_list[: args.max_qa]

        log(f"\n{'='*60}")
        log(f"Conv {conv_id}: {len(sessions)} sessions, {len(qa_list)} QA")

        ingestion_log = []
        # --- Ingest ---
        if oc and not args.skip_ingest:
            log("Ingesting sessions (with recall simulation at each session start)...")
            ingestion_log = await ingest_conversation(oc, sessions, conv_id, log)
            await asyncio.sleep(1)  # let embeddings settle

        # --- OpenCortex QA ---
        oc_results: List[Dict] = []
        if oc:
            log(f"Evaluating OpenCortex (top-k={args.top_k})...")
            oc_results = await eval_oc(
                oc, llm, qa_list, conv_id, args.top_k, rng, args.delay, log
            )
            all_oc_results.extend(oc_results)

        # --- Baseline QA ---
        bl_results: List[Dict] = []
        if not args.oc_only:
            log("Evaluating baseline (full context)...")
            bl_results = await eval_baseline(llm, qa_list, sessions, rng, args.delay, log)
            all_bl_results.extend(bl_results)

        # --- Per-conv result ---
        conv_out: Dict[str, Any] = {
            "conv_id": conv_id,
            "num_sessions": len(sessions),
            "num_qa": len(qa_list),
            "ingestion_log": ingestion_log,
        }
        if oc_results:
            conv_out["opencortex"] = {
                "metrics": aggregate(oc_results),
                "qa": oc_results,
            }
        if bl_results:
            conv_out["baseline"] = {
                "metrics": aggregate(bl_results),
                "qa": bl_results,
            }

        conv_file = out_dir / f"{conv_id}.json"
        conv_out["result_file"] = conv_file.name
        conv_file.write_text(json.dumps(conv_out, indent=2, ensure_ascii=False))
        log(f"Saved {conv_file}")

        oc_f1 = conv_out.get("opencortex", {}).get("metrics", {}).get("overall_f1", "-")
        bl_f1 = conv_out.get("baseline",   {}).get("metrics", {}).get("overall_f1", "-")
        log(f"  OC={oc_f1}  BL={bl_f1}")
        conv_summaries.append({
            "conv_id": conv_id,
            "result_file": conv_file.name,
            "oc_f1": oc_f1,
            "bl_f1": bl_f1,
        })

    # --- Global summary ---
    summary: Dict[str, Any] = {
        "config": {
            "data": args.data,
            "server": args.server,
            "llm_model": args.llm_model,
            "llm_api_style": llm._api_style,
            "top_k": args.top_k,
            "seed": args.seed,
            "run_name": run_name,
            "output_dir": str(out_dir),
            "conversations_evaluated": indices,
        },
        "conversations": conv_summaries,
    }
    if all_oc_results:
        summary["opencortex"] = aggregate(all_oc_results)
    if all_bl_results:
        summary["baseline"] = aggregate(all_bl_results)

    summary_file = out_dir / "summary.json"
    summary_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    log(f"\nSummary saved to {summary_file}")

    print_comparison(
        summary.get("opencortex"),
        summary.get("baseline"),
    )

    await llm.close()
    if oc:
        await oc.close()


def main():
    p = argparse.ArgumentParser(description="LoCoMo benchmark eval for OpenCortex")

    # Data
    p.add_argument("--data",    required=True,  help="Path to locomo10.json")
    p.add_argument("--output",  default="benchmark", help="Output directory (default: benchmark/)")

    # OpenCortex
    p.add_argument("--server",  default="http://127.0.0.1:8921")
    p.add_argument("--token",   default="",     help="JWT Bearer token")
    p.add_argument("--top-k",   type=int, default=5, help="Memories to retrieve per question")
    p.add_argument("--skip-ingest", action="store_true", help="Skip ingestion (reuse existing memories)")

    # LLM
    p.add_argument("--llm-base",  required=True,  help="OpenAI-compatible API base URL")
    p.add_argument("--llm-key",   required=True,  help="LLM API key")
    p.add_argument("--llm-model", required=True,  help="LLM model name/endpoint")
    p.add_argument(
        "--llm-api-style",
        default="auto",
        choices=["auto", "openai", "anthropic"],
        help="LLM API response style (default: auto)",
    )
    p.add_argument("--delay",   type=float, default=0.2, help="Seconds between LLM calls (rate limiting)")
    p.add_argument(
        "--run-name",
        default="",
        help="Benchmark output subdirectory name (default: derived from llm model)",
    )

    # Run control
    p.add_argument("--conversations", default="", help="Comma-separated conv indices (default: all)")
    p.add_argument("--max-conv",  type=int, default=0,  help="Max conversations to evaluate")
    p.add_argument("--max-qa",   type=int, default=0,   help="Max QA per conversation (for quick tests)")
    p.add_argument("--baseline-only", action="store_true")
    p.add_argument("--oc-only",       action="store_true")
    p.add_argument("--seed", type=int, default=42, help="Random seed for cat-5 option ordering")

    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
