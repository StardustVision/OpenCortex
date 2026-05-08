# SPDX-License-Identifier: Apache-2.0
"""Identity profile context for opencortex_app."""

from __future__ import annotations

from contextvars import ContextVar, Token

from pydantic import BaseModel


class IdentityProfile(BaseModel):
    """Request-scoped identity profile."""

    tenant_id: str = "default"
    user_id: str = "default"
    project_id: str = "public"
    collection: str = ""
    session_id: str = ""


_profile: ContextVar[IdentityProfile | None] = ContextVar("profile", default=None)


class IdentityContext:
    """ContextVar-backed identity profile holder."""

    def set(self, profile: IdentityProfile) -> Token[IdentityProfile | None]:
        """Set the current identity profile."""
        return _profile.set(profile)

    def reset(self, token: Token[IdentityProfile | None]) -> None:
        """Reset the current identity profile."""
        _profile.reset(token)

    def get(
        self,
        *,
        session_id: str = "",
        collection: str = "",
    ) -> IdentityProfile:
        """Return current identity profile with optional overrides."""
        profile = _profile.get() or IdentityProfile()
        updates = {}
        if session_id:
            updates["session_id"] = session_id
        if collection:
            updates["collection"] = collection
        if updates:
            return profile.model_copy(update=updates)
        return profile

    @property
    def collection_name(self) -> str | None:
        """Return request collection override."""
        return self.get().collection or None

    @property
    def project_id(self) -> str:
        """Return current project id."""
        return self.get().project_id or "public"


identity_context = IdentityContext()


def set_identity_profile(profile: IdentityProfile) -> Token[IdentityProfile | None]:
    """Set the current identity profile."""
    return identity_context.set(profile)


def reset_identity_profile(token: Token[IdentityProfile | None]) -> None:
    """Reset the current identity profile."""
    identity_context.reset(token)


def get_identity_profile(
    *,
    session_id: str = "",
    collection: str = "",
) -> IdentityProfile:
    """Return current identity profile with optional overrides."""
    return identity_context.get(session_id=session_id, collection=collection)


def get_collection_name() -> str | None:
    """Return request collection override."""
    return identity_context.collection_name


def get_effective_project_id() -> str:
    """Return current project id."""
    return identity_context.project_id
