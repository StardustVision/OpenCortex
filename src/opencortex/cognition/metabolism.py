# SPDX-License-Identifier: Apache-2.0
"""Cognitive metabolism controller.

Pure/store-free controller that derives store-ready state updates from long-horizon
metabolism rules (cooling, compression, archiving).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, Mapping, MutableMapping, Tuple

from .state_types import CognitiveState, LifecycleState, MetabolismResult, OwnerType


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _value_score(state: CognitiveState) -> float:
    # Conservative "value" heuristic: stable and low-risk are preferred.
    return float(state.stability_score) - float(state.risk_score)


class CognitiveMetabolismController:
    """Deterministic metabolism tick for a batch of cognitive states."""

    def __init__(
        self,
        *,
        now_iso_fn: Callable[[], str] = _utc_now_iso,
        source_label: str = "cognitive_metabolism_controller",
        # Cooling dominant hot states (prevent winner-take-all loops).
        hot_activation_threshold: float = 0.8,
        dominance_count_threshold: int = 3,
        cooling_decay: float = 0.1,
        # Compression: cold + low-value active state -> compressed.
        compress_activation_threshold: float = 0.1,
        compress_value_threshold: float = 0.0,
        # Archiving: already compressed + deeper cold/low-value -> archived.
        archive_activation_threshold: float = 0.02,
        archive_value_threshold: float = 0.0,
        # Forgetting: already archived + deep-cold/low-value -> forgotten (logical terminal).
        forget_activation_threshold: float = 0.005,
        forget_value_threshold: float = 0.0,
    ) -> None:
        self._now_iso_fn = now_iso_fn
        self._source_label = str(source_label)
        self._hot_activation_threshold = float(hot_activation_threshold)
        self._dominance_count_threshold = int(dominance_count_threshold)
        self._cooling_decay = float(cooling_decay)
        self._compress_activation_threshold = float(compress_activation_threshold)
        self._compress_value_threshold = float(compress_value_threshold)
        self._archive_activation_threshold = float(archive_activation_threshold)
        self._archive_value_threshold = float(archive_value_threshold)
        self._forget_activation_threshold = float(forget_activation_threshold)
        self._forget_value_threshold = float(forget_value_threshold)

    def tick(
        self,
        states: Mapping[str, CognitiveState] | Iterable[CognitiveState],
        dominance_window: Mapping[Any, Any] | Iterable[Any] | None = None,
    ) -> MetabolismResult:
        now = self._now_iso_fn()
        updates_by_owner: Dict[Tuple[OwnerType, str], Dict[str, Any]] = {}
        review_events: list[Dict[str, Any]] = []

        for state in self._iter_states(states):
            if state.lifecycle_state == LifecycleState.FORGOTTEN:
                continue

            fields: MutableMapping[str, Any] = {}
            mutation_reason: str | None = None

            score = _value_score(state)

            if (
                state.lifecycle_state == LifecycleState.ARCHIVED
                and state.activation_score <= self._forget_activation_threshold
                and score <= self._forget_value_threshold
            ):
                mutation_reason = "metabolism_forget"
                fields["lifecycle_state"] = LifecycleState.FORGOTTEN.value
                review_events.append(
                    {
                        "kind": "forget",
                        "owner_type": state.owner_type.value,
                        "owner_id": state.owner_id,
                        "state_id": state.state_id,
                        "lifecycle_before": state.lifecycle_state.value,
                        "lifecycle_after": LifecycleState.FORGOTTEN.value,
                        "reason": mutation_reason,
                        "at": now,
                    }
                )
            elif (
                state.lifecycle_state == LifecycleState.COMPRESSED
                and state.activation_score <= self._archive_activation_threshold
                and score <= self._archive_value_threshold
            ):
                mutation_reason = "metabolism_archive"
                fields["lifecycle_state"] = LifecycleState.ARCHIVED.value
                review_events.append(
                    {
                        "kind": "archive",
                        "owner_type": state.owner_type.value,
                        "owner_id": state.owner_id,
                        "state_id": state.state_id,
                        "lifecycle_before": state.lifecycle_state.value,
                        "lifecycle_after": LifecycleState.ARCHIVED.value,
                        "reason": mutation_reason,
                        "at": now,
                    }
                )
            elif (
                state.lifecycle_state == LifecycleState.ACTIVE
                and state.activation_score <= self._compress_activation_threshold
                and score <= self._compress_value_threshold
            ):
                mutation_reason = "metabolism_compress"
                fields["lifecycle_state"] = LifecycleState.COMPRESSED.value
                review_events.append(
                    {
                        "kind": "compress",
                        "owner_type": state.owner_type.value,
                        "owner_id": state.owner_id,
                        "state_id": state.state_id,
                        "lifecycle_before": state.lifecycle_state.value,
                        "lifecycle_after": LifecycleState.COMPRESSED.value,
                        "reason": mutation_reason,
                        "at": now,
                    }
                )
            else:
                dominance = self._dominance_count(dominance_window, state.owner_type, state.owner_id)
                if (
                    dominance >= self._dominance_count_threshold
                    and state.activation_score >= self._hot_activation_threshold
                ):
                    mutation_reason = "metabolism_cool"
                    decay = self._cooling_decay * max(0.0, float(state.activation_score))
                    next_activation = max(0.0, float(state.activation_score) - decay)
                    if next_activation < float(state.activation_score):
                        fields["activation_score"] = next_activation

            if not fields or mutation_reason is None:
                continue

            fields["last_mutation_at"] = now
            fields["last_mutation_reason"] = mutation_reason
            fields["last_mutation_source"] = self._source_label

            updates_by_owner[(state.owner_type, state.owner_id)] = {
                "owner_type": state.owner_type,
                "owner_id": state.owner_id,
                "expected_version": state.version,
                "fields": dict(fields),
            }

        return MetabolismResult(
            state_updates=list(updates_by_owner.values()),
            review_events=review_events,
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
    def _dominance_count(
        dominance_window: Mapping[Any, Any] | Iterable[Any] | None,
        owner_type: OwnerType,
        owner_id: str,
    ) -> int:
        if dominance_window is None:
            return 0

        key_a = (owner_type.value, owner_id)
        key_b = (owner_type, owner_id)
        key_c = f"{owner_type.value}:{owner_id}"

        if isinstance(dominance_window, Mapping):
            raw = (
                dominance_window.get(key_a)
                if key_a in dominance_window
                else dominance_window.get(key_b)
                if key_b in dominance_window
                else dominance_window.get(key_c)
                if key_c in dominance_window
                else dominance_window.get(owner_id)
            )
            if isinstance(raw, Mapping):
                wins = raw.get("wins")
                if wins is None:
                    wins = raw.get("dominance_wins")
                if wins is None:
                    wins = raw.get("count")
                try:
                    return int(wins or 0)
                except (TypeError, ValueError):
                    return 0
            try:
                return int(raw or 0)
            except (TypeError, ValueError):
                return 0

        # Fallback: count occurrences of matching winner markers.
        count = 0
        for item in dominance_window:
            if item is None:
                continue
            if isinstance(item, tuple) and len(item) == 2 and item == key_a:
                count += 1
                continue
            if isinstance(item, Mapping):
                ot = item.get("owner_type")
                oid = item.get("owner_id")
                if (ot == owner_type.value or ot == owner_type) and oid == owner_id:
                    count += 1
        return count
