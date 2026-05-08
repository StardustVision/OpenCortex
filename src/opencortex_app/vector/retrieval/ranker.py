# SPDX-License-Identifier: Apache-2.0
"""Candidate fusion for opencortex_app recall."""

from __future__ import annotations

from typing import Any

from opencortex_app.vector.retrieval.schemas import (
    RetrievalHit,
    RetrievalPlan,
    RetrievalSurface,
)


class RetrievalRanker:
    """Fuse hits from primary and secondary retrieval surfaces."""

    def rank(
        self,
        hits: list[RetrievalHit],
        *,
        plan: RetrievalPlan,
    ) -> list[RetrievalHit]:
        """Collapse surface hits to one scored hit per primary URI."""
        by_uri: dict[str, RetrievalHit] = {}
        surface_counts: dict[str, set[RetrievalSurface]] = {}
        for hit in hits:
            uri = hit.source_uri or str(hit.record.get("uri", "") or "")
            if not uri:
                continue
            weighted_hit = self.weighted_hit(hit, plan=plan)
            current = by_uri.get(uri)
            if current is None or weighted_hit.score > current.score:
                by_uri[uri] = weighted_hit
            surface_counts.setdefault(uri, set()).add(weighted_hit.surface)

        fused: list[RetrievalHit] = []
        for uri, hit in by_uri.items():
            surfaces = surface_counts.get(uri, set())
            score = hit.score
            score += min(
                plan.max_diversity_bonus,
                max(0, len(surfaces) - 1) * plan.diversity_bonus,
            )
            for surface in surfaces:
                score += plan.surface_bonus.get(surface, 0.0)
            if is_near_starting_uri(uri, plan.starting_uris):
                score += plan.starting_uri_bonus
            record = dict(hit.record)
            record["_retrieval_surfaces"] = sorted(
                surface.value for surface in surfaces
            )
            record["_final_score"] = score
            fused.append(
                RetrievalHit(
                    record=record,
                    score=score,
                    surface=hit.surface,
                    source_uri=uri,
                )
            )
        fused.sort(key=lambda item: item.score, reverse=True)
        return fused[: plan.limit]

    @staticmethod
    def weighted_hit(hit: RetrievalHit, *, plan: RetrievalPlan) -> RetrievalHit:
        """Apply planner surface weight without mutating executor output."""
        weight = plan.surface_weights.get(hit.surface, 1.0)
        record = dict(hit.record)
        record["_raw_score"] = hit.score
        record["_weighted_score"] = hit.score * weight
        return RetrievalHit(
            record=record,
            score=hit.score * weight,
            surface=hit.surface,
            source_uri=hit.source_uri,
            path_cost=hit.path_cost,
        )


def merge_primary_payload(
    *,
    hit: RetrievalHit,
    primary: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the primary payload for a fused hit."""
    payload = dict(primary or hit.record)
    payload["_final_score"] = hit.score
    payload["_retrieval_surfaces"] = list(
        hit.record.get("_retrieval_surfaces") or [hit.surface.value]
    )
    return payload


def is_near_starting_uri(uri: str, starting_uris: list[str]) -> bool:
    """Return whether a URI is equal to or below a planner starting URI."""
    for starting_uri in starting_uris:
        text = str(starting_uri or "").strip()
        if not text:
            continue
        prefix = text if text.endswith("/") else f"{text}/"
        if uri == text or uri.startswith(prefix):
            return True
    return False
