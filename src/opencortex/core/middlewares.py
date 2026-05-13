# SPDX-License-Identifier: Apache-2.0
"""FastAPI middlewares for opencortex."""

from __future__ import annotations

import asyncio

import jwt
import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from opencortex.auth.token import (
    decode_token,
    ensure_secret,
    find_token_record,
)
from opencortex.core.identity import (
    IdentityProfile,
    identity_context,
)

logger = structlog.get_logger(__name__)


class WriteRequestContextMiddleware(BaseHTTPMiddleware):
    """Populate request-scoped identity context for write APIs."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Set context variables from headers for the request duration."""
        settings = getattr(request.app.state, "settings", None)
        profile = await authenticated_profile_from_bearer(
            request.headers.get("authorization", ""),
            data_root=str(getattr(settings, "data_root", "./data")),
        )
        if profile is None and protected_path(request.url.path):
            return JSONResponse(
                status_code=401,
                content={"detail": "Bearer token is required"},
            )
        profile = profile or IdentityProfile()
        if not bool(getattr(settings, "identity_context_enabled", True)):
            return await call_next(request)

        profile_token = identity_context.set(
            profile.model_copy(
                update={
                    "collection": request.headers.get("x-collection", ""),
                }
            )
        )
        try:
            return await call_next(request)
        finally:
            identity_context.reset(profile_token)


async def authenticated_profile_from_bearer(
    authorization: str,
    *,
    data_root: str,
) -> IdentityProfile | None:
    """Return identity claims from a saved bearer token."""
    scheme, _, token = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        secret = await asyncio.to_thread(ensure_secret, data_root)
        claims = decode_token(token, secret)
    except jwt.InvalidTokenError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning(
            "opencortex.auth_context_failed",
            error_type=type(exc).__name__,
        )
        return None

    if await asyncio.to_thread(find_token_record, data_root, token) is None:
        return None
    return IdentityProfile(
        tenant_id=str(claims.get("tid", "") or "default"),
        user_id=str(claims.get("uid", "") or "default"),
        project_id=str(claims.get("pid", "") or "public"),
        role=str(claims.get("role", "user") or "user"),
    )


def protected_path(path: str) -> bool:
    """Return whether the request path requires bearer authentication."""
    return (
        path.startswith("/api/")
        or path.startswith("/admin/")
        or path.startswith("/console/")
        or path == "/mcp"
    )
