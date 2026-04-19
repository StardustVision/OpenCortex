"""
Unified evaluation scoring: F1 token overlap, BLEU-1, NDCG, LLM-as-Judge.

Migrated from benchmarks/locomo_eval.py with generalized category handling.
"""

import json as _json
import math
import re
import string
from collections import Counter
from typing import Any, List, Sequence


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


def exact_match(prediction: str, ground_truth: str) -> float:
    """Normalized exact match (standard HotPotQA metric)."""
    return 1.0 if _normalize(prediction) == _normalize(ground_truth) else 0.0


def supporting_fact_f1(retrieved_titles: set, gold_titles: set) -> float:
    """Set-based F1 over document titles (supporting fact retrieval quality)."""
    if not gold_titles:
        return 0.0
    if not retrieved_titles:
        return 0.0
    common = retrieved_titles & gold_titles
    if not common:
        return 0.0
    prec = len(common) / len(retrieved_titles)
    rec = len(common) / len(gold_titles)
    return (2 * prec * rec) / (prec + rec)


# ---------------------------------------------------------------------------
# BLEU-1 (LoCoMo official generation metric)
# ---------------------------------------------------------------------------

def bleu1_score(prediction: str, ground_truth: str) -> float:
    """BLEU-1 (unigram precision) between prediction and ground truth.

    Used as official metric in LoCoMo benchmark for response generation quality.
    """
    pred_tokens = _normalize(prediction).split()
    gt_tokens = _normalize(ground_truth).split()
    if not pred_tokens or not gt_tokens:
        return 0.0
    gt_counts = Counter(gt_tokens)
    clipped = 0
    for tok in pred_tokens:
        if gt_counts.get(tok, 0) > 0:
            clipped += 1
            gt_counts[tok] -= 1
    # Brevity penalty
    bp = 1.0 if len(pred_tokens) >= len(gt_tokens) else math.exp(1 - len(gt_tokens) / len(pred_tokens))
    return bp * clipped / len(pred_tokens)


# ---------------------------------------------------------------------------
# NDCG@k (Normalized Discounted Cumulative Gain)
# ---------------------------------------------------------------------------

def ndcg_from_relevances(relevances: Sequence[float], k: int) -> float:
    """Compute NDCG@k from a list of relevance scores (ordered by rank).

    Args:
        relevances: relevance score per retrieved item (1.0 = relevant, 0.0 = not).
        k: cutoff rank.
    """
    relevances = list(relevances[:k])
    if not relevances:
        return 0.0

    def _dcg(scores: List[float]) -> float:
        return sum(s / math.log2(i + 2) for i, s in enumerate(scores))

    actual = _dcg(relevances)
    ideal = _dcg(sorted(relevances, reverse=True))
    return actual / ideal if ideal > 0 else 0.0


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
# J-Score (LLM-as-Judge, industry-aligned)
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

# Type-specific prompts matching LongMemEval official evaluation protocol.

_JSCORE_TEMPORAL_SYSTEM = (
    "You are an evaluation judge. Given a question, ground truth answer, and "
    "a predicted answer, determine if the prediction is correct.\n"
    "Do not penalize off-by-one errors for the number of days. If the question "
    "asks for the number of days/weeks/months, and the model makes off-by-one "
    "errors (e.g., predicting 19 days when the answer is 18), the response is "
    "still correct.\n"
    'Output JSON: {"label": "CORRECT"} or {"label": "WRONG"}'
)

_JSCORE_KNOWLEDGE_UPDATE_SYSTEM = (
    "You are an evaluation judge. Given a question, ground truth answer, and "
    "a predicted answer, determine if the prediction is correct.\n"
    "If the response contains some previous information along with an updated "
    "answer, the response should be considered correct as long as the updated "
    "answer is the required answer.\n"
    'Output JSON: {"label": "CORRECT"} or {"label": "WRONG"}'
)

_JSCORE_PREFERENCE_USER = (
    "Question: {question}\n"
    "Rubric: {ground_truth}\n"
    "Prediction: {prediction}\n\n"
    "Does the prediction correctly recall and utilize the user's personal "
    "information? The prediction does not need to reflect all points in the "
    "rubric — it is correct as long as it recalls and utilizes the user's "
    "personal information correctly.\n"
    'Output JSON: {"label": "CORRECT"} or {"label": "WRONG"}'
)

_JSCORE_ABSTENTION_SYSTEM = (
    "You are an evaluation judge. Given an unanswerable question, an "
    "explanation of why it is unanswerable, and a model's response, determine "
    "if the model correctly identifies the question as unanswerable.\n"
    "The model could say that the information is incomplete, or that some "
    "other information is given but the asked information is not.\n"
    'Output JSON: {"label": "CORRECT"} or {"label": "WRONG"}'
)

_JSCORE_ABSTENTION_USER = (
    "Question (unanswerable): {question}\n"
    "Explanation: {ground_truth}\n"
    "Prediction: {prediction}\n\n"
    "Does the model correctly identify the question as unanswerable?\n"
    'Output JSON: {"label": "CORRECT"} or {"label": "WRONG"}'
)

# Question types that use type-specific judge prompts.
_TEMPORAL_TYPES = {"temporal-reasoning", "Temporal Reasoning"}
_KNOWLEDGE_UPDATE_TYPES = {"knowledge-update", "Knowledge Update"}
_PREFERENCE_TYPES = {"single-session-preference", "Single-Session (Preference)"}
_ABSTENTION_TYPES = {"abstention"}


def _is_abstention(question_id: str) -> bool:
    """Check if a question is an abstention (unanswerable) variant."""
    return str(question_id).endswith("_abs")


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


def _select_judge_prompts(
    question_type: str,
    question_id: str = "",
) -> tuple:
    """Select system/user prompt pair based on question type.

    Returns (system_prompt, user_prompt_template) where user_prompt_template
    uses {question}, {ground_truth}, {prediction} placeholders.
    """
    if _is_abstention(question_id) or question_type in _ABSTENTION_TYPES:
        return _JSCORE_ABSTENTION_SYSTEM, _JSCORE_ABSTENTION_USER
    if question_type in _TEMPORAL_TYPES:
        return _JSCORE_TEMPORAL_SYSTEM, JSCORE_USER
    if question_type in _KNOWLEDGE_UPDATE_TYPES:
        return _JSCORE_KNOWLEDGE_UPDATE_SYSTEM, JSCORE_USER
    if question_type in _PREFERENCE_TYPES:
        return JSCORE_SYSTEM, _JSCORE_PREFERENCE_USER
    return JSCORE_SYSTEM, JSCORE_USER


async def jscore_judge(
    prediction: str,
    ground_truth: str,
    question: str,
    llm_complete_fn,
    *,
    question_type: str = "",
    question_id: str = "",
) -> float:
    """Binary LLM-as-Judge score aligned with industry standard.

    Returns 1.0 (CORRECT) or 0.0 (WRONG). Routes to type-specific prompts
    for LongMemEval question types (temporal, knowledge-update, preference,
    abstention). Falls back to generic prompt for LoCoMo and other datasets.

    Args:
        prediction: Model's predicted answer.
        ground_truth: Gold-standard answer.
        question: The question text.
        llm_complete_fn: async callable(prompt, max_tokens, *, system, temperature) -> str
        question_type: LongMemEval question type for prompt selection.
        question_id: Question ID (checked for _abs suffix for abstention).
    """
    system_prompt, user_template = _select_judge_prompts(question_type, question_id)
    user_prompt = user_template.format(
        question=question,
        ground_truth=ground_truth,
        prediction=prediction,
    )
    response = await llm_complete_fn(
        user_prompt, 512, system=system_prompt, temperature=0
    )
    return _parse_jscore_label(response)
