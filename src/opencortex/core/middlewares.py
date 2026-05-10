# SPDX-License-Identifier: Apache-2.0
"""FastAPI middlewares for opencortex."""

from __future__ import annotations

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from opencortex.core.identity import (
    IdentityProfile,
    identity_context,
)


class WriteRequestContextMiddleware(BaseHTTPMiddleware):
    """Populate request-scoped identity context for write APIs."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Set context variables from headers for the request duration."""
        tenant_id = request.headers.get("x-tenant-id", "default")
        user_id = request.headers.get("x-user-id", "default")
        project_id = request.headers.get("x-project-id", "public")
        collection = request.headers.get("x-collection", "")

        profile_token = identity_context.set(
            IdentityProfile(
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=project_id,
                collection=collection,
            )
        )
        try:
            return await call_next(request)
        finally:
            identity_context.reset(profile_token)
