"""Recall mutation engine for state-level cognitive updates."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Set

from .state_types import CognitiveState, ExposureState, RecallMutationResult


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecallMutationEngine:
    """Derive store-ready mutation updates from recall outcomes."""

    def __init__(
        self,
        *,
        source_label: str = "recall_mutation_engine",
        hot_activation_threshold: float = 0.8,
        reinforce_base_gain: float = 0.2,
        penalize_base_decay: float = 0.1,
    ) -> None:
        self._source_label = source_label
        self._hot_activation_threshold = hot_activation_threshold
        self._reinforce_base_gain = reinforce_base_gain
        self._penalize_base_decay = penalize_base_decay

    def apply(
        self,
        query: str,
        states: List[CognitiveState],
        recall_outcome: Mapping[str, Any] | None,
    ) -> RecallMutationResult:
        del query  # query is carried by caller and reserved for future scoring logic.

        outcome = dict(recall_outcome or {})
        used_owner_ids = self._extract_owner_ids(outcome.get("final_answer_used_memories"))
        recalled_owner_ids = self._extract_owner_ids(outcome.get("selected_results"))
        recalled_owner_ids.update(self._extract_owner_ids(outcome.get("cited_results")))
        recalled_owner_ids.update(self._extract_owner_ids(outcome.get("rejected_results")))
        conflict_signals = list(outcome.get("conflict_signals") or [])
        conflict_owner_ids = self._extract_owner_ids(conflict_signals)
        touched_owner_ids = used_owner_ids | recalled_owner_ids | conflict_owner_ids

        updates_by_owner: Dict[str, Dict[str, Any]] = {}
        explanations: List[str] = []
        contestation_events: List[Dict[str, Any]] = []

        for state in states:
            if state.owner_id not in touched_owner_ids:
                continue

            now = _utc_now_iso()
            fields: MutableMapping[str, Any] = {
                "access_count": state.access_count + 1,
                "last_accessed_at": now,
            }
            mutation_reason = ""

            if state.owner_id in used_owner_ids:
                gain = self._reinforce_base_gain * max(0.0, 1.0 - state.activation_score)
                next_activation = min(1.0, state.activation_score + gain)
                fields["activation_score"] = next_activation
                fields["last_reinforced_at"] = now
                mutation_reason = "reinforce"
                explanations.append(
                    f"reinforce:{state.owner_type.value}:{state.owner_id}:gain={gain:.4f}"
                )
            elif (
                state.owner_id in recalled_owner_ids
                and state.activation_score >= self._hot_activation_threshold
            ):
                decay = self._penalize_base_decay * max(0.0, state.activation_score)
                next_activation = max(0.0, state.activation_score - decay)
                fields["activation_score"] = next_activation
                fields["last_penalized_at"] = now
                mutation_reason = "penalize"
                explanations.append(
                    f"penalize:{state.owner_type.value}:{state.owner_id}:decay={decay:.4f}"
                )

            if state.owner_id in conflict_owner_ids:
                fields["exposure_state"] = ExposureState.CONTESTED.value
                mutation_reason = "contest"
                reason = self._extract_conflict_reason(conflict_signals, state.owner_id)
                contestation_events.append(
                    {
                        "owner_type": state.owner_type.value,
                        "owner_id": state.owner_id,
                        "state_id": state.state_id,
                        "reason": reason,
                        "at": now,
                    }
                )
                explanations.append(
                    f"contest:{state.owner_type.value}:{state.owner_id}:reason={reason}"
                )

            if mutation_reason:
                fields["last_mutation_reason"] = mutation_reason
                fields["last_mutation_source"] = self._source_label
                fields["last_mutation_at"] = now

            updates_by_owner[state.owner_id] = {
                "owner_type": state.owner_type,
                "owner_id": state.owner_id,
                "expected_version": state.version,
                "fields": dict(fields),
            }

        return RecallMutationResult(
            state_updates=list(updates_by_owner.values()),
            generated_candidates=[],
            quarantine_events=[],
            contestation_events=contestation_events,
            explanations=explanations,
        )

    @staticmethod
    def _extract_owner_ids(values: Any) -> Set[str]:
        owner_ids: Set[str] = set()
        if values is None:
            return owner_ids
        if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
            values = [values]
        for item in values:
            if isinstance(item, str):
                owner_ids.add(item)
                continue
            if not isinstance(item, Mapping):
                continue
            owner_id = item.get("owner_id")
            if isinstance(owner_id, str) and owner_id:
                owner_ids.add(owner_id)
                continue
            state_id = item.get("state_id")
            if isinstance(state_id, str) and ":" in state_id:
                _, _, tail = state_id.partition(":")
                if tail:
                    owner_ids.add(tail)
        return owner_ids

    @staticmethod
    def _extract_conflict_reason(conflict_signals: List[Any], owner_id: str) -> str:
        for signal in conflict_signals:
            if not isinstance(signal, Mapping):
                continue
            if signal.get("owner_id") == owner_id:
                reason = signal.get("reason")
                if isinstance(reason, str) and reason:
                    return reason
        return "conflict_signaled"
