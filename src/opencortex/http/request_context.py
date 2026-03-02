# SPDX-License-Identifier: Apache-2.0
"""
Per-request identity and client configuration via contextvars.

In multi-tenant HTTP mode, each request carries its own identity and
client preferences via HTTP headers.  The middleware sets these into
contextvars so that downstream code can read the effective values
without changing method signatures.

Headers:
    X-Tenant-ID                   — tenant identifier (default: "default")
    X-User-ID                     — user identifier (default: "default")
    X-Share-Skills-To-Team        — enable skill sharing (default: "false")
    X-Skill-Share-Mode            — "manual" | "auto_safe" | "auto_aggressive"
    X-Skill-Share-Score-Threshold — minimum share score (default: "0.85")
    X-ACE-Scope-Enforcement       — enable write auth checks (default: "false")

When no contextvar is set (e.g. in tests or single-tenant mode), callers
fall back to defaults.
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_DEFAULT_TENANT = "default"
_DEFAULT_USER = "default"

# ---------------------------------------------------------------------------
# Identity contextvars
# ---------------------------------------------------------------------------
_request_tenant_id: ContextVar[Optional[str]] = ContextVar(
    "_request_tenant_id", default=None
)
_request_user_id: ContextVar[Optional[str]] = ContextVar(
    "_request_user_id", default=None
)

# ---------------------------------------------------------------------------
# ACE skill sharing contextvars
# ---------------------------------------------------------------------------
_request_share_skills_to_team: ContextVar[Optional[bool]] = ContextVar(
    "_request_share_skills_to_team", default=None
)
_request_skill_share_mode: ContextVar[Optional[str]] = ContextVar(
    "_request_skill_share_mode", default=None
)
_request_skill_share_score_threshold: ContextVar[Optional[float]] = ContextVar(
    "_request_skill_share_score_threshold", default=None
)
_request_ace_scope_enforcement: ContextVar[Optional[bool]] = ContextVar(
    "_request_ace_scope_enforcement", default=None
)


# ---------------------------------------------------------------------------
# Token type alias
# ---------------------------------------------------------------------------
RequestTokens = List[Token]


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


# ---------------------------------------------------------------------------
# ACE config API
# ---------------------------------------------------------------------------

@dataclass
class ACEConfig:
    """Per-request ACE skill sharing configuration."""
    share_skills_to_team: bool = False
    skill_share_mode: str = "manual"
    skill_share_score_threshold: float = 0.85
    ace_scope_enforcement_enabled: bool = False


def set_request_ace_config(
    share_skills_to_team: bool = False,
    skill_share_mode: str = "manual",
    skill_share_score_threshold: float = 0.85,
    ace_scope_enforcement: bool = False,
) -> RequestTokens:
    """Set per-request ACE config.  Returns tokens for later reset."""
    return [
        _request_share_skills_to_team.set(share_skills_to_team),
        _request_skill_share_mode.set(skill_share_mode),
        _request_skill_share_score_threshold.set(skill_share_score_threshold),
        _request_ace_scope_enforcement.set(ace_scope_enforcement),
    ]


def reset_request_ace_config(tokens: RequestTokens) -> None:
    """Reset ACE config contextvars."""
    _request_share_skills_to_team.reset(tokens[0])
    _request_skill_share_mode.reset(tokens[1])
    _request_skill_share_score_threshold.reset(tokens[2])
    _request_ace_scope_enforcement.reset(tokens[3])


def get_effective_ace_config() -> ACEConfig:
    """Return the effective ACE config for the current request."""
    return ACEConfig(
        share_skills_to_team=_request_share_skills_to_team.get() or False,
        skill_share_mode=_request_skill_share_mode.get() or "manual",
        skill_share_score_threshold=_request_skill_share_score_threshold.get() or 0.85,
        ace_scope_enforcement_enabled=_request_ace_scope_enforcement.get() or False,
    )
