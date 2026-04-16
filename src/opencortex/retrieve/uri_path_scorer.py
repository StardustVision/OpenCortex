"""
uri_path_scorer — URI-linked minimum-cost path scoring for memory recall.

Pure function module with zero external dependencies.
Operates on three retrieval surfaces (leaf / anchor / fact_point) connected
by URI projection links, computing the minimum path cost per leaf.

Cost model:
  direct:       leaf_distance + URI_DIRECT_PENALTY
  anchor→leaf:  anchor_distance + URI_HOP_COST
  fp→leaf:      fp_distance + hop
                  where hop = URI_HOP_COST * HIGH_CONFIDENCE_DISCOUNT
                        if fp_distance < HIGH_CONFIDENCE_THRESHOLD
                        else URI_HOP_COST

Per leaf: final_cost = min(all paths reaching that leaf)

Score offset semantics (vs raw cosine similarity)
--------------------------------------------------
The final URI path score exposed to callers is ``1.0 - min_cost`` and is
therefore offset relative to the raw cosine ``_score`` that would come out
of a direct vector search:

  - direct leaf hit:       score = 1.0 - (1.0 - cosine) - URI_DIRECT_PENALTY
                                  = cosine - 0.15
  - anchor → leaf path:    score = cosine_anchor - URI_HOP_COST
                                  = cosine_anchor - 0.05
  - fp → leaf path:        score = cosine_fp - URI_HOP_COST
                                  = cosine_fp - 0.05
  - fp → leaf (high conf): score = cosine_fp - URI_HOP_COST * 0.5
                                  = cosine_fp - 0.025
                           (triggered when fp_distance < HIGH_CONFIDENCE_THRESHOLD,
                            i.e. cosine_fp > 0.90)

Callers using a fixed ``score_threshold`` should expect a score-distribution
shift. Direct-hit-dominant workloads are shifted DOWN by up to 0.15: a leaf
with cosine=0.82 that previously passed a threshold of 0.80 now emerges with
URI path score 0.67 and gets dropped. Consider lowering ``score_threshold``
by the expected penalty, or run a benchmark to recalibrate.

URI_DIRECT_PENALTY starts at 0.15 (conservative) and will ramp to 0.30 after
fact_point coverage is well-established. Existing thresholds will need
re-tuning at that time.
"""

from typing import Dict, List

URI_DIRECT_PENALTY = 0.15          # Added to direct leaf hits (conservative; ramp to 0.30 later)
URI_HOP_COST = 0.05                # Per-hop cost for anchor/fp paths
HIGH_CONFIDENCE_THRESHOLD = 0.10   # fp distance strictly below this triggers discount
HIGH_CONFIDENCE_DISCOUNT = 0.5     # Multiply URI_HOP_COST by this at high confidence


def compute_uri_path_scores(
    leaf_hits: List[Dict],
    anchor_hits: List[Dict],
    fact_point_hits: List[Dict],
) -> Dict[str, float]:
    """Return {leaf_uri: min_path_cost} — lower cost means better match.

    Args:
        leaf_hits:        Dicts with at least ``_score`` and ``uri``.
        anchor_hits:      Dicts with at least ``_score``, ``uri``, and
                          ``projection_target_uri`` (top-level or in ``meta``).
        fact_point_hits:  Same shape as anchor_hits.

    Returns:
        Mapping from leaf URI to its minimum path cost across all paths.
        Leaves discovered only through anchor/fp projection are included even
        if they were absent from ``leaf_hits``.
    """
    leaf_paths: Dict[str, List[float]] = {}

    # --- Direct paths (leaf hits) ---
    for hit in leaf_hits:
        score = float(hit.get("_score", 0.0))
        score = max(0.0, min(1.0, score))
        dist = 1.0 - score
        cost = dist + URI_DIRECT_PENALTY
        uri = hit.get("uri", "")
        if not uri:
            continue
        leaf_paths.setdefault(uri, []).append(cost)

    # --- Anchor paths (anchor → leaf) ---
    for hit in anchor_hits:
        target_uri = hit.get("projection_target_uri") or (hit.get("meta") or {}).get("projection_target_uri", "")
        if not target_uri:
            continue
        score = float(hit.get("_score", 0.0))
        score = max(0.0, min(1.0, score))
        dist = 1.0 - score
        cost = dist + URI_HOP_COST
        leaf_paths.setdefault(target_uri, []).append(cost)

    # --- Fact-point paths (fp → leaf, with high-confidence discount) ---
    for hit in fact_point_hits:
        target_uri = hit.get("projection_target_uri") or (hit.get("meta") or {}).get("projection_target_uri", "")
        if not target_uri:
            continue
        score = float(hit.get("_score", 0.0))
        score = max(0.0, min(1.0, score))
        dist = 1.0 - score
        hop = URI_HOP_COST * HIGH_CONFIDENCE_DISCOUNT if dist < HIGH_CONFIDENCE_THRESHOLD else URI_HOP_COST
        cost = dist + hop
        leaf_paths.setdefault(target_uri, []).append(cost)

    return {uri: min(paths) for uri, paths in leaf_paths.items()}
