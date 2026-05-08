# SPDX-License-Identifier: Apache-2.0
"""Identity and routing profile for request-scoped work."""

from __future__ import annotations

from pydantic import BaseModel


class IdentityProfile(BaseModel):
    """Tenant, user, project, session, and collection context."""

    tenant_id: str
    user_id: str
    project_id: str = "public"
    session_id: str = ""
    collection: str = ""

    def with_session(self, session_id: str) -> "IdentityProfile":
        """Return a copy scoped to a session."""
        return self.model_copy(update={"session_id": session_id})

    def with_collection(self, collection: str) -> "IdentityProfile":
        """Return a copy scoped to a collection."""
        return self.model_copy(update={"collection": collection})
