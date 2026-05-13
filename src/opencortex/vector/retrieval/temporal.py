# SPDX-License-Identifier: Apache-2.0
"""Explicit temporal query helpers for recall."""

from __future__ import annotations

import re
from typing import Any

from qdrant_client import models

from opencortex.utils.facts import normalize_date_ref
from opencortex.vector.retrieval.schemas import TemporalPlan

_DATE_TOKEN_RE = re.compile(
    r"\b(?:19|20)\d{2}(?:[-/.](?:0?[1-9]|1[0-2])(?:[-/.](?:0?[1-9]|[12]\d|3[01]))?)?\b"
)


def parse_temporal_plan(query: str) -> TemporalPlan:
    """Return a bounded temporal plan from explicit query text only."""
    lowered = str(query or "").lower()
    dates = [
        normalize_date_ref(match.group(0)) for match in _DATE_TOKEN_RE.finditer(lowered)
    ]
    dates = [value for value in dates if value]
    before = first_after_hint(
        lowered, ("before", "until", "之前", "以前", "直到"), dates
    )
    after = first_after_hint(lowered, ("after", "since", "之后", "以后", "以来"), dates)
    order = ""
    if any(word in lowered for word in ("latest", "最近", "最晚")):
        order = "latest"
    elif any(word in lowered for word in ("earliest", "最早")):
        order = "earliest"
    return TemporalPlan(
        enabled=bool(before or after or order),
        before=before,
        until=before,
        after=after,
        since=after,
        order=order,
    )


def first_after_hint(query: str, hints: tuple[str, ...], dates: list[str]) -> str:
    """Return the first explicit date if the query contains one of the hints."""
    if not dates or not any(hint in query for hint in hints):
        return ""
    return dates[0]


def temporal_filter_conditions(plan: TemporalPlan) -> list[models.Condition]:
    """Build Qdrant datetime range conditions for explicit temporal constraints."""
    if not plan.enabled:
        return []
    range_kwargs: dict[str, Any] = {}
    after = plan.after or plan.since
    before = plan.before or plan.until
    if after:
        range_kwargs["gte"] = after
    if before:
        range_kwargs["lte"] = before
    if not range_kwargs:
        return []
    date_range = models.DatetimeRange(**range_kwargs)
    conditions = [
        models.FieldCondition(key="event_ts", range=date_range),
        models.FieldCondition(key="utterance_ts", range=date_range),
        models.FieldCondition(key="date_range_start", range=date_range),
        models.FieldCondition(key="date_range_end", range=date_range),
    ]
    return [
        models.Filter(
            should=conditions,
            min_should=models.MinShould(conditions=conditions, min_count=1),
        )
    ]


def record_time_key(record: dict[str, Any]) -> str:
    """Return the best top-level timestamp for temporal tie-breaking."""
    for key in ("event_ts", "utterance_ts", "date_range_start", "date_range_end"):
        value = str(record.get(key, "") or "")
        if value:
            return value
    meta = dict(record.get("meta") or {})
    for key in ("event_ts", "utterance_ts", "event_date", "timestamp"):
        value = normalize_date_ref(meta.get(key))
        if value:
            return value
    return ""
