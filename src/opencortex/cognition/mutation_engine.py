"""Recall mutation engine for state-level cognitive updates."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Set, Tuple

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
        states: Mapping[str, CognitiveState] | Iterable[CognitiveState],
        recall_outcome: Mapping[str, Any] | None,
    ) -> RecallMutationResult:
        del query  # query is carried by caller and reserved for future scoring logic.

        outcome = dict(recall_outcome or {})
        used_state_keys = self._extract_state_keys(
            outcome.get("final_answer_used_memories"), default_owner_type="memory"
        )
        recalled_state_keys = self._extract_state_keys(
            outcome.get("selected_results"), default_owner_type="memory"
        )
        recalled_state_keys.update(
            self._extract_state_keys(outcome.get("cited_results"), default_owner_type="memory")
        )
        recalled_state_keys.update(
            self._extract_state_keys(outcome.get("rejected_results"), default_owner_type="memory")
        )
        conflict_signals = list(outcome.get("conflict_signals") or [])
        conflict_state_keys = self._extract_state_keys(
            conflict_signals, default_owner_type="memory"
        )
        touched_state_keys = used_state_keys | recalled_state_keys | conflict_state_keys

        updates_by_owner: Dict[Tuple[str, str], Dict[str, Any]] = {}
        explanations: List[Dict[str, Any]] = []
        contestation_events: List[Dict[str, Any]] = []

        for state in self._iter_states(states):
            state_key = (state.owner_type.value, state.owner_id)
            if state_key not in touched_state_keys:
                continue

            now = _utc_now_iso()
            fields: MutableMapping[str, Any] = {
                "access_count": state.access_count + 1,
                "last_accessed_at": now,
            }
            mutation_reason = "touch"

            if state_key in used_state_keys:
                gain = self._reinforce_base_gain * max(0.0, 1.0 - state.activation_score)
                next_activation = min(1.0, state.activation_score + gain)
                fields["activation_score"] = next_activation
                fields["last_reinforced_at"] = now
                mutation_reason = "reinforce"
                explanations.append(
                    {
                        "kind": "reinforce",
                        "owner_type": state.owner_type.value,
                        "owner_id": state.owner_id,
                        "state_id": state.state_id,
                        "gain": gain,
                        "activation_before": state.activation_score,
                        "activation_after": next_activation,
                    }
                )
            elif (
                state_key in recalled_state_keys
                and state.activation_score >= self._hot_activation_threshold
            ):
                decay = self._penalize_base_decay * max(0.0, state.activation_score)
                next_activation = max(0.0, state.activation_score - decay)
                fields["activation_score"] = next_activation
                fields["last_penalized_at"] = now
                mutation_reason = "penalize"
                explanations.append(
                    {
                        "kind": "penalize",
                        "owner_type": state.owner_type.value,
                        "owner_id": state.owner_id,
                        "state_id": state.state_id,
                        "decay": decay,
                        "activation_before": state.activation_score,
                        "activation_after": next_activation,
                    }
                )

            if state_key in conflict_state_keys:
                fields["exposure_state"] = ExposureState.CONTESTED.value
                mutation_reason = "contest"
                reason = self._extract_conflict_reason(
                    conflict_signals, owner_type=state.owner_type.value, owner_id=state.owner_id
                )
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
                    {
                        "kind": "contest",
                        "owner_type": state.owner_type.value,
                        "owner_id": state.owner_id,
                        "state_id": state.state_id,
                        "reason": reason,
                    }
                )

            fields["last_mutation_reason"] = mutation_reason
            fields["last_mutation_source"] = self._source_label
            fields["last_mutation_at"] = now

            updates_by_owner[state_key] = {
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
    def _iter_states(
        states: Mapping[str, CognitiveState] | Iterable[CognitiveState],
    ) -> Iterator[CognitiveState]:
        if isinstance(states, Mapping):
            for state in states.values():
                if isinstance(state, CognitiveState):
                    yield state
            return
        for state in states:
            if isinstance(state, CognitiveState):
                yield state

    @staticmethod
    def _extract_state_keys(
        values: Any, *, default_owner_type: str = "memory"
    ) -> Set[Tuple[str, str]]:
        state_keys: Set[Tuple[str, str]] = set()
        if values is None:
            return state_keys
        if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
            values = [values]
        for item in values:
            if isinstance(item, str):
                state_keys.add((default_owner_type, item))
                continue
            if not isinstance(item, Mapping):
                continue
            owner_type_raw = item.get("owner_type", default_owner_type)
            owner_type = (
                owner_type_raw.value if hasattr(owner_type_raw, "value") else str(owner_type_raw)
            )
            owner_id = item.get("owner_id")
            if isinstance(owner_id, str) and owner_id:
                state_keys.add((owner_type, owner_id))
                continue
            state_id = item.get("state_id")
            if isinstance(state_id, str) and ":" in state_id:
                prefix, _, tail = state_id.partition(":")
                if tail:
                    state_keys.add((prefix or default_owner_type, tail))
        return state_keys

    @staticmethod
    def _extract_conflict_reason(
        conflict_signals: List[Any], *, owner_type: str, owner_id: str
    ) -> str:
        for signal in conflict_signals:
            if not isinstance(signal, Mapping):
                continue
            signal_keys = RecallMutationEngine._extract_state_keys(
                [signal], default_owner_type="memory"
            )
            if (owner_type, owner_id) in signal_keys:
                reason = signal.get("reason")
                if isinstance(reason, str) and reason:
                    return reason
        return "conflict_signaled"
