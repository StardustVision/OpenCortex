# SPDX-License-Identifier: Apache-2.0
"""Per-request identity via contextvars.

In multi-tenant HTTP mode, each request carries its own identity derived
from a JWT Bearer token.  The middleware decodes the token and sets
tenant_id / user_id into contextvars so that downstream code can read the
effective values without changing method signatures.

When no contextvar is set (e.g. in tests or single-tenant mode), callers
fall back to defaults.
"""

from contextvars import ContextVar, Token
from typing import Optional, Tuple

from opencortex.core.identity import IdentityProfile

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_DEFAULT_TENANT = "default"
_DEFAULT_USER = "default"
_DEFAULT_PROJECT = "public"

# ---------------------------------------------------------------------------
# Identity contextvars
# ---------------------------------------------------------------------------
_request_tenant_id: ContextVar[Optional[str]] = ContextVar(
    "_request_tenant_id", default=None
)
_request_user_id: ContextVar[Optional[str]] = ContextVar(
    "_request_user_id", default=None
)
_request_project_id: ContextVar[Optional[str]] = ContextVar(
    "_request_project_id", default=None
)
_identity_profile: ContextVar[Optional[IdentityProfile]] = ContextVar(
    "_identity_profile", default=None
)


# ---------------------------------------------------------------------------
# Identity API
# ---------------------------------------------------------------------------


def set_request_identity(
    tenant_id: str, user_id: str
) -> Tuple[Token[Optional[str]], Token[Optional[str]]]:
    """Set per-request identity.  Returns tokens for later reset."""
    t1 = _request_tenant_id.set(tenant_id)
    t2 = _request_user_id.set(user_id)
    return (t1, t2)


def reset_request_identity(
    tokens: Tuple[Token[Optional[str]], Token[Optional[str]]],
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


def set_identity_profile(profile: IdentityProfile) -> Token[Optional[IdentityProfile]]:
    """Set the current request identity profile."""
    return _identity_profile.set(profile)


def reset_identity_profile(token: Token[Optional[IdentityProfile]]) -> None:
    """Reset the identity profile contextvar."""
    _identity_profile.reset(token)


def get_identity_profile(
    *,
    session_id: str = "",
    collection: str = "",
) -> IdentityProfile:
    """Return the current identity profile with optional overrides."""
    profile = _identity_profile.get()
    if profile is None:
        tenant_id, user_id = get_effective_identity()
        profile = IdentityProfile(
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=get_effective_project_id(),
            collection=get_collection_name() or "",
        )
    updates = {}
    if session_id:
        updates["session_id"] = session_id
    if collection:
        updates["collection"] = collection
    if updates:
        return profile.model_copy(update=updates)
    return profile


# ---------------------------------------------------------------------------
# Project ID API
# ---------------------------------------------------------------------------


def set_request_project_id(project_id: str) -> Token[Optional[str]]:
    """Set per-request project ID.  Returns token for later reset."""
    return _request_project_id.set(project_id)


def reset_request_project_id(token: Token[Optional[str]]) -> None:
    """Reset project ID contextvar using token from :func:`set_request_project_id`."""
    _request_project_id.reset(token)


def get_effective_project_id() -> str:
    """Return the effective project ID for the current request.

    Falls back to "public" when no header is set.
    """
    return _request_project_id.get() or _DEFAULT_PROJECT


# ---------------------------------------------------------------------------
# Collection Name API
# ---------------------------------------------------------------------------

_collection_name: ContextVar[Optional[str]] = ContextVar(
    "_collection_name", default=None
)


def get_collection_name() -> Optional[str]:
    """Return the active collection name override for the current request.

    Returns None when no X-Collection header was set (use default collection).
    """
    return _collection_name.get()


def set_collection_name(name: str) -> Token[Optional[str]]:
    """Set per-request collection name override.  Returns token for later reset."""
    return _collection_name.set(name)


def reset_collection_name(token: Token[Optional[str]]) -> None:
    """Reset collection name contextvar using token from :func:`set_collection_name`."""
    _collection_name.reset(token)


# ---------------------------------------------------------------------------
# Role API
# ---------------------------------------------------------------------------

_request_role: ContextVar[Optional[str]] = ContextVar("_request_role", default=None)


def set_request_role(role: str) -> Token[Optional[str]]:
    """Set per-request role. Returns token for later reset."""
    return _request_role.set(role)


def reset_request_role(token: Token[Optional[str]]) -> None:
    """Reset role contextvar."""
    _request_role.reset(token)


def get_effective_role() -> str:
    """Return the effective role for the current request ('admin' or 'user')."""
    return _request_role.get() or "user"


def is_admin() -> bool:
    """Return True if the current request is from an admin token."""
    return get_effective_role() == "admin"
