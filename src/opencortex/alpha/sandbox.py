"""
Sandbox Evaluator — validates knowledge candidates before approval.

Three-stage pipeline:
  1. Statistical gate: trace count, success rate, user diversity
  2. LLM simulation verification
  3. Human approval flow

Design doc §5.4, §10.3.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

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
