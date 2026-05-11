# SPDX-License-Identifier: Apache-2.0
"""Authentication and admin token routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from opencortex.auth.token import (
    ensure_secret,
    generate_token,
    load_token_records,
    public_token_record,
    revoke_token,
    save_token_record,
)
from opencortex.core.identity import IdentityProfile, get_identity_profile

auth_router = APIRouter(prefix="/api/v1")
admin_router = APIRouter(prefix="/admin/v1")


class AuthMeResponse(BaseModel):
    """Current authenticated identity."""

    tenant_id: str
    user_id: str
    project_id: str = "public"
    role: str = "user"


class TokenCreateRequest(BaseModel):
    """Admin request to create one user API key."""

    tenant_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    role: str = Field(default="user", pattern=r"^(user|admin)$")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def strip_values(self) -> "TokenCreateRequest":
        """Normalize text fields."""
        self.tenant_id = self.tenant_id.strip()
        self.user_id = self.user_id.strip()
        self.role = self.role.strip() or "user"
        return self


class TokenCreateResponse(BaseModel):
    """Created API key response."""

    token: str
    tenant_id: str
    user_id: str
    role: str


class TokenRecordResponse(BaseModel):
    """Public token record returned to admin UI."""

    tenant_id: str
    user_id: str
    role: str
    created_at: str
    token_prefix: str
    token: str = ""


class TokenListResponse(BaseModel):
    """Admin token list response."""

    tokens: list[TokenRecordResponse]


class TokenRevokeRequest(BaseModel):
    """Request to revoke one token by visible prefix."""

    token_prefix: str = Field(..., min_length=4)

    model_config = ConfigDict(extra="forbid")


class TokenRevokeResponse(BaseModel):
    """Revoke result response."""

    status: str = "ok"
    revoked: bool = True


@auth_router.get("/auth/me")
async def auth_me() -> AuthMeResponse:
    """Return the identity derived from the current bearer token."""
    profile = get_identity_profile()
    return AuthMeResponse(
        tenant_id=profile.tenant_id,
        user_id=profile.user_id,
        project_id=profile.project_id,
        role=profile.role,
    )


@admin_router.get("/tokens")
async def admin_list_tokens(
    _admin: Annotated[IdentityProfile, Depends(require_admin)],
    request: Request,
) -> TokenListResponse:
    """List issued API keys without exposing full token values."""
    records = [
        TokenRecordResponse.model_validate(public_token_record(record))
        for record in load_token_records(data_root(request))
    ]
    return TokenListResponse(tokens=records)


@admin_router.post("/tokens")
async def admin_create_token(
    req: TokenCreateRequest,
    _admin: Annotated[IdentityProfile, Depends(require_admin)],
    request: Request,
) -> TokenCreateResponse:
    """Create one tenant/user API key."""
    root = data_root(request)
    secret = ensure_secret(root)
    token = generate_token(
        req.tenant_id,
        req.user_id,
        secret,
        role=req.role,
    )
    save_token_record(
        root,
        token,
        req.tenant_id,
        req.user_id,
        role=req.role,
    )
    return TokenCreateResponse(
        token=token,
        tenant_id=req.tenant_id,
        user_id=req.user_id,
        role=req.role,
    )


@admin_router.delete("/tokens")
async def admin_revoke_token(
    req: TokenRevokeRequest,
    _admin: Annotated[IdentityProfile, Depends(require_admin)],
    request: Request,
) -> TokenRevokeResponse:
    """Revoke one API key by visible token prefix."""
    removed = revoke_token(data_root(request), req.token_prefix.strip())
    if not removed:
        raise HTTPException(status_code=404, detail="Token not found")
    return TokenRevokeResponse()


def require_admin() -> IdentityProfile:
    """Require the current request to be authenticated as an admin."""
    profile = get_identity_profile()
    if profile.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return profile


def data_root(request: Request) -> str:
    """Return configured token storage root."""
    settings = getattr(request.app.state, "settings", None)
    return str(getattr(settings, "data_root", "./data"))


__all__ = ["admin_router", "auth_router"]
