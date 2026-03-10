"""
Sandbox Evaluator — validates knowledge candidates before approval.

Three-stage pipeline:
  1. Statistical gate: trace count, success rate, user diversity
  2. LLM simulation verification
  3. Human approval flow

Design doc §5.4, §10.3.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    """Result from the statistical gate."""
    passed: bool
    reason: str
    trace_count: int = 0
    success_rate: float = 0.0
    unique_users: int = 0


def stat_gate(
    knowledge_dict: Dict[str, Any],
    traces: List[Dict[str, Any]],
    min_traces: int = 3,
    min_success_rate: float = 0.7,
    min_source_users: int = 2,
    min_source_users_private: int = 1,
) -> GateResult:
    """
    Statistical gate for knowledge validation.

    Args:
        knowledge_dict: Knowledge item as dict (needs 'scope' field)
        traces: List of trace dicts (need 'outcome' and 'user_id' fields)
        min_traces: Minimum supporting traces required
        min_success_rate: Minimum success rate among traces
        min_source_users: Min distinct users for tenant/global scope
        min_source_users_private: Min distinct users for user scope

    Returns:
        GateResult with passed/failed and reason
    """
    trace_count = len(traces)

    if trace_count < min_traces:
        return GateResult(
            passed=False,
            reason=f"Insufficient traces: {trace_count} < {min_traces}",
            trace_count=trace_count,
        )

    # Calculate success rate
    success_count = sum(
        1 for t in traces
        if t.get("outcome") == "success"
    )
    success_rate = success_count / trace_count if trace_count > 0 else 0.0

    if success_rate < min_success_rate:
        return GateResult(
            passed=False,
            reason=f"Low success rate: {success_rate:.2f} < {min_success_rate}",
            trace_count=trace_count,
            success_rate=success_rate,
        )

    # Check user diversity
    unique_users = len(set(t.get("user_id", "") for t in traces))
    scope = knowledge_dict.get("scope", "user")
    required_users = min_source_users_private if scope == "user" else min_source_users

    if unique_users < required_users:
        return GateResult(
            passed=False,
            reason=f"Insufficient user diversity: {unique_users} < {required_users}",
            trace_count=trace_count,
            success_rate=success_rate,
            unique_users=unique_users,
        )

    return GateResult(
        passed=True,
        reason="Statistical gate passed",
        trace_count=trace_count,
        success_rate=success_rate,
        unique_users=unique_users,
    )


# ---------------------------------------------------------------------------
# Stage 2: LLM Simulation Verification
# ---------------------------------------------------------------------------

_LLM_VERIFY_PROMPT = """You are evaluating whether a knowledge item would have improved
the outcome of a historical task trace.

Knowledge item:
Type: {knowledge_type}
Statement: {statement}
Objective: {objective}
Action steps: {action_steps}

Historical trace summary:
{trace_summary}

Question: If the agent had applied this knowledge during the trace above,
would the outcome have improved? Answer with a JSON object:
{{"improved": true/false, "reason": "brief explanation"}}"""


@dataclass
class LLMVerifyResult:
    """Result from LLM simulation verification."""
    passed: bool
    pass_rate: float
    reasons: List[str] = field(default_factory=list)
    sample_size: int = 0


async def llm_verify(
    knowledge_dict: Dict[str, Any],
    sample_traces: List[Dict[str, Any]],
    llm_fn: Callable[..., Coroutine],
    min_pass_rate: float = 0.6,
) -> LLMVerifyResult:
    """
    LLM simulation verification — ask LLM if knowledge would help historical traces.

    Args:
        knowledge_dict: Knowledge item as dict
        sample_traces: Sample of historical traces to evaluate against
        llm_fn: Async LLM completion function (prompt -> response string)
        min_pass_rate: Minimum fraction of traces that must show improvement

    Returns:
        LLMVerifyResult with pass/fail and reasons
    """
    if not sample_traces:
        return LLMVerifyResult(
            passed=False, pass_rate=0.0,
            reasons=["No traces to verify against"],
            sample_size=0,
        )

    improved_count = 0
    reasons = []

    for trace in sample_traces:
        prompt = _LLM_VERIFY_PROMPT.format(
            knowledge_type=knowledge_dict.get("knowledge_type", "unknown"),
            statement=knowledge_dict.get("statement", "N/A"),
            objective=knowledge_dict.get("objective", "N/A"),
            action_steps=knowledge_dict.get("action_steps", "N/A"),
            trace_summary=trace.get("abstract", trace.get("trace_id", "unknown")),
        )

        try:
            response = await llm_fn(prompt)
            # Parse JSON response
            result = json.loads(response)
            if result.get("improved", False):
                improved_count += 1
            reasons.append(result.get("reason", "no reason given"))
        except (json.JSONDecodeError, Exception) as e:
            reasons.append(f"LLM error: {e}")

    pass_rate = improved_count / len(sample_traces) if sample_traces else 0.0

    return LLMVerifyResult(
        passed=pass_rate >= min_pass_rate,
        pass_rate=pass_rate,
        reasons=reasons,
        sample_size=len(sample_traces),
    )


# ---------------------------------------------------------------------------
# Stage 3: Full Evaluation Pipeline
# ---------------------------------------------------------------------------

@dataclass
class EvalResult:
    """Result from the full evaluation pipeline."""
    status: str  # "needs_more_traces" | "needs_improvement" | "verified" | "active"
    stat_gate: Optional[GateResult] = None
    llm_verify: Optional[LLMVerifyResult] = None
    reason: str = ""


async def evaluate(
    knowledge_dict: Dict[str, Any],
    traces: List[Dict[str, Any]],
    llm_fn: Optional[Callable[..., Coroutine]] = None,
    min_traces: int = 3,
    min_success_rate: float = 0.7,
    min_source_users: int = 2,
    min_source_users_private: int = 1,
    llm_sample_size: int = 5,
    llm_min_pass_rate: float = 0.6,
    require_human_approval: bool = True,
    user_auto_approve_confidence: float = 0.95,
) -> EvalResult:
    """
    Full sandbox evaluation pipeline.

    1. Statistical gate
    2. LLM verify (if llm_fn provided)
    3. Auto-approve or await human approval

    Returns:
        EvalResult with final status
    """
    # Stage 1: Statistical gate
    gate = stat_gate(
        knowledge_dict, traces,
        min_traces=min_traces,
        min_success_rate=min_success_rate,
        min_source_users=min_source_users,
        min_source_users_private=min_source_users_private,
    )

    if not gate.passed:
        return EvalResult(
            status="needs_more_traces",
            stat_gate=gate,
            reason=gate.reason,
        )

    # Stage 2: LLM verify (if available)
    llm_result = None
    if llm_fn and traces:
        sample = traces[:llm_sample_size]
        llm_result = await llm_verify(
            knowledge_dict, sample, llm_fn,
            min_pass_rate=llm_min_pass_rate,
        )
        if not llm_result.passed:
            return EvalResult(
                status="needs_improvement",
                stat_gate=gate,
                llm_verify=llm_result,
                reason=f"LLM pass rate {llm_result.pass_rate:.2f} < {llm_min_pass_rate}",
            )

    # Stage 3: Auto-approve or await human
    scope = knowledge_dict.get("scope", "user")
    confidence = knowledge_dict.get("confidence", 0.0)

    if scope == "user" and confidence >= user_auto_approve_confidence:
        return EvalResult(
            status="active",
            stat_gate=gate,
            llm_verify=llm_result,
            reason="Auto-approved: user scope + high confidence",
        )

    if not require_human_approval:
        return EvalResult(
            status="active",
            stat_gate=gate,
            llm_verify=llm_result,
            reason="Auto-approved: human approval not required",
        )

    return EvalResult(
        status="verified",
        stat_gate=gate,
        llm_verify=llm_result,
        reason="Awaiting human approval",
    )
