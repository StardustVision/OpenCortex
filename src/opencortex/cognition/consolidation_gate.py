# SPDX-License-Identifier: Apache-2.0
"""Consolidation gate: propose durable consolidation candidates + map governance feedback."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence

from opencortex.cognition.candidate_store import CandidateStore
from opencortex.cognition.state_types import (
    CognitiveState,
    ConsolidationCandidate,
    ConsolidationState,
    ExposureState,
    GovernanceFeedback,
    GovernanceFeedbackKind,
    OwnerType,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None and str(v)]
    return [str(value)]


@dataclass
class ConsolidationGateResult:
    candidates: List[ConsolidationCandidate] = field(default_factory=list)
    state_updates: List[Dict[str, Any]] = field(default_factory=list)


class ConsolidationGate:
    def __init__(
        self,
        *,
        candidate_store: CandidateStore,
        cooldown_seconds: int = 3600,
        now_iso_fn: Callable[[], str] = _utc_now_iso,
        source_label: str = "consolidation_gate",
    ) -> None:
        self._candidate_store = candidate_store
        self._cooldown_seconds = int(cooldown_seconds)
        self._now_iso_fn = now_iso_fn
        self._source_label = source_label

    async def evaluate(self, states: Iterable[CognitiveState]) -> ConsolidationGateResult:
        now = self._now_iso_fn()
        candidates: List[ConsolidationCandidate] = []
        state_updates: List[Dict[str, Any]] = []
        seen_fingerprints: set[str] = set()

        for state in states:
            if not isinstance(state, CognitiveState):
                continue
            if state.consolidation_state != ConsolidationState.CANDIDATE:
                continue

            candidate = self._build_candidate(state)
            if candidate is None:
                continue

            fingerprint = self._candidate_store.build_fingerprint(candidate)
            if fingerprint in seen_fingerprints:
                continue
            seen_fingerprints.add(fingerprint)
            candidate.dedupe_fingerprint = fingerprint
            is_dup = await self._candidate_store.is_duplicate_within_cooldown(
                dedupe_fingerprint=candidate.dedupe_fingerprint,
                tenant_id=candidate.tenant_id,
                user_id=candidate.user_id,
                project_id=candidate.project_id,
                cooldown_seconds=self._cooldown_seconds,
                now_iso=now,
            )
            if is_dup:
                continue

            candidates.append(candidate)
            state_updates.append(
                {
                    "owner_type": state.owner_type,
                    "owner_id": state.owner_id,
                    "expected_version": state.version,
                    "fields": {
                        "consolidation_state": ConsolidationState.SUBMITTED.value,
                        "last_mutation_at": now,
                        "last_mutation_reason": "submit_consolidation_candidate",
                        "last_mutation_source": self._source_label,
                        "metadata": {
                            **(state.metadata or {}),
                            "last_consolidation_candidate_id": candidate.candidate_id,
                            "last_consolidation_candidate_fingerprint": candidate.dedupe_fingerprint,
                        },
                    },
                }
            )

        if candidates:
            await self._candidate_store.save_many(candidates)

        return ConsolidationGateResult(candidates=candidates, state_updates=state_updates)

    def map_governance_feedback(
        self, feedback: Sequence[GovernanceFeedback], *, states: Sequence[CognitiveState]
    ) -> List[Dict[str, Any]]:
        now = self._now_iso_fn()
        states_by_owner: Dict[tuple[OwnerType, str], CognitiveState] = {}
        for state in states:
            if isinstance(state, CognitiveState):
                states_by_owner[(state.owner_type, state.owner_id)] = state

        updates: List[Dict[str, Any]] = []
        for item in feedback:
            state = states_by_owner.get((item.owner_type, item.owner_id))
            if state is None:
                continue

            fields: Dict[str, Any] = {
                "last_mutation_at": now,
                "last_mutation_reason": "governance_feedback",
                "last_mutation_source": self._source_label,
                "metadata": {
                    **(state.metadata or {}),
                    "last_governance_candidate_id": item.candidate_id,
                    "last_governance_feedback_kind": item.kind.value,
                    "last_governance_material_new_evidence": bool(item.has_material_new_evidence),
                },
            }

            if item.kind == GovernanceFeedbackKind.ACCEPTED:
                fields["consolidation_state"] = ConsolidationState.ACCEPTED.value
            elif item.kind == GovernanceFeedbackKind.REJECTED:
                if item.has_material_new_evidence:
                    fields["consolidation_state"] = ConsolidationState.NONE.value
                else:
                    fields["consolidation_state"] = ConsolidationState.REJECTED.value
            elif item.kind == GovernanceFeedbackKind.CONTESTED:
                fields["exposure_state"] = ExposureState.CONTESTED.value
                fields["consolidation_state"] = ConsolidationState.SUBMITTED.value
            elif item.kind == GovernanceFeedbackKind.DEPRECATED:
                fields["consolidation_state"] = ConsolidationState.EXPIRED.value

            updates.append(
                {
                    "owner_type": state.owner_type,
                    "owner_id": state.owner_id,
                    "expected_version": state.version,
                    "fields": fields,
                }
            )

        return updates

    @staticmethod
    def _build_candidate(state: CognitiveState) -> ConsolidationCandidate | None:
        md: Mapping[str, Any] = state.metadata or {}
        statement = md.get("statement") or ""
        if not isinstance(statement, str) or not statement.strip():
            return None

        abstract = md.get("abstract") or ""
        overview = md.get("overview") or ""
        submission_reason = md.get("submission_reason") or ""
        conflict_summary = md.get("conflict_summary") or ""
        if not conflict_summary and state.exposure_state == ExposureState.CONTESTED:
            conflict_summary = "contested"

        confidence = md.get("confidence_estimate")
        if not isinstance(confidence, (int, float)):
            # Lightweight heuristic: stable + evidence, penalize risk.
            confidence = max(
                0.0,
                min(
                    1.0,
                    0.6 * float(state.stability_score)
                    + 0.4 * float(state.evidence_residual_score)
                    - 0.2 * float(state.risk_score),
                ),
            )

        candidate_kind = md.get("candidate_kind") or "state_consolidation"

        return ConsolidationCandidate(
            candidate_id=ConsolidationCandidate.new_id(),
            source_owner_type=state.owner_type.value,
            source_owner_id=state.owner_id,
            tenant_id=state.tenant_id,
            user_id=state.user_id,
            project_id=state.project_id,
            candidate_kind=str(candidate_kind),
            statement=str(statement),
            abstract=str(abstract),
            overview=str(overview),
            supporting_memory_ids=_as_str_list(md.get("supporting_memory_ids")),
            supporting_trace_ids=_as_str_list(md.get("supporting_trace_ids")),
            confidence_estimate=float(confidence),
            stability_score=float(state.stability_score),
            risk_score=float(state.risk_score),
            conflict_summary=str(conflict_summary),
            submission_reason=str(submission_reason),
            dedupe_fingerprint="",
        )
