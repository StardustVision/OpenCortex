"""
Unified evaluation scoring: F1 token overlap + LLM-as-Judge.

Migrated from benchmarks/locomo_eval.py with generalized category handling.
"""

import json as _json
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


# ---------------------------------------------------------------------------
# J-Score (Mem0-aligned binary LLM-as-Judge)
# ---------------------------------------------------------------------------

JSCORE_SYSTEM = (
    "You are an evaluation judge. Given a question, ground truth answer, and "
    "a predicted answer, determine if the prediction is correct.\n"
    'Output JSON: {"label": "CORRECT"} or {"label": "WRONG"}\n'
    "Be generous — if the prediction captures the key fact, mark CORRECT."
)

JSCORE_USER = (
    "Question: {question}\n"
    "Ground truth: {ground_truth}\n"
    "Prediction: {prediction}\n\n"
    "Output only JSON."
)


def _parse_jscore_label(response: str) -> float:
    """Parse {"label": "CORRECT"} → 1.0, anything else → 0.0."""
    text = response.strip()
    # Try JSON parse first
    try:
        obj = _json.loads(text)
        if isinstance(obj, dict) and str(obj.get("label", "")).upper() == "CORRECT":
            return 1.0
        return 0.0
    except (_json.JSONDecodeError, ValueError):
        pass
    # Regex fallback
    upper = text.upper()
    if "CORRECT" in upper and "WRONG" not in upper:
        return 1.0
    return 0.0


async def jscore_judge(
    prediction: str,
    ground_truth: str,
    question: str,
    llm_complete_fn,
) -> float:
    """Mem0-aligned binary J-score. Returns 1.0 (CORRECT) or 0.0 (WRONG).

    Args:
        llm_complete_fn: async callable(prompt, max_tokens, *, system, temperature) -> str
    """
    user_prompt = JSCORE_USER.format(
        question=question,
        ground_truth=ground_truth,
        prediction=prediction,
    )
    response = await llm_complete_fn(user_prompt, 32, system=JSCORE_SYSTEM, temperature=0)
    return _parse_jscore_label(response)
