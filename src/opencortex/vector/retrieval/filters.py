# SPDX-License-Identifier: Apache-2.0
"""Qdrant filter builders for opencortex recall."""

from __future__ import annotations

from qdrant_client import models

from opencortex.core.identity import IdentityProfile


def retrieval_filter(
    *,
    profile: IdentityProfile,
    surface: str | None = None,
    tenant_key: str = "tenant_id",
) -> models.Filter:
    """Build a visibility and surface filter for recall queries."""
    must: list[models.Condition] = [
        field_match("retrieval_ready", True),
        field_match(tenant_key, profile.tenant_id),
        field_match("project_id", profile.project_id),
    ]
    if surface:
        must.append(field_match("retrieval_surface", surface))

    must_not: list[models.Condition] = [
        field_match("meta.superseded", True),
    ]
    should: list[models.Condition] = [
        field_match("scope", "public"),
        field_match("user_id", profile.user_id),
        field_match("source_user_id", profile.user_id),
    ]

    return models.Filter(
        must=must,
        must_not=must_not,
        should=should,
        min_should=models.MinShould(conditions=should, min_count=1),
    )


def field_match(key: str, value: str | bool) -> models.FieldCondition:
    """Build an exact Qdrant field match."""
    return models.FieldCondition(
        key=key,
        match=models.MatchValue(value=value),
    )
