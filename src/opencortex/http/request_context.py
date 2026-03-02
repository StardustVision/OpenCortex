# SPDX-License-Identifier: Apache-2.0
"""
Per-request tenant/user identity via contextvars.

In multi-tenant HTTP mode, each request carries its own tenant_id and user_id
via ``X-Tenant-ID`` / ``X-User-ID`` headers.  The middleware sets these into
contextvars so that downstream code (orchestrator, retriever) can read the
effective identity without changing method signatures.

When no contextvar is set (e.g. in tests or single-tenant mode), callers
fall back to "default".
"""

from contextvars import ContextVar, Token
from typing import Optional, Tuple

_DEFAULT_TENANT = "default"
_DEFAULT_USER = "default"

_request_tenant_id: ContextVar[Optional[str]] = ContextVar(
    "_request_tenant_id", default=None
)
_request_user_id: ContextVar[Optional[str]] = ContextVar(
    "_request_user_id", default=None
)


def set_request_identity(
    tenant_id: str, user_id: str
) -> Tuple[Token[Optional[str]], Token[Optional[str]]]:
    """Set per-request identity.  Returns tokens for later reset."""
    t1 = _request_tenant_id.set(tenant_id)
    t2 = _request_user_id.set(user_id)
    return (t1, t2)


def reset_request_identity(
    tokens: Tuple[Token[Optional[str]], Token[Optional[str]]]
) -> None:
    """Reset contextvars using tokens from :func:`set_request_identity`."""
    _request_tenant_id.reset(tokens[0])
    _request_user_id.reset(tokens[1])


def get_effective_identity() -> Tuple[str, str]:
    """Return (tenant_id, user_id), preferring contextvar over defaults.

    Resolution order:
    1. contextvar (set by middleware for this request)
    2. "default" / "default"
    """
    tenant = _request_tenant_id.get() or _DEFAULT_TENANT
    user = _request_user_id.get() or _DEFAULT_USER
    return (tenant, user)
