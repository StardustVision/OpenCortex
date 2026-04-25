# SPDX-License-Identifier: Apache-2.0
"""Shared HTTP connection-pool stat helpers and tunable caps.

Single source of truth for:
- ``HTTPX_MAX_CONNECTIONS`` / ``HTTPX_MAX_KEEPALIVE_CONNECTIONS`` —
  the project-wide caps applied to every long-lived ``httpx.AsyncClient``
  the server holds. Plan 009 / R2 set these to the conservative values
  derived from the original CLOSE_WAIT incident: high enough that
  normal recall-path concurrency does not block, low enough that a
  future pool-leak regression triggers backpressure before the kernel
  exhausts ephemeral ports.
- ``POOL_DEGRADED_THRESHOLD`` — the open/max ratio at which
  ``classify_pool_status`` flips a single client to ``"degraded"`` and
  the periodic sweeper logs a WARNING. Same number, two consumers, so
  operator alerts and health-endpoint state stay consistent.
- ``extract_pool_stats(client)`` — best-effort read of the httpx
  internal pool. Wrapped in try/except so a future httpx version bump
  cannot crash callers (admin health endpoint and the periodic sweeper).
- ``classify_pool_status(stats)`` — single-client healthy/degraded/
  unavailable classifier from the dict ``extract_pool_stats`` returns.

REVIEW closure tracker (plan 009 review):
- MAINT-001 / kieran-py-001: the helpers were originally
  underscore-private in ``http/admin_routes.py`` and imported lazily
  from ``orchestrator.py``. That layering inversion is gone — both
  callers depend on this neutral module, which depends on neither.
- MAINT-002: pool-cap constants were duplicated across three modules
  (``llm_factory.py``, ``rerank_client.py``, comments in
  ``orchestrator.py``). Consolidated here.
"""

from __future__ import annotations

from typing import Any, Dict

# Plan 009 / R2 — pool caps applied to every server-held async client.
HTTPX_MAX_CONNECTIONS = 20
HTTPX_MAX_KEEPALIVE_CONNECTIONS = 5

# Plan 009 / R5 — single-client status threshold. open/max above this
# ratio flips ``classify_pool_status`` to "degraded" and trips the
# sweeper's WARNING log.
POOL_DEGRADED_THRESHOLD = 0.8


def extract_pool_stats(client: Any) -> Dict[str, Any]:
    """Best-effort read of httpx pool internals.

    httpx exposes pool counts via ``client._transport._pool``, which is
    a private API. Wrap in try/except so a future httpx version bump
    does not crash callers — they still see "this client exists with
    these limits" even when live counts are unavailable.

    Returns a dict shaped like::

        {
            "stats_source": "transport_pool" | "unavailable",
            "open_connections": int | None,
            "keepalive_connections": int | None,
            "limits": {"max_connections": int, "max_keepalive_connections": int} | None,
            "reason": str  # only when stats_source == "unavailable"
        }
    """
    out: Dict[str, Any] = {
        "stats_source": "unavailable",
        "open_connections": None,
        "keepalive_connections": None,
        "limits": None,
    }
    if client is None:
        out["reason"] = "client is None"
        return out
    # Limits are a public-ish init kwarg held internally as ``_limits``
    # — not strictly public but stable across httpx 0.27.x. Read first
    # because it's cheaper than the pool walk.
    try:
        limits = getattr(client, "_limits", None)
        if limits is not None:
            out["limits"] = {
                "max_connections": getattr(limits, "max_connections", None),
                "max_keepalive_connections": getattr(
                    limits, "max_keepalive_connections", None,
                ),
            }
    except Exception:
        pass
    try:
        transport = getattr(client, "_transport", None)
        pool = getattr(transport, "_pool", None) if transport is not None else None
        if pool is None:
            out["reason"] = "transport pool not exposed"
            return out
        connections = list(getattr(pool, "connections", []) or [])
        out["open_connections"] = len(connections)
        keepalive = 0
        for conn in connections:
            try:
                if hasattr(conn, "is_idle") and conn.is_idle():
                    keepalive += 1
            except Exception:
                pass
        out["keepalive_connections"] = keepalive
        out["stats_source"] = "transport_pool"
    except Exception as exc:
        out["reason"] = f"pool read failed: {exc}"
    return out


def classify_pool_status(stats: Dict[str, Any]) -> str:
    """Return ``"healthy"`` / ``"degraded"`` / ``"unavailable"``.

    Single-client classification; the admin health endpoint folds the
    per-client values into the worst case across all clients.
    """
    if stats.get("stats_source") != "transport_pool":
        return "unavailable"
    open_count = stats.get("open_connections")
    limits = stats.get("limits") or {}
    max_conn = limits.get("max_connections")
    if not isinstance(open_count, int) or not isinstance(max_conn, int) or max_conn <= 0:
        return "unavailable"
    if open_count > POOL_DEGRADED_THRESHOLD * max_conn:
        return "degraded"
    return "healthy"
